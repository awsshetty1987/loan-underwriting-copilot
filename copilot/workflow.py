"""The LangGraph workflow (NB03): one node per agent, stop points after the first three, and a pause for the
human underwriter after the memo. Paused cases are kept by the checkpointer (Postgres in PROD)."""
import operator
import uuid
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents, telemetry

DECISIONS = {"approve", "decline", "refer"}        # refer = more documents or a senior review


class LoanState(TypedDict, total=False):
    request: str
    applicant_id: str
    status: str            # set only when the run stops early or finishes: blocked / missing_documents / identity_mismatch / done
    reason: str
    slip: dict
    report: dict
    flags: list
    policy: dict
    risk_tier: str
    memo: dict
    human_decision: dict
    metrics: Annotated[list, operator.add]     # one entry per agent run: time and GPT calls (telemetry.py)


def guardrails_node(state: LoanState) -> dict:
    g = agents.guardrails_agent(state["request"])
    if g["blocked"]:
        return {"status": "blocked", "reason": g["reason"]}
    return {"applicant_id": g["applicant_id"]}


async def documents_node(state: LoanState) -> dict:
    docs = await agents.document_extractor_agent(state["applicant_id"])
    if docs["missing"]:
        return {"status": "missing_documents",
                "reason": f"{len(docs['missing'])} document(s) missing: {', '.join(docs['missing'])}"}
    return {"slip": docs["slip"]}


async def bureau_node(state: LoanState) -> dict:
    bureau = await agents.credit_bureau_resolver_agent(state["applicant_id"], state["slip"])
    if not bureau["identity_ok"]:
        await agents.audit_logger_agent(state["applicant_id"],
                                        {"decision": "stopped: identity mismatch", "reason": bureau["reason"]})
        return {"status": "identity_mismatch", "reason": bureau["reason"]}
    return {"report": bureau["report"]}


async def consistency_node(state: LoanState) -> dict:
    return {"flags": await agents.consistency_checker_agent(state["applicant_id"], state["slip"])}


async def policy_node(state: LoanState) -> dict:
    policy = await agents.policy_rule_engine_agent(state["applicant_id"], state["report"], state["flags"], state["slip"])
    return {"policy": policy, "flags": policy["flags"]}


async def risk_node(state: LoanState) -> dict:
    return {"risk_tier": await agents.risk_scorer_agent(state["report"], state["policy"]["history"], state["flags"])}


def memo_node(state: LoanState) -> dict:
    s, r, p, tier = state["slip"], state["report"], state["policy"], state["risk_tier"]
    return {"memo": agents.memo_drafter_agent({
        "applicant_id": state["applicant_id"], "name": s["name"], "employer": s["employer"],
        "declared_income": agents.rupees(s["declared_income"]), "net_pay": agents.rupees(s.get("net_pay")),
        **agents.affordability_facts(p.get("affordability")),
        "credit_score": r["credit_score"], "existing_loans": r["existing_loans"],
        "bureau_defaults": r["defaults"], "bank_defaults": p["history"].get("past_loan_defaults", 0),
        "flags": state["flags"], "violations": p["violations"], "risk_tier": tier,
        "suggested_next_step": agents.NEXT_STEP[tier]})}


def human_review_node(state: LoanState) -> dict:
    """Pauses here. Resumes only when CaseService.submit_decision sends the underwriter's answer."""
    answer = interrupt({"applicant_id": state["applicant_id"], "risk_tier": state["risk_tier"],
                        "memo": state["memo"]["memo"]})
    return {"human_decision": answer}


async def audit_node(state: LoanState) -> dict:
    human = state.get("human_decision")
    await agents.audit_logger_agent(state["applicant_id"], {
        "risk_tier": state["risk_tier"], "credit_score": state["report"]["credit_score"],
        "flags": state["flags"], "violations": state["policy"]["violations"],
        "affordability": state["policy"].get("affordability"),
        "memo": state["memo"]["memo"], "memo_written_by": state["memo"]["written_by"],
        "decision": human["decision"] if human else "pending human underwriter sign-off",
        "decided_by": human["underwriter"] if human else None,
        "comment": human["comment"] if human else None,
        "rules_version": state["policy"]["rules"].get("rules_version"),
        "run_metrics": telemetry.summarize(state.get("metrics"))})
    return {"status": "done"}


def _stop_or(next_node: str):
    return lambda state: END if state.get("status") else next_node


def build_workflow(checkpointer, with_human_review: bool = True):
    graph = StateGraph(LoanState)
    for name, node in [("guardrails", guardrails_node), ("documents", documents_node), ("bureau", bureau_node),
                       ("consistency", consistency_node), ("policy", policy_node), ("risk", risk_node),
                       ("memo", memo_node), ("audit", audit_node)]:
        graph.add_node(name, telemetry.traced_node(name)(node))
    graph.add_edge(START, "guardrails")
    graph.add_conditional_edges("guardrails", _stop_or("documents"), ["documents", END])
    graph.add_conditional_edges("documents", _stop_or("bureau"), ["bureau", END])
    graph.add_conditional_edges("bureau", _stop_or("consistency"), ["consistency", END])
    graph.add_edge("consistency", "policy")
    graph.add_edge("policy", "risk")
    graph.add_edge("risk", "memo")
    if with_human_review:
        graph.add_node("human_review", human_review_node)
        graph.add_edge("memo", "human_review")
        graph.add_edge("human_review", "audit")
    else:
        graph.add_edge("memo", "audit")
    graph.add_edge("audit", END)
    return graph.compile(checkpointer=checkpointer)


def _screen_facts(state: dict) -> dict:
    """The few extra facts the screen shows next to the tier: who, credit score, hard-rule failures."""
    slip, report, policy = state.get("slip") or {}, state.get("report") or {}, state.get("policy") or {}
    return {"name": slip.get("name"), "employer": slip.get("employer"), "credit_score": report.get("credit_score"),
            "violations": policy.get("violations"), "memo_written_by": (state.get("memo") or {}).get("written_by"),
            "rules_version": (policy.get("rules") or {}).get("rules_version"),
            "run_metrics": telemetry.summarize(state.get("metrics"))}


class CaseNotFound(LookupError):
    pass


class CaseConflict(RuntimeError):
    pass


class CaseService:
    """Start a case, look it up, record the underwriter's decision."""

    def __init__(self, checkpointer):
        self.workflow = build_workflow(checkpointer)

    @staticmethod
    def _config(case_id: str) -> dict:
        return {"configurable": {"thread_id": case_id}}

    async def start(self, request_text: str) -> dict:
        case_id = uuid.uuid4().hex[:12]
        with telemetry.tracer.start_as_current_span("underwrite case") as span:
            span.set_attribute("copilot.case_id", case_id)
            state = await self.workflow.ainvoke({"request": request_text}, self._config(case_id))
            span.set_attribute("copilot.applicant_id", state.get("applicant_id") or "")
            span.set_attribute("copilot.risk_tier", state.get("risk_tier") or "")
            span.set_attribute("copilot.status", "waiting_for_underwriter" if "__interrupt__" in state
                               else state.get("status", ""))
        waiting = "__interrupt__" in state
        return {"case_id": case_id, "status": "waiting_for_underwriter" if waiting else state["status"],
                "applicant_id": state.get("applicant_id"), "risk_tier": state.get("risk_tier"),
                "memo": state.get("memo", {}).get("memo"), "reason": state.get("reason"),
                "flags": state.get("flags"), "affordability": state.get("policy", {}).get("affordability"),
                **_screen_facts(state)}

    async def get(self, case_id: str) -> dict:
        snapshot = await self.workflow.aget_state(self._config(case_id))
        if not snapshot.values:
            raise CaseNotFound(case_id)
        v = snapshot.values
        return {"case_id": case_id, "applicant_id": v.get("applicant_id"), "risk_tier": v.get("risk_tier"),
                "status": "waiting_for_underwriter" if "human_review" in snapshot.next else v.get("status"),
                "memo": v.get("memo", {}).get("memo"), "flags": v.get("flags"),
                "affordability": v.get("policy", {}).get("affordability"),
                "human_decision": v.get("human_decision"), **_screen_facts(v)}

    async def submit_decision(self, case_id: str, decision: str, underwriter: str, comment: str = "") -> dict:
        current = await self.get(case_id)
        if current["status"] != "waiting_for_underwriter":
            raise CaseConflict(f"Case is {current['status']}, not waiting for a decision")
        decision = (decision or "").strip().lower()
        if decision not in DECISIONS:
            raise ValueError(f"Decision must be one of {sorted(DECISIONS)}")
        if not (underwriter or "").strip():
            raise ValueError("Underwriter name/ID is required")
        if decision == "approve" and current["risk_tier"] == "HIGH" and not (comment or "").strip():
            raise ValueError("Approving a HIGH-risk application needs a written reason (comment)")
        answer = {"decision": decision, "underwriter": underwriter.strip(), "comment": (comment or "").strip()}
        with telemetry.tracer.start_as_current_span("underwriter decision") as span:
            span.set_attribute("copilot.case_id", case_id)
            span.set_attribute("copilot.decision", decision)
            state = await self.workflow.ainvoke(Command(resume=answer), self._config(case_id))
        return {"case_id": case_id, "status": state["status"], "applicant_id": state["applicant_id"],
                "risk_tier": state["risk_tier"], **state["human_decision"]}

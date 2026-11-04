"""The 8 agents (NB02 + NB04). Each one may call only its own MCP tools (AGENT_TOOLS).
GPT is used in two places only: reading the payslip (inside the MCP tool) and writing the memo."""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import telemetry
from .llm import call_llm
from .tools import inr

log = logging.getLogger(__name__)

REQUEST_PATTERN = re.compile(r"\s*underwrite applicant (APP_\d{4})\s*", re.IGNORECASE)
BLOCKED_PHRASES = ["auto approve", "auto-approve", "approve without", "skip review", "ignore policy",
                   "bypass", "override"]
AGENT_TOOLS = {
    "document_extractor": {"find_missing_documents", "read_salary_slip"},
    "credit_bureau_resolver": {"get_credit_bureau_report"},
    "consistency_checker": {"check_income", "check_documents"},
    "policy_rule_engine": {"get_policy_rules", "get_core_banking_history", "check_affordability"},
    "risk_scorer": {"calculate_risk_score"},
    "audit_logger": {"log_audit_action"},
}
NEXT_STEP = {"LOW": "Standard review; no issues found.",
             "MEDIUM": "Not a clear LOW (flags, or score/loans just outside LOW limits); review before deciding.",
             "HIGH": "Hard rule(s) broken; review carefully, likely not eligible as is."}
MEMO_RULES = """You write short credit memos for a bank's human underwriter, in plain English.
Use ONLY the facts given. Do not invent numbers, names or events.
Never say the loan is approved or rejected: a human underwriter makes that decision.
Write exactly these 4 parts:
Summary: 3-4 bullets with the key facts, including the loan asked for, its EMI and the FOIR when given.
Why this risk tier: 1-2 sentences linking the facts to the tier (what pushed it up or kept it down).
Possible explanations: 1-2 bullets on what could explain any flag or broken rule, clearly worded as possibilities, not facts.
Ask the customer for: 2-3 specific documents or questions that would resolve the open points.
This is an Indian bank: suggest Indian documents (salary slips, Form 16, ITR, bank statement of any other salary account).
Base 'Why this risk tier' on the why_this_tier fact. If a document looks edited or copied, say it plainly and
suggest getting the original directly from the employer or bank. Do not guess about data you were not given.
End with the line 'Decision: pending human underwriter sign-off'."""
LOW_MIN_SCORE, LOW_MAX_LOANS = 700, 1


class ToolClient:
    """One MCP server process, started on first use and shared by every case (the notebooks started one per call).
    A background task owns the connection; if the server dies, the next call starts a new one."""

    def __init__(self):
        self._session = None
        self._task = None
        self._loop = None
        self._ready = None
        self._stop = None
        self._error = None

    def _params(self) -> StdioServerParameters:
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return StdioServerParameters(command=sys.executable, args=["-m", "copilot.mcp_server"], env=env, cwd=root)

    async def _run(self):
        try:
            async with stdio_client(self._params(), errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as e:                       # server failed to start or crashed
            self._error = e
            log.exception("MCP tool server stopped")
        finally:
            self._session = None
            self._ready.set()

    async def session(self) -> ClientSession:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._task is None or self._task.done():
            self._loop, self._error = loop, None
            self._ready, self._stop = asyncio.Event(), asyncio.Event()
            self._task = loop.create_task(self._run())
        await self._ready.wait()
        if self._session is None:
            raise RuntimeError(f"MCP tool server is not running: {self._error!r}")
        return self._session

    async def close(self):
        if self._task and not self._task.done() and self._loop is asyncio.get_running_loop():
            self._stop.set()
            await self._task


tool_client = ToolClient()


async def use_tool(agent: str, tool: str, **args):
    """Checks the agent may use this tool, then calls it on the MCP server and returns plain Python data."""
    if tool not in AGENT_TOOLS.get(agent, set()):
        raise PermissionError(f"{agent} is not allowed to use {tool}")
    with telemetry.tracer.start_as_current_span(f"tool {tool}") as span:
        span.set_attribute("copilot.agent", agent)
        session = await tool_client.session()
        result = await session.call_tool(tool, args)
        if result.isError:
            span.set_attribute("error", True)
            raise RuntimeError(f"{tool} failed: {result.content[0].text}")
        data = result.structuredContent
        if data is None:
            data = json.loads(result.content[0].text)
        elif set(data) == {"result"}:
            data = data["result"]
        if isinstance(data, dict) and "llm_usage" in data:      # GPT calls made inside the tool server process
            for call in data.pop("llm_usage"):
                telemetry.add_llm_span(call)
                telemetry.record_llm_call(call)
        return data


def rupees(amount) -> str:
    return f"{inr(amount)} / month" if amount else "missing"


# ---------- Agent 1: guardrails ----------

def guardrails_agent(request_text: str) -> dict:
    """Blocks attempts to skip the human, and anything that isn't 'Underwrite applicant APP_1234'."""
    lowered = (request_text or "").lower()
    for phrase in BLOCKED_PHRASES:
        if phrase in lowered:
            return {"blocked": True, "applicant_id": None,
                    "reason": f"Request tries to bypass human review ('{phrase}')"}
    match = REQUEST_PATTERN.fullmatch(request_text or "")
    if not match:
        return {"blocked": True, "applicant_id": None,
                "reason": "Expected a request like 'Underwrite applicant APP_1234'"}
    return {"blocked": False, "applicant_id": match.group(1).upper(), "reason": None}


# ---------- Agents 2 to 6 ----------

async def document_extractor_agent(applicant_id: str) -> dict:
    missing = await use_tool("document_extractor", "find_missing_documents", applicant_id=applicant_id)
    if missing:
        return {"missing": missing, "slip": None}
    return {"missing": [], "slip": await use_tool("document_extractor", "read_salary_slip", applicant_id=applicant_id)}


async def credit_bureau_resolver_agent(applicant_id: str, slip: dict) -> dict:
    """On a PAN mismatch the bureau is never called."""
    try:
        report = await use_tool("credit_bureau_resolver", "get_credit_bureau_report",
                                applicant_id=applicant_id, slip=slip)
    except RuntimeError as e:
        msg = str(e)
        if "does not match KYC PAN" in msg:
            return {"identity_ok": False, "report": None, "reason": msg[msg.find("PAN on salary slip"):]}
        raise
    return {"identity_ok": True, "report": report, "reason": None}


async def consistency_checker_agent(applicant_id: str, slip: dict) -> list:
    flags = await use_tool("consistency_checker", "check_income", applicant_id=applicant_id,
                           declared_income=slip["declared_income"], employer=slip["employer"],
                           net_pay=slip.get("net_pay"))
    return flags + await use_tool("consistency_checker", "check_documents", applicant_id=applicant_id, slip=slip)


async def policy_rule_engine_agent(applicant_id: str, report: dict, flags: list, slip: dict | None = None) -> dict:
    """Hard rules from rules.json, plus affordability (FOIR) and eligibility. Returns every flag."""
    rules = await use_tool("policy_rule_engine", "get_policy_rules")
    history = await use_tool("policy_rule_engine", "get_core_banking_history", applicant_id=applicant_id)
    afford = await use_tool("policy_rule_engine", "check_affordability", applicant_id=applicant_id,
                            credit_report=report, slip=slip or {})
    all_flags = flags + afford.pop("flags")
    violations = []
    if report["credit_score"] < rules["min_credit_score"]:
        violations.append(f"Credit score {report['credit_score']} is below the minimum {rules['min_credit_score']}")
    if report["existing_loans"] > rules["max_existing_loans"]:
        violations.append(f"{report['existing_loans']} existing loans, more than the maximum {rules['max_existing_loans']}")
    if rules["auto_reject_if_default_exists"]:
        if report["defaults"] > 0:
            violations.append(f"{report['defaults']} default(s) on the credit bureau report")
        if history.get("past_loan_defaults", 0) > 0:
            violations.append(f"{history['past_loan_defaults']} past default(s) with our own bank")
    violations += [f for f in all_flags if f.startswith("MAJOR")]
    return {"rules": rules, "history": history, "violations": violations, "passes_hard_rules": not violations,
            "affordability": afford, "flags": all_flags}


async def risk_scorer_agent(report: dict, history: dict, flags: list) -> str:
    return await use_tool("risk_scorer", "calculate_risk_score",
                          credit_report=report, banking_history=history, flags=flags)


# ---------- Agent 7: memo ----------

def affordability_facts(a: dict | None) -> dict:
    """The loan numbers in words, for the memo and the screen."""
    if not a or a.get("loan_amount") is None:
        return {"loan_request": "none on file"}
    return {"loan_request": f"{inr(a['loan_amount'])} over {a['tenure_months']} months at {a['interest_rate']}% "
                            f"({a['purpose']})",
            "new_emi": f"{inr(a['new_emi'])}/month", "existing_emis": f"{inr(a['existing_emi'])}/month",
            "salary_credited": f"{inr(a['salary_credited'])}/month",
            "foir": f"{a['foir']:.0%} (limit {a['max_foir']:.0%})" if a.get("foir") is not None else "not computable",
            "largest_affordable_loan": inr(a["max_loan"])}


def tier_reason(facts: dict) -> str:
    """Why the tier is what it is, worked out in code so GPT never has to guess."""
    if facts["risk_tier"] == "HIGH":
        return "HIGH because: " + "; ".join(facts["violations"] or ["a hard rule is broken"])
    if facts["risk_tier"] == "MEDIUM":
        if facts["flags"]:
            return "MEDIUM because of: " + "; ".join(facts["flags"])
        return (f"MEDIUM because LOW needs score >= {LOW_MIN_SCORE} and at most {LOW_MAX_LOANS} loan(s); "
                f"this applicant has score {facts['credit_score']} and {facts['existing_loans']} loan(s)")
    return f"LOW: score {facts['credit_score']}, {facts['existing_loans']} loan(s), no flags, no rules broken"


def fallback_memo(facts: dict) -> str:
    """Plain-code memo, used when GPT is unavailable or breaks the rules."""
    lines = [f"- Applicant {facts['applicant_id']} ({facts['name']}, {facts['employer']}), "
             f"gross pay {facts['declared_income']}"
             + (f", net pay {facts['net_pay']}" if facts.get("net_pay") not in (None, "missing") else ""),
             f"- Credit score {facts['credit_score']}, {facts['existing_loans']} existing loan(s), "
             f"{facts['bureau_defaults']} bureau default(s), {facts['bank_defaults']} default(s) with our bank"]
    if facts.get("foir"):
        lines.append(f"- Loan asked: {facts['loan_request']}; EMI {facts['new_emi']}; FOIR {facts['foir']}; "
                     f"largest affordable loan {facts['largest_affordable_loan']}")
    lines.append(f"- Risk tier: {facts['risk_tier']}")
    lines += [f"- Rule broken: {v}" for v in facts["violations"]]
    lines += [f"- Flag: {f}" for f in facts["flags"] if f not in facts["violations"]]
    lines += [f"- Suggested next step: {NEXT_STEP[facts['risk_tier']]}",
              "Decision: pending human underwriter sign-off"]
    return "\n".join(lines)


def memo_drafter_agent(facts: dict) -> dict:
    """Returns {'memo': text, 'written_by': 'GPT' or 'fallback'}. A memo that says approved/rejected is thrown away."""
    facts = {**facts, "why_this_tier": tier_reason(facts)}
    try:
        text = call_llm(MEMO_RULES, json.dumps(facts, indent=2), purpose="write memo", pii_names=[facts.get("name")])
    except Exception as e:
        log.warning("GPT unavailable (%s), using the plain-code memo", type(e).__name__)
        text = None
    if text and "approved" not in text.lower() and "rejected" not in text.lower():
        text = text.strip()
        if "human underwriter" not in text.lower():
            text += "\nDecision: pending human underwriter sign-off"
        return {"memo": text, "written_by": "GPT"}
    return {"memo": fallback_memo(facts), "written_by": "fallback"}


# ---------- Agent 8: audit ----------

async def audit_logger_agent(applicant_id: str, details: dict, action: str = "underwriting_completed") -> dict:
    return await use_tool("audit_logger", "log_audit_action", applicant_id=applicant_id, action=action, details=details)

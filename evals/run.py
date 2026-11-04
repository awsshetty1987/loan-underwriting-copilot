"""Offline evaluation of the copilot.

    python -m evals.run                      # print the report
    python -m evals.run --ci --out report.md # also fail (exit 1) if a score is under evals/thresholds.json
    python -m evals.run --judge              # with OPENAI_API_KEY: GPT also grades each memo for faithfulness

Every labelled applicant goes through the real workflow (a temporary copy of sample_data, the stand-in bank systems
on a local port, no human step). Without OPENAI_API_KEY it scores the plain-code payslip reader and memo, which is
what CI runs; with a key it scores GPT, so the same command compares a model or prompt change against the baseline.

Scores: payslip extraction accuracy, risk-tier accuracy, fraud recall and false alarms, memo rules and grounding,
guardrail accuracy, and time / tokens / cost per case."""
import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
ALWAYS_OK_NUMBERS = set(range(0, 13))        # counts like "2 loans", "4 parts", months: not worth flagging


def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# ---------- environment: same as the tests ----------

def start_environment() -> Path:
    data = Path(tempfile.mkdtemp(prefix="copilot-eval-"))
    shutil.copytree(ROOT / "sample_data", data, dirs_exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    os.environ.update({"STORAGE_BACKEND": "local", "LOCAL_DATA_DIR": str(data),
                       "CREDIT_BUREAU_URL": f"http://127.0.0.1:{port}/credit-report",
                       "CREDIT_BUREAU_API_KEY": "eval-bureau-key",
                       "CORE_BANKING_URL": f"http://127.0.0.1:{port}/core-banking/history",
                       "CORE_BANKING_API_KEY": "eval-core-banking-key"})
    import uvicorn

    from copilot.bank_systems_mock import app as bank_systems
    server = uvicorn.Server(uvicorn.Config(bank_systems, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return data


# ---------- 1. payslip extraction ----------

def _same(expected, actual) -> bool:
    if isinstance(expected, (int, float)) or isinstance(actual, (int, float)):
        try:
            return abs(float(expected) - float(actual)) < 0.5
        except (TypeError, ValueError):
            return False
    norm = lambda v: " ".join(str(v or "").lower().split())     # noqa: E731
    return norm(expected) == norm(actual)


def eval_extraction() -> dict:
    from copilot import telemetry, tools
    truth = _load("extraction_truth.json")["applicants"]
    per_field, misses, readers, calls = Counter(), [], Counter(), []
    for app, expected in truth.items():
        with telemetry.collect_llm_calls() as c:
            got = tools.extract_income_fields(tools.get_document(app, "income"))
        calls += c
        readers[got.get("extracted_by")] += 1
        for field, value in expected.items():
            if _same(value, got.get(field)):
                per_field[field] += 1
            else:
                misses.append(f"{app} {field}: expected {value!r}, got {got.get(field)!r}")
    n = len(truth)
    fields = {f: per_field[f] / n for f in next(iter(truth.values()))}
    return {"applicants": n, "field_accuracy": sum(per_field.values()) / (n * len(fields)),
            "per_field": fields, "misses": misses, "read_by": dict(readers), "llm": telemetry.summarize(
                [{"agent": "extraction", "ms": 0, "llm": calls}])}


# ---------- 2-4. run the workflow on every labelled applicant ----------

async def run_cases(apps: list[str]) -> dict:
    from langgraph.checkpoint.memory import MemorySaver

    from copilot.agents import tool_client
    from copilot.workflow import build_workflow
    workflow = build_workflow(MemorySaver(), with_human_review=False)
    results = {}
    for app in apps:
        results[app] = await workflow.ainvoke({"request": f"Underwrite applicant {app}"},
                                              {"configurable": {"thread_id": f"eval-{app}"}})
    await tool_client.close()
    return results


def eval_tiers(results: dict, labels: dict) -> dict:
    confusion, misses = Counter(), []
    for app, label in labels.items():
        got = results[app].get("risk_tier") or results[app].get("status")
        confusion[(label["tier"], got)] += 1
        if got != label["tier"]:
            misses.append(f"{app}: expected {label['tier']}, got {got} ({label['source']} label)")
    correct = sum(v for (e, g), v in confusion.items() if e == g)
    return {"cases": len(labels), "accuracy": correct / len(labels), "misses": misses,
            "confusion": {f"{e} -> {g}": v for (e, g), v in sorted(confusion.items())},
            "hand_labelled": sum(1 for v in labels.values() if v["source"] == "hand")}


def eval_fraud(results: dict, planted: dict) -> dict:
    caught, missed, false_alarms = [], [], []
    for app, expected_flag in planted.items():
        flags = results[app].get("flags") or []
        ok = results[app].get("risk_tier") == "HIGH" and any(f.startswith(expected_flag) for f in flags)
        (caught if ok else missed).append(app)
    for app, state in results.items():
        if app not in planted and any(f.startswith("MAJOR document") for f in state.get("flags") or []):
            false_alarms.append(app)
    return {"planted": len(planted), "recall": len(caught) / len(planted), "missed": missed,
            "false_alarms": false_alarms}


def _numbers(text: str) -> set[float]:
    found = set()
    for m in NUMBER.findall(text):
        try:
            found.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return found


def _grounding_numbers(state: dict) -> set[float]:
    """Every number the memo is allowed to use: anything in the case's facts, plus fractions as percentages."""
    facts = {k: state.get(k) for k in ["applicant_id", "slip", "report", "flags", "policy", "risk_tier"]}
    nums = _numbers(json.dumps(facts, default=str))
    from copilot.tools import inr
    for v in list(nums):
        if 0 < v < 1:
            nums.add(round(v * 100, 2))
            nums.add(round(v * 100))
        if v >= 1000:
            nums.add(round(float(inr(v).replace("Rs.", "").replace(",", "")), 2))
    return nums | {float(n) for n in ALWAYS_OK_NUMBERS}


def eval_memos(results: dict, judge: bool) -> dict:
    rules = Counter()
    problems, scores = [], []
    for app, state in results.items():
        memo = (state.get("memo") or {}).get("memo")
        if not memo:
            continue
        text = memo.lower()
        checks = {"no_decision_words": "approved" not in text and "rejected" not in text,
                  "ends_with_sign_off": memo.strip().endswith("Decision: pending human underwriter sign-off"),
                  "names_the_tier": (state.get("risk_tier") or "").lower() in text}
        allowed = _grounding_numbers(state)
        ungrounded = sorted(n for n in _numbers(memo) if n not in allowed and round(n) not in allowed)
        checks["numbers_grounded"] = not ungrounded
        for name, ok in checks.items():
            rules[name] += ok
        failed = [n for n, ok in checks.items() if not ok]
        if failed:
            problems.append(f"{app}: {', '.join(failed)}" + (f" (numbers not in facts: {ungrounded[:5]})"
                                                             if ungrounded else ""))
        if judge:
            scores.append(_judge(state, memo))
    n = sum(1 for s in results.values() if (s.get("memo") or {}).get("memo"))
    out = {"memos": n, "pass_rate": (n - len(problems)) / n if n else 1.0,
           "per_rule": {k: v / n for k, v in rules.items()}, "problems": problems,
           "written_by": dict(Counter((s.get("memo") or {}).get("written_by") for s in results.values()
                                      if s.get("memo")))}
    if judge:
        valid = [s for s in scores if s is not None]
        out["judge_mean_score"] = round(statistics.mean(valid), 2) if valid else None
    return out


JUDGE_PROMPT = """You check a bank credit memo against the facts it was written from.
Score faithfulness from 1 (invents or contradicts facts) to 5 (every statement is supported by the facts).
Reply with JSON: {"score": <1-5>, "unsupported": ["each claim not supported by the facts"]}"""


def _judge(state: dict, memo: str) -> float | None:
    from copilot.llm import call_llm, parse_json
    facts = {k: state.get(k) for k in ["slip", "report", "flags", "policy", "risk_tier"]}
    try:
        answer = call_llm(JUDGE_PROMPT, json.dumps({"facts": facts, "memo": memo}, default=str), json_mode=True,
                          purpose="judge memo", pii_names=[(state.get("slip") or {}).get("name")])
        return float(parse_json(answer)["score"]) if answer else None
    except Exception:
        return None


def eval_guardrails() -> dict:
    from copilot.agents import guardrails_agent
    cases = _load("guardrail_cases.json")["cases"]
    misses = [f"{c['request']!r}: expected {c['expect']}"
              for c in cases if guardrails_agent(c["request"])["blocked"] != (c["expect"] == "blocked")]
    return {"cases": len(cases), "accuracy": (len(cases) - len(misses)) / len(cases), "misses": misses}


def eval_performance(results: dict) -> dict:
    from copilot.telemetry import summarize
    runs = [summarize(s.get("metrics")) for s in results.values()]
    times = sorted(r["total_ms"] for r in runs)
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    slowest = Counter()
    for r in runs:
        for agent, ms in r["per_agent_ms"].items():
            slowest[agent] += ms
    return {"cases": len(runs), "p50_ms": statistics.median(times), "p95_ms": p95,
            "mean_llm_calls": statistics.mean(r["llm_calls"] for r in runs),
            "mean_tokens": statistics.mean(r["input_tokens"] + r["output_tokens"] for r in runs),
            "mean_cost_usd": round(statistics.mean(costs), 6) if costs else 0.0,
            "time_by_agent_ms": {a: round(ms / len(runs), 1) for a, ms in slowest.most_common()}}


# ---------- report ----------

def check(report: dict, limits: dict) -> list[str]:
    got = {"extraction_field_accuracy": report["extraction"]["field_accuracy"],
           "tier_accuracy": report["tiers"]["accuracy"], "fraud_recall": report["fraud"]["recall"],
           "memo_pass_rate": report["memos"]["pass_rate"], "guardrail_accuracy": report["guardrails"]["accuracy"]}
    failures = [f"{k} {v:.1%} is under {limits[k]:.0%}" for k, v in got.items() if v < limits[k]]
    if len(report["fraud"]["false_alarms"]) > limits["false_fraud_alarms_max"]:
        failures.append(f"{len(report['fraud']['false_alarms'])} false fraud alarms "
                        f"(max {limits['false_fraud_alarms_max']})")
    if report["performance"]["p95_ms"] > limits["p95_case_ms_max"]:
        failures.append(f"p95 case time {report['performance']['p95_ms']:.0f} ms is over "
                        f"{limits['p95_case_ms_max']} ms")
    return failures


def to_markdown(r: dict, failures: list[str]) -> str:
    e, t, f, m, g, p = (r[k] for k in ["extraction", "tiers", "fraud", "memos", "guardrails", "performance"])
    mode = "GPT" if r["gpt"] else "plain code (no OPENAI_API_KEY)"
    lines = [f"# Evaluation report ({mode})", "",
             "| Check | Score | Detail |", "| --- | --- | --- |",
             f"| Payslip extraction | {e['field_accuracy']:.1%} | {e['applicants']} payslips x {len(e['per_field'])} "
             f"fields, read by {e['read_by']} |",
             f"| Risk tier | {t['accuracy']:.1%} | {t['cases']} applicants ({t['hand_labelled']} hand-labelled) |",
             f"| Fraud recall | {f['recall']:.0%} | {f['planted']} planted cases, "
             f"{len(f['false_alarms'])} false alarms |",
             f"| Memo rules | {m['pass_rate']:.1%} | {m['memos']} memos, written by {m['written_by']}"
             + (f", judge {m['judge_mean_score']}/5" if m.get("judge_mean_score") else "") + " |",
             f"| Guardrails | {g['accuracy']:.0%} | {g['cases']} requests incl. bypass attempts |",
             f"| Time per case | p50 {p['p50_ms']:.0f} ms, p95 {p['p95_ms']:.0f} ms | machine time, no human wait |",
             f"| GPT per case | {p['mean_llm_calls']:.1f} calls, {p['mean_tokens']:.0f} tokens | "
             f"${p['mean_cost_usd']:.5f} per case |", "",
             "**Result: " + ("PASS**" if not failures else "FAIL**: " + "; ".join(failures)), ""]
    lines += ["Time by agent (mean ms): " + ", ".join(f"{a} {ms}" for a, ms in p["time_by_agent_ms"].items()), ""]
    for title, items in [("Extraction misses", e["misses"]), ("Tier misses", t["misses"]),
                         ("Fraud missed", f["missed"]), ("False fraud alarms", f["false_alarms"]),
                         ("Memo problems", m["problems"]), ("Guardrail misses", g["misses"])]:
        if items:
            lines += [f"**{title}**", ""] + [f"- {i}" for i in items[:20]] + [""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ci", action="store_true", help="exit 1 if a score is under evals/thresholds.json")
    ap.add_argument("--out", help="write the markdown report to this file")
    ap.add_argument("--json", help="write the raw results to this file")
    ap.add_argument("--judge", action="store_true", help="GPT grades each memo (needs OPENAI_API_KEY)")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    data_dir = start_environment()
    try:
        labels = _load("tier_labels.json")
        results = asyncio.run(run_cases(sorted(labels["applicants"])))
        report = {"gpt": bool(os.environ.get("OPENAI_API_KEY")), "extraction": eval_extraction(),
                  "tiers": eval_tiers(results, labels["applicants"]), "fraud": eval_fraud(results, labels["planted"]),
                  "memos": eval_memos(results, args.judge and bool(os.environ.get("OPENAI_API_KEY"))),
                  "guardrails": eval_guardrails(), "performance": eval_performance(results)}
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    failures = check(report, _load("thresholds.json"))
    text = to_markdown(report, failures)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return 1 if (args.ci and failures) else 0


if __name__ == "__main__":
    sys.exit(main())

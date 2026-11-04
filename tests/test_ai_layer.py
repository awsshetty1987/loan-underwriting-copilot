"""The AI engineering layer: PII masking, GPT call records (tokens, cost), run metrics, rules version, tracing, evals."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from copilot import llm, pii, telemetry, tools
from copilot.api import app

KEY = {"x-api-key": "test-api-key"}
EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def capture_spans():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(provider)       # the copilot's tracer is a proxy, so it uses this from now on
    yield


# ---------- PII masking ----------

def test_mask_and_unmask_round_trip():
    text = "Employee Name: Arjun Rao\nPAN: ARJPR4821K\nAadhaar: XXXX XXXX 1234\nMobile 9876543210, a@b.in"
    masked, mapping = pii.mask(text, names=["Arjun Rao"])
    for secret in ["Arjun Rao", "ARJPR4821K", "1234", "9876543210", "a@b.in"]:
        assert secret not in masked
    assert "[NAME_1]" in masked and "[PAN_1]" in masked
    assert pii.unmask(masked, mapping) == text


def test_mask_leaves_amounts_and_dates_alone():
    text = "Gross Pay: 1,20,000.00\nNet Pay: 95600\nPay Date: 2026-06-26\nLoan 1575000 over 36 months"
    masked, mapping = pii.mask(text)
    assert masked == text and mapping == {}


# ---------- GPT calls: masked, timed, priced ----------

class FakeOpenAI:
    """Answers like OpenAI and remembers what it was sent."""
    def __init__(self, answer):
        self.sent, self.answer = [], answer
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(model=kwargs["model"], usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200),
                               choices=[SimpleNamespace(message=SimpleNamespace(content=self.answer))])


def test_gpt_never_sees_pii_and_each_call_is_recorded(monkeypatch):
    fake = FakeOpenAI('{"pan": "[PAN_1]", "name": "[NAME_1]", "net_pay": 50000}')
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(llm, "_client", fake)
    with telemetry.collect_llm_calls() as calls:
        answer = llm.call_llm("system", "Employee Name: Arjun Rao\nPAN: ARJPR4821K", json_mode=True,
                              purpose="read payslip", pii_names=["Arjun Rao"])
    prompt = fake.sent[0]["messages"][1]["content"]
    assert "ARJPR4821K" not in prompt and "Arjun Rao" not in prompt
    assert json.loads(answer) == {"pan": "ARJPR4821K", "name": "Arjun Rao", "net_pay": 50000}
    (call,) = calls
    assert call["input_tokens"] == 1000 and call["output_tokens"] == 200 and call["pii_masked"] == 2
    assert call["cost_usd"] == pytest.approx((1000 * 0.15 + 200 * 0.60) / 1_000_000)


def test_masking_can_be_turned_off(monkeypatch):
    fake = FakeOpenAI("ok")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("PII_MASKING", "false")
    monkeypatch.setattr(llm, "_client", fake)
    llm.call_llm("system", "PAN: ARJPR4821K")
    assert "ARJPR4821K" in fake.sent[0]["messages"][1]["content"]


def test_price_override_and_unknown_model(monkeypatch):
    assert telemetry.cost_usd("some-other-model", 10, 10) is None
    monkeypatch.setenv("OPENAI_PRICE_PER_1M", "1,2")
    assert telemetry.cost_usd("some-other-model", 1_000_000, 1_000_000) == 3.0


# ---------- a whole case: metrics, rules version, spans ----------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_case_carries_run_metrics_and_rules_version(client):
    EXPORTER.clear()
    case = client.post("/underwrite", json={"request": "Underwrite applicant APP_1008"}, headers=KEY).json()
    m = case["run_metrics"]
    assert set(m["per_agent_ms"]) == {"guardrails", "documents", "bureau", "consistency", "policy", "risk", "memo"}
    assert m["total_ms"] > 0 and m["llm_calls"] == 0 and m["cost_usd"] == 0.0      # no OpenAI key in tests
    assert case["rules_version"].startswith("sha256:")
    assert case["rules_version"] == tools.get_policy_rules()["rules_version"]

    names = [s.name for s in EXPORTER.get_finished_spans()]
    assert "underwrite case" in names and "agent documents" in names and "tool read_salary_slip" in names
    root = next(s for s in EXPORTER.get_finished_spans() if s.name == "underwrite case")
    agent = next(s for s in EXPORTER.get_finished_spans() if s.name == "agent policy")
    assert agent.context.trace_id == root.context.trace_id                     # one trace per case

    r = client.post(f"/cases/{case['case_id']}/decision", headers=KEY,
                    json={"decision": "approve", "underwriter": "UW-001", "comment": "ok"})
    assert r.status_code == 200
    record = client.get("/audit?applicant_id=APP_1008", headers=KEY).json()["entries"][0]["details"]
    assert record["rules_version"] == case["rules_version"]
    assert record["run_metrics"]["per_agent_ms"]["memo"] >= 0 and record["run_metrics"]["total_ms"] > 0


def test_rules_version_changes_with_the_rules(offline_env):
    before = tools.get_policy_rules()["rules_version"]
    path = offline_env / "loan-policy" / "rules.json"
    original = path.read_text()
    try:
        path.write_text(original.replace('"min_credit_score": 650', '"min_credit_score": 660'))
        assert tools.get_policy_rules()["rules_version"] != before
    finally:
        path.write_text(original)


# ---------- the eval checks themselves ----------

def test_memo_grounding_catches_an_invented_number():
    from evals.run import eval_memos
    state = {"applicant_id": "APP_1", "risk_tier": "LOW", "slip": {"net_pay": 95600}, "report": {"credit_score": 800},
             "flags": [], "policy": {}}
    good = "- Net pay Rs.95,600, score 800\n- Risk tier: LOW\nDecision: pending human underwriter sign-off"
    bad = "- Net pay Rs.1,95,600, score 800, approved\n- Risk tier: LOW\nDecision: pending human underwriter sign-off"
    out = eval_memos({"A": {**state, "memo": {"memo": good, "written_by": "x"}},
                      "B": {**state, "memo": {"memo": bad, "written_by": "x"}}}, judge=False)
    assert out["pass_rate"] == 0.5
    assert out["problems"][0].startswith("B: no_decision_words, numbers_grounded")

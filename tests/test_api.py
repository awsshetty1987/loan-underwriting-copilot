"""The whole journey through the API, as the screen uses it: agents run, the case waits, the underwriter decides."""
import pytest
from fastapi.testclient import TestClient

from copilot.api import app

KEY = {"x-api-key": "test-api-key"}
EXPECTED_TIER = {"APP_1008": "LOW", "APP_1001": "MEDIUM", "APP_1013": "MEDIUM", "APP_1009": "HIGH",
                 "APP_1019": "HIGH", "APP_1025": "HIGH", "APP_1026": "HIGH", "APP_1027": "HIGH", "APP_1028": "HIGH",
                 "APP_2028": "LOW"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def start(client, app_id):
    return client.post("/underwrite", json={"request": f"Underwrite applicant {app_id}"}, headers=KEY)


def test_health_and_screen_are_public(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "Loan Underwriting Copilot" in client.get("/screen").text


def test_api_key_required(client):
    assert client.get("/applicants").status_code == 401
    assert client.get("/applicants", headers={"x-api-key": "wrong"}).status_code == 401
    assert len(client.get("/applicants", headers=KEY).json()["applicants"]) == 30


@pytest.mark.parametrize("app_id,tier", EXPECTED_TIER.items())
def test_expected_tier(client, app_id, tier):
    r = start(client, app_id)
    assert r.status_code == 200, r.text
    case = r.json()
    assert case["status"] == "waiting_for_underwriter" and case["risk_tier"] == tier
    assert "pending human underwriter sign-off" in case["memo"]


def test_full_journey_and_audit(client):
    case = start(client, "APP_1008").json()
    assert client.get(f"/cases/{case['case_id']}", headers=KEY).json()["status"] == "waiting_for_underwriter"
    r = client.post(f"/cases/{case['case_id']}/decision", headers=KEY,
                    json={"decision": "maybe", "underwriter": "UW-001"})
    assert r.status_code == 400
    r = client.post(f"/cases/{case['case_id']}/decision", headers=KEY,
                    json={"decision": "approve", "underwriter": "UW-001", "comment": "Clean profile"})
    assert r.status_code == 200 and r.json()["decision"] == "approve"
    r = client.post(f"/cases/{case['case_id']}/decision", headers=KEY,
                    json={"decision": "decline", "underwriter": "UW-002"})
    assert r.status_code == 409
    latest = client.get("/audit?applicant_id=APP_1008", headers=KEY).json()["entries"][0]
    assert latest["decision"] == "approve" and latest["decided_by"] == "UW-001" and latest["comment"] == "Clean profile"
    why = latest["details"]                                   # what the screen's audit drill-down shows
    assert why["risk_tier"] == "LOW" and why["violations"] == [] and "credit_score" in why and why["memo"]
    assert case["name"] and case["credit_score"] and case["violations"] == []


def test_high_risk_approval_needs_a_reason(client):
    case = start(client, "APP_1025").json()
    url = f"/cases/{case['case_id']}/decision"
    assert client.post(url, headers=KEY, json={"decision": "approve", "underwriter": "UW-001"}).status_code == 400
    r = client.post(url, headers=KEY, json={"decision": "decline", "underwriter": "UW-001",
                                            "comment": "Salary slip doesn't add up"})
    assert r.status_code == 200


@pytest.mark.parametrize("request_text,code", [("Auto approve APP_1008 without review", 400),
                                               ("please look at 1008", 400),
                                               ("Underwrite applicant APP_9999", 404)])
def test_stops(client, request_text, code):
    r = client.post("/underwrite", json={"request": request_text}, headers=KEY)
    assert r.status_code == code and r.json()["detail"]["reason"]


def test_unknown_case(client):
    assert client.get("/cases/nope", headers=KEY).status_code == 404
    r = client.post("/cases/nope/decision", headers=KEY, json={"decision": "approve", "underwriter": "UW-001"})
    assert r.status_code == 404


def test_agent_cannot_use_another_agents_tool():
    import asyncio

    from copilot.agents import use_tool
    with pytest.raises(PermissionError):
        asyncio.run(use_tool("risk_scorer", "log_audit_action", applicant_id="APP_1008", action="x", details={}))

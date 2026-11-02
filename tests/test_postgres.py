"""A case waiting for the underwriter survives a restart (needs Postgres: set TEST_DATABASE_URL)."""
import os

import pytest
from fastapi.testclient import TestClient

from copilot.api import app

KEY = {"x-api-key": "test-api-key"}
pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def test_waiting_case_survives_restart(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    with TestClient(app) as before_restart:
        case = before_restart.post("/underwrite", json={"request": "Underwrite applicant APP_1001"}, headers=KEY).json()
        assert case["status"] == "waiting_for_underwriter"
    with TestClient(app) as after_restart:                 # new process state, same database
        found = after_restart.get(f"/cases/{case['case_id']}", headers=KEY).json()
        assert found["status"] == "waiting_for_underwriter" and found["risk_tier"] == "MEDIUM"
        r = after_restart.post(f"/cases/{case['case_id']}/decision", headers=KEY,
                               json={"decision": "refer", "underwriter": "UW-002", "comment": "Ask for Form 16"})
        assert r.status_code == 200 and r.json()["status"] == "done"

"""Stand-in for a real credit bureau (CIBIL etc.) until there is a lender partner.
Answers POST /credit-report {pan, name} from the credit-bureau/report.json files, keyed by PAN.
Run: uvicorn copilot.bureau_mock:app --port 9000"""
import json

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import storage
from .config import settings

app = FastAPI(title="Mock credit bureau")


class ReportRequest(BaseModel):
    pan: str
    name: str | None = None


def reports_by_pan() -> dict:
    found = {}
    for key in storage.list_keys("loan-docs/"):
        if key.endswith("/credit-bureau/report.json"):
            report = json.loads(storage.get_text(key))
            found[report["pan"]] = report
    return found


@app.post("/credit-report")
def credit_report(body: ReportRequest, x_api_key: str = Header(default="")):
    expected = settings().bureau_api_key
    if not expected or x_api_key != expected:
        raise HTTPException(401, detail="Invalid bureau API key")
    report = reports_by_pan().get(body.pan.upper())
    if report is None:
        raise HTTPException(404, detail="No report for this PAN")
    return report


@app.get("/health")
def health():
    return {"status": "ok"}

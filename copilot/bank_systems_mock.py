"""Stand-in for the two bank systems the copilot calls live, until the real ones are connected:

    POST /credit-report          {pan, name}  -> credit bureau report (CIBIL etc.)
    POST /core-banking/history   {pan}        -> the customer's history with our own bank (Finacle etc.)

Both answer from the JSON files in S3 (credit-bureau/report.json, core-banking/history.json), looked up by PAN.
In PROD only CREDIT_BUREAU_URL and CORE_BANKING_URL (and their keys) change; the copilot code stays the same.
Run: uvicorn copilot.bank_systems_mock:app --port 9000"""
import json

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import storage
from .config import settings

app = FastAPI(title="Mock bank systems: credit bureau and core banking")


class PanRequest(BaseModel):
    pan: str
    name: str | None = None


def records_by_pan(suffix: str) -> dict:
    found = {}
    for key in storage.list_keys("loan-docs/"):
        if key.endswith(suffix):
            record = json.loads(storage.get_text(key))
            if record.get("pan"):
                found[record["pan"].upper()] = record
    return found


def check_key(given: str, expected: str, system: str):
    if not expected or given != expected:
        raise HTTPException(401, detail=f"Invalid {system} API key")


@app.post("/credit-report")
def credit_report(body: PanRequest, x_api_key: str = Header(default="")):
    check_key(x_api_key, settings().bureau_api_key, "bureau")
    report = records_by_pan("/credit-bureau/report.json").get(body.pan.upper())
    if report is None:
        raise HTTPException(404, detail="No report for this PAN")
    return report


@app.post("/core-banking/history")
def core_banking_history(body: PanRequest, x_api_key: str = Header(default="")):
    """404 = not a customer of our bank (new to bank)."""
    check_key(x_api_key, settings().core_banking_api_key, "core banking")
    history = records_by_pan("/core-banking/history.json").get(body.pan.upper())
    if history is None:
        raise HTTPException(404, detail="No customer with this PAN")
    return history


@app.get("/health")
def health():
    return {"status": "ok"}

"""The API and the underwriter screen. Run: uvicorn copilot.api:app --port 8000
Every route except /health and /screen needs the header  x-api-key: <one of API_KEYS>."""
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import telemetry, tools
from .agents import tool_client
from .config import settings
from .workflow import DECISIONS, CaseConflict, CaseNotFound, CaseService

log = logging.getLogger(__name__)
STOP_CODES = {"blocked": 400, "missing_documents": 404, "identity_mismatch": 422}
SCREEN = Path(__file__).parent / "static" / "screen.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry.setup_tracing()
    url = settings().database_url
    if url:                                            # PROD: paused cases survive restarts and deploys
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()
            app.state.cases = CaseService(saver)
            yield
    else:                                              # local / tests: cases live in memory
        from langgraph.checkpoint.memory import MemorySaver
        log.warning("DATABASE_URL not set: cases are kept in memory and lost on restart")
        app.state.cases = CaseService(MemorySaver())
        yield
    await tool_client.close()


app = FastAPI(title="Loan Underwriting Copilot", lifespan=lifespan)
if settings().cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings().cors_origins,
                       allow_methods=["GET", "POST"], allow_headers=["content-type", "x-api-key"])


def require_api_key(x_api_key: str = Header(default="")):
    keys = settings().api_keys
    if not keys:
        raise HTTPException(503, detail="API_KEYS is not configured on the server")
    if not any(hmac.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(401, detail="Missing or wrong API key")


class UnderwriteRequest(BaseModel):
    request: str = Field(max_length=200, examples=["Underwrite applicant APP_1008"])


class DecisionRequest(BaseModel):
    decision: str = Field(examples=["approve"])          # approve / decline / refer
    underwriter: str = Field(max_length=100)
    comment: str = Field(default="", max_length=2000)


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/screen")


@app.get("/screen", include_in_schema=False)
def screen():
    return HTMLResponse(SCREEN.read_text(encoding="utf-8"))


@app.post("/underwrite", dependencies=[Depends(require_api_key)])
async def underwrite(body: UnderwriteRequest):
    """Runs the agents. Returns a case waiting for the underwriter, or an error saying why it stopped."""
    case = await app.state.cases.start(body.request)
    if case["status"] in STOP_CODES:
        raise HTTPException(STOP_CODES[case["status"]], detail={"status": case["status"], "reason": case["reason"]})
    return case


@app.get("/cases/{case_id}", dependencies=[Depends(require_api_key)])
async def get_case(case_id: str):
    try:
        return await app.state.cases.get(case_id)
    except CaseNotFound:
        raise HTTPException(404, detail="Unknown case id") from None


@app.post("/cases/{case_id}/decision", dependencies=[Depends(require_api_key)])
async def decide(case_id: str, body: DecisionRequest):
    """The underwriter's sign-off. 404 unknown case, 409 already decided, 400 invalid decision."""
    try:
        return await app.state.cases.submit_decision(case_id, body.decision, body.underwriter, body.comment)
    except CaseNotFound:
        raise HTTPException(404, detail="Unknown case id") from None
    except CaseConflict as e:
        raise HTTPException(409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from None


@app.get("/applicants", dependencies=[Depends(require_api_key)])
def applicants():
    """Applicant IDs = the loan-docs/APP_1234 folders that hold at least one document."""
    return {"applicants": tools.list_applicants()}


def _status(decision) -> str:
    if decision in DECISIONS:
        return "Decided"
    if str(decision).startswith("stopped"):
        return "Stopped"
    return "Agents only (no sign-off)"


@app.get("/audit", dependencies=[Depends(require_api_key)])
def audit(applicant_id: str = ""):
    """Every audit entry (or one applicant's), newest first, in the shape the screen's table uses."""
    try:
        raw = tools.read_audit_entries(applicant_id or None)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from None
    entries = []
    for e in raw:
        d = e.get("details", {})
        entries.append({"time": e.get("timestamp", "")[:19].replace("T", " "), "applicant_id": e.get("applicant_id"),
                        "risk_tier": d.get("risk_tier"), "status": _status(d.get("decision")),
                        "decision": d.get("decision") if d.get("decision") in DECISIONS else None,
                        "decided_by": d.get("decided_by"), "comment": d.get("comment") or d.get("reason"),
                        "timestamp": e.get("timestamp"), "details": d})          # details: everything, for the drill-down
    return {"entries": entries}

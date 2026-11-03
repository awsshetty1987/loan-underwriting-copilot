# Loan Underwriting Copilot

Agents prepare a loan case; a human underwriter decides.

An underwriter picks an applicant. Eight agents read the payslip, bank statement and KYC, check identity, pull the credit report, run the consistency, document and affordability checks, set a risk tier (LOW / MEDIUM / HIGH) and write a memo. The case then **waits for a human**: Approve, Decline or Refer. Every step is written to an audit trail.

This repo is the production version of the Colab notebooks NB01 to NB04.

## How it fits together

```
Underwriter screen ──► FastAPI (x-api-key) ──► LangGraph workflow ──► MCP tool server ──► S3 / mock bureau / rules
                                               8 agents, pauses for      10 tools, each agent
                                               human sign-off            may call only its own
                                               (Postgres keeps cases)
```

| Folder / file | What it is |
|---|---|
| `copilot/tools.py` | The checks in plain Python: payslip reader, salary credits vs net pay, slip arithmetic, statement balances, file reuse, FOIR and largest affordable loan, eligibility, risk tier, audit |
| `copilot/mcp_server.py` | MCP server exposing the 10 tools. One process, shared by every case |
| `copilot/agents.py` | The 8 agents and the per-agent tool allow-list. GPT is used only to read the payslip and write the memo; both fall back to plain code |
| `copilot/workflow.py` | LangGraph graph, the human sign-off pause, and `CaseService` (start, look up, decide) |
| `copilot/api.py` | REST API and the underwriter screen (`/screen`) |
| `copilot/bureau_mock.py` | Stand-in credit bureau until there is a lender partner |
| `copilot/storage.py` | Same keys on S3 (PROD) or a local folder (development, tests) |
| `sample_data/` | 29 synthetic applicants (APP_1000 to APP_1028, 4 planted fraud cases) and `rules.json` |
| `infra/` | ECS task definition and least-privilege IAM policy |

## What changed from the notebooks

| Notebook behaviour | Here |
|---|---|
| A paused case is lost when Colab restarts | Kept in **Postgres** (`DATABASE_URL`) and picked up after a restart or deploy |
| Anyone with the link can use the API | Every call needs an **API key** (`x-api-key`) |
| A new MCP server process for every tool call (slow) | **One shared MCP server**; a full case runs in about a second without GPT |
| One `log.json` per applicant, rewritten on each event | **One new audit object per event**, never rewritten (old `log.json` files are still read) |
| Keys typed into cells or Colab Secrets | Environment variables locally, **Secrets Manager** on AWS |
| Notebook cells | A package, 36 automated tests, Docker image, CI |

The rules, checks, agents, tiers and screen are the same as NB03_v2 + NB04.

## Run it on your laptop

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q                                              # 38 tests, no AWS or OpenAI needed
```

To use the screen, start the bureau and the API in two terminals:

```bash
cp -r sample_data local_data
export STORAGE_BACKEND=local LOCAL_DATA_DIR=local_data API_KEYS=dev-key CREDIT_BUREAU_API_KEY=dev-bureau
uvicorn copilot.bureau_mock:app --port 9000          # terminal 1
uvicorn copilot.api:app --port 8000                  # terminal 2
```

Open http://localhost:8000/screen and enter `dev-key` when asked. Add `OPENAI_API_KEY` to have GPT read payslips and write memos.

Or run everything, including Postgres, with Docker:

```bash
cp .env.example .env          # set API_KEYS and CREDIT_BUREAU_API_KEY
cp -r sample_data local_data
docker compose up --build     # then open http://localhost:8000/screen
```

## API

| Method and path | What it does |
|---|---|
| `POST /underwrite` `{"request": "Underwrite applicant APP_1008"}` | Runs the agents; returns a case waiting for the underwriter. 400 blocked request, 404 documents missing, 422 PAN mismatch |
| `GET /cases/{case_id}` | Where the case is now |
| `POST /cases/{case_id}/decision` `{"decision", "underwriter", "comment"}` | approve / decline / refer. Approving HIGH needs a comment. 409 if already decided |
| `GET /applicants` | Applicant folders that hold documents |
| `GET /audit?applicant_id=` | Audit entries, newest first |
| `GET /screen`, `GET /health` | Underwriter screen and health check (no key needed) |

Interactive docs: `/docs`.

## Deploy to AWS

For a demo: `python scripts/demo.py up`, then `python scripts/demo.py down` (about $0.05 an hour; it stops itself after 60 minutes). Full details and the all-day setup: [docs/DEPLOY.md](docs/DEPLOY.md).

## Known limits (next phases)

- Documents are text files. Phase 2 reads real PDFs and photos with Textract or GPT vision into the same fields, so the checks don't change.
- The checks prove documents agree with each other, not that they are genuine. Real proof needs Account Aggregator, PAN verification, Form 26AS / EPFO and PDF tamper checks.
- The underwriter ID is typed in. Next: single sign-on, and a second approver above a loan amount.
- The file-reuse check reads every applicant's files; a fingerprint index will be needed beyond a few thousand applicants.
- The age eligibility rule is not built yet.

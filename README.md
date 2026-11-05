# Loan Underwriting Copilot

Agents prepare a loan case; a human underwriter decides.

An underwriter picks an applicant. Eight agents read the payslip, bank statement and KYC (PDFs), check identity, pull the credit report, run the consistency, document and affordability checks, set a risk tier (LOW / MEDIUM / HIGH) and write a memo. The case then **waits for a human**: Approve, Decline or Refer. Every step is written to an audit trail in S3.

## Run a demo on AWS

The S3 bucket stays. Everything else is created for the demo and destroyed after it, about **$0.10 per hour**.

You need: the AWS CLI logged in to your account, [Terraform](https://developer.hashicorp.com/terraform/install), and Docker Desktop running.

**Once: put the test data in S3**

```bash
aws s3 sync sample_data s3://loan-bucket2026/
```

**Once, optional: store your OpenAI key in Secrets Manager**, so GPT reads payslips and writes memos. Without it, plain code does both.
In the AWS console (region eu-north-1): Secrets Manager → Store a new secret → Other type of secret → **Plaintext** → paste only the key → name it `loan-copilot/openai`.
Terraform only reads this secret and never deletes it. It costs about $0.40 a month.

**Once, optional: store a LangSmith key** the same way, named `loan-copilot/langsmith`, to see every case as a trace in [LangSmith](https://smith.langchain.com) (project `loan-copilot`). LangSmith is an outside service: it receives the case data (GPT prompts are masked, see below).

**Each demo: bring it up** (takes about 5 minutes the first time)

```bash
cd terraform
terraform init                 # first time only
terraform apply                # type yes
terraform output -raw api_key  # the screen asks for this key
```

Open the `screen_url` it prints. Only your own IP address can open it.

Nothing to type in: the app's own keys are made fresh in Secrets Manager on every apply, and your OpenAI key is picked up from `loan-copilot/openai` if it exists.

**After the demo: destroy it**

```bash
terraform destroy              # type yes. Removes everything except the S3 bucket.
```

## Tracing, cost and safety around GPT

- **Traces in AWS X-Ray.** Every case is one trace: a span per agent, per tool call and per GPT call, with tokens and cost. Open the AWS console → CloudWatch → X-Ray traces → Traces (region eu-north-1) and filter with `annotation.copilot_applicant_id = "APP_1008"` or `annotation.copilot_risk_tier = "HIGH"`. A small ADOT collector container next to the app forwards them; `terraform apply -var tracing=false` turns it off. Locally, set `OTEL_EXPORTER_OTLP_ENDPOINT` to any OpenTelemetry collector.
- **Readable logs.** CloudWatch → Log groups → `/ecs/loan-copilot-demo`, stream `api/...` (or `aws logs tail /ecs/loan-copilot-demo --log-stream-name-prefix api --follow`). Each case writes one line when it starts (with its X-Ray trace id), one per agent (ok or stopped, time, GPT tokens and cost, flags, broken rules, tier) and one with the result; the underwriter's decision adds two more. `LOG_LEVEL=DEBUG` for more. If traces don't reach X-Ray, the stream `otel/...` says why.
- **LangSmith**, if its key is stored (above): the LangGraph run and each GPT prompt and answer.
- **Time and cost per case.** Each case records how long every agent took and what its GPT calls cost (`run_metrics`). The screen shows it under the memo and in the audit drawer, and it is saved in the audit record. Prices are list prices for the model (see `copilot/telemetry.py`); set `OPENAI_PRICE_PER_1M=0.15,0.60` to use your own.
- **No personal data to GPT.** PAN, Aadhaar, phone numbers, e-mail addresses, account numbers and the applicant's name are replaced by tokens such as `[PAN_1]` before a prompt leaves, and put back in the answer (`copilot/pii.py`). `PII_MASKING=false` turns it off.
- **Rules version on every decision.** The audit record keeps a fingerprint of `rules.json` (`rules_version`), so an auditor can tell which limits were in force.

## Evals

```bash
python -m evals.run            # the report; --ci also fails if a score drops under evals/thresholds.json
python -m evals.run --judge    # with OPENAI_API_KEY set: GPT reads payslips and writes memos, and grades each memo
```

It runs all 30 applicants through the real workflow and scores:

| Check | How |
|---|---|
| Payslip extraction | 8 fields on 29 payslips vs the text the PDFs were made from (`evals/data/extraction_truth.json`) |
| Risk tier | Expected tier per applicant (`evals/data/tier_labels.json`: 10 checked by hand, 20 frozen from today's rules) |
| Fraud | The 4 planted cases must come out HIGH with the right flag; no clean applicant may get a document flag |
| Memo | No "approved/rejected", ends with the sign-off line, names the tier, and every number in it appears in the case facts. With `--judge`, GPT also scores faithfulness 1 to 5 |
| Guardrails | 16 requests, including bypass and injection attempts (`evals/data/guardrail_cases.json`) |
| Time and cost | p50 / p95 machine time per case, GPT calls, tokens and dollars per case |

CI runs it on every pull request without GPT and puts the report on the run's summary page. Run it with a key before changing the model or a prompt, and compare with the last report.

## Test data (`sample_data/`)

30 synthetic applicants: APP_1000 to APP_1028, plus APP_2028 (added later to show how a new customer is added). Four are planted fraud cases: APP_1025 (payslip arithmetic), APP_1026 (statement balances), APP_1027 (statement copied from another applicant), APP_1028 (loan too large for income).

| Folder | Comes from | In PROD |
|---|---|---|
| `loan-docs/APP_xxxx/income/salary_slip.pdf` | Customer | Uploaded by the bank employee |
| `loan-docs/APP_xxxx/bank-statements/statement_6m.pdf` | Customer | Uploaded by the bank employee |
| `loan-docs/APP_xxxx/kyc/id_proof.pdf` | Customer | Uploaded by the bank employee (from DigiLocker or another route the bank allows) |
| `loan-docs/APP_xxxx/application/loan_request.json` | Application form | Saved by the online or branch form |
| `loan-docs/APP_xxxx/credit-bureau/report.json` | Credit bureau | Bureau API, asked live by PAN |
| `loan-docs/APP_xxxx/core-banking/history.json` | Bank's own systems | Core-banking API, asked live by PAN (not found = new to bank) |
| `loan-policy/rules.json` | Credit policy | Same file |

## Run the tests

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q                                              # no AWS or OpenAI needed
```

## What is where

| Path | What it is |
|---|---|
| `terraform/main.tf` | The whole AWS demo setup in one file: image build, ECS task (app, stand-in bank systems, Postgres), load balancer, secrets, IAM |
| `copilot/tools.py` | The checks in plain Python, and the PDF readers |
| `copilot/agents.py` | The 8 agents and which tools each may use |
| `copilot/workflow.py` | The LangGraph workflow and the pause for human sign-off |
| `copilot/api.py` | REST API and the underwriter screen (`/screen`) |
| `copilot/mcp_server.py` | The tool server the agents call |
| `copilot/bank_systems_mock.py` | Stand-in for the bank's systems: credit bureau and core banking, looked up by PAN |
| `copilot/storage.py` | Reads and writes S3 (or a local folder in tests) |
| `copilot/telemetry.py` | Tracing (OpenTelemetry) and time / cost per agent |
| `copilot/pii.py` | Masks personal data before a GPT call |
| `evals/` | The evaluation suite and its labelled data |
| `tests/` | Automated tests; `make_sample_pdfs.py` made the test PDFs |

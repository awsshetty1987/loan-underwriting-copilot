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

**Each demo: bring it up** (takes about 5 minutes the first time)

```bash
cd terraform
terraform init                 # first time only
terraform apply                # type yes
terraform output -raw api_key  # the screen asks for this key
```

Open the `screen_url` it prints. Only your own IP address can open it.

- To let GPT read payslips and write memos, add `-var openai_api_key=sk-...` to `terraform apply`. Without it, plain code does both.
- On Windows, add `-var docker_host=npipe:////./pipe/docker_engine`.

**After the demo: destroy it**

```bash
terraform destroy              # type yes. Removes everything except the S3 bucket.
```

## Test data (`sample_data/`)

29 synthetic applicants, APP_1000 to APP_1028. Four are planted fraud cases: APP_1025 (payslip arithmetic), APP_1026 (statement balances), APP_1027 (statement copied from another applicant), APP_1028 (loan too large for income).

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
| `tests/` | Automated tests; `make_sample_pdfs.py` made the test PDFs |

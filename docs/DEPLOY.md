# Deploying to AWS

There are two ways. Use the **demo** for now; the **full setup** is for when real users need it all day.

## Demo: start it for an hour, then stop it

One script does everything: `scripts/demo.py`. Run it from your laptop.

**You need:** Python with boto3 (`pip install boto3`), AWS credentials for your account (`aws configure`, or the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables, for a user allowed to manage ECS, ECR, IAM roles, Secrets Manager, CloudWatch Logs and EC2 security groups), and **Docker Desktop** running (only to build the image).

```bash
python scripts/demo.py up --openai-key sk-...   # first time: also stores your OpenAI key (optional)
python scripts/demo.py up                       # later demos
python scripts/demo.py key                      # the API key the screen asks for
python scripts/demo.py status                   # is anything running?
python scripts/demo.py down                     # stop it (do this when the demo ends)
```

`up` prints a link like `http://13.48.x.x:8000/screen`. The demo **stops by itself after 60 minutes** (`--minutes 30` to change), so a forgotten demo can't run up a bill.

What it creates the first time, and reuses afterwards: an ECR repository `loan-copilot`, a secret `loan-copilot-demo` (random API key, bureau key and database password, plus your OpenAI key if given), two IAM roles, a log group kept 7 days, an ECS cluster and a security group that opens port 8000 **to your current IP only** (updated on every `up`).

Each demo is one Fargate task (0.5 vCPU, 1 GB) with four containers: the API, the mock bureau, a Postgres for cases waiting for a decision, and the timer. There is no load balancer and no RDS, because both charge even when nothing runs. Documents and the audit log are your existing S3 bucket; cases still waiting when the task stops are gone, decisions already made stay in the audit log.

Approximate cost (list prices, eu-north-1): about **$0.05 per demo hour** plus a few rupees of GPT; **under $1 a month** while stopped (the secret, the stored image, logs). Plain HTTP to one IP address is fine for a demo, not for customer data.

---

## Full setup (all day, real users)

The pilot runs as **one ECS Fargate task with two containers** (the API and the mock bureau, which talk over localhost), behind an Application Load Balancer, with **RDS Postgres** for paused cases and the existing **S3 bucket** for documents and audit records.

Approximate monthly cost for a pilot running 24x7 (list prices, check the AWS pricing calculator for eu-north-1): Fargate 0.5 vCPU / 1 GB about $20 to 40, load balancer about $20, RDS `db.t4g.micro` about $15 to 25, logs, secrets and ECR about $10. Avoid a NAT gateway (about $35 more): put the task in public subnets with a security group that only accepts traffic from the load balancer, or use VPC endpoints.

## One-time setup

1. **ECR repository** `loan-copilot` in eu-north-1.
2. **RDS Postgres 16** (`db.t4g.micro`, encryption on, not publicly accessible). Create database `copilot`.
   `DATABASE_URL` = `postgresql://USER:PASSWORD@HOST:5432/copilot?sslmode=require`
3. **Secrets Manager** secret `loan-copilot` with the keys `API_KEYS`, `OPENAI_API_KEY`, `CREDIT_BUREAU_API_KEY`, `DATABASE_URL`.
   Use new keys here, not ones that have been in a notebook.
4. **IAM roles**
   - `loan-copilot-execution`: the AWS managed `AmazonECSTaskExecutionRolePolicy`, plus `secretsmanager:GetSecretValue` on the `loan-copilot` secret.
   - `loan-copilot-task`: [infra/iam/task-role-policy.json](../infra/iam/task-role-policy.json). It can read documents and rules and can only **add** audit records, nothing else.
5. **CloudWatch log group** `/ecs/loan-copilot`.
6. **ECS cluster**, a service with 1 task from [infra/ecs/task-definition.json](../infra/ecs/task-definition.json) (replace `<ACCOUNT_ID>` and `<TAG>`), and an **ALB** with an HTTPS listener (ACM certificate) forwarding to port 8000, health check path `/health`.
7. **S3 data**: the bucket already holds the NB04 data. To add anything missing from `sample_data/`, run `python scripts/seed_s3.py --bucket loan-bucket2026` (it lists first and never overwrites unless you pass `--overwrite`).

## Each release

```bash
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.eu-north-1.amazonaws.com
docker build -t loan-copilot:<TAG> .
docker tag loan-copilot:<TAG> <ACCOUNT_ID>.dkr.ecr.eu-north-1.amazonaws.com/loan-copilot:<TAG>
docker push <ACCOUNT_ID>.dkr.ecr.eu-north-1.amazonaws.com/loan-copilot:<TAG>
# update <TAG> in the task definition, register it, and update the service
```

Cases waiting for a decision are kept in Postgres, so a deploy does not lose them.

## Before real customer data

- Turn on S3 versioning, and Object Lock on the audit prefix if the regulator asks for write-once records.
- Put the screen behind single sign-on (for example, ALB authentication with Cognito or the bank's identity provider).
- Restrict the load balancer to the bank's network or VPN.

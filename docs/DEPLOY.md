# Deploying to AWS (pilot)

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

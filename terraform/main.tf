# Loan Underwriting Copilot: everything needed for a demo, around your existing S3 bucket.
#   terraform apply     builds the app image on this computer, pushes it, starts the app, prints the link
#   terraform destroy   removes all of it again. The S3 bucket and its documents are only read, never touched.
# Needs: AWS credentials (aws configure), Docker Desktop running. See README.md.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    docker = { source = "kreuzwerker/docker", version = "~> 3.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    http   = { source = "hashicorp/http", version = "~> 3.4" }
  }
}

# ---------------- settings you may change ----------------

variable "region" {
  default = "eu-north-1"
}

variable "bucket" {
  description = "Existing S3 bucket with loan-docs/ and loan-policy/rules.json"
  default     = "loan-bucket2026"
}

variable "openai_secret_name" {
  description = "Optional Secrets Manager secret (plain text) holding your OpenAI key. You create it once; Terraform only reads it and never deletes it. Without it, plain code reads payslips and writes memos."
  default     = "loan-copilot/openai"
}

variable "langsmith_secret_name" {
  description = "Optional Secrets Manager secret (plain text) holding a LangSmith API key. If it exists, every case is also traced in LangSmith (project loan-copilot). Terraform only reads it."
  default     = "loan-copilot/langsmith"
}

variable "langsmith_endpoint" {
  description = "LangSmith API address for your account's region. Look at the address bar when signed in: smith.langchain.com = leave empty (US), eu.smith.langchain.com = https://eu.api.smith.langchain.com, apac.smith.langchain.com = https://apac.api.smith.langchain.com. A wrong region gives 403 Forbidden in the api log."
  default     = ""
}

variable "tracing" {
  description = "Send OpenTelemetry traces (one per case: agents, tools, GPT calls) to AWS X-Ray through an ADOT collector next to the app"
  default     = true
}

variable "allowed_ip" {
  description = "Who may open the app. Empty = this computer's public IP only."
  default     = ""
}

variable "docker_host" {
  description = "Only if Docker is somewhere unusual. Empty = Docker Desktop's default on Windows, Mac and Linux"
  default     = ""
}

locals {
  windows = !startswith(abspath(path.root), "/") # Windows paths start with a drive letter
}

# Your OpenAI key, if you stored it once in Secrets Manager (see README). Found by exact name; absent = no GPT.
data "aws_secretsmanager_secrets" "openai" {
  filter {
    name   = "name"
    values = [var.openai_secret_name]
  }
}

# Same for an optional LangSmith key.
data "aws_secretsmanager_secrets" "langsmith" {
  filter {
    name   = "name"
    values = [var.langsmith_secret_name]
  }
}

locals {
  openai_secret = one([for arn in data.aws_secretsmanager_secrets.openai.arns : arn
  if can(regex(":secret:${var.openai_secret_name}-[A-Za-z0-9]{6}$", arn))])
  langsmith_secret = one([for arn in data.aws_secretsmanager_secrets.langsmith.arns : arn
  if can(regex(":secret:${var.langsmith_secret_name}-[A-Za-z0-9]{6}$", arn))])
}

# ---------------- providers ----------------

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "loan-copilot-demo" } }
}

data "aws_ecr_authorization_token" "ecr" {}

provider "docker" {
  host = var.docker_host != "" ? var.docker_host : (local.windows ? "npipe:////./pipe/docker_engine" : null)
  registry_auth {
    address  = data.aws_ecr_authorization_token.ecr.proxy_endpoint
    username = data.aws_ecr_authorization_token.ecr.user_name
    password = data.aws_ecr_authorization_token.ecr.password
  }
}

locals {
  name  = "loan-copilot-demo"
  root  = "${path.module}/.."
  my_ip = var.allowed_ip != "" ? var.allowed_ip : trimspace(data.http.my_ip.response_body)
  # The image is rebuilt only when the app code changes
  source = sha1(join("", [for f in sort(concat(tolist(fileset(local.root, "copilot/**")), ["Dockerfile", "requirements.txt"])) : filesha1("${local.root}/${f}")]))
}

data "http" "my_ip" {
  url = "https://checkip.amazonaws.com"
}

data "aws_s3_bucket" "docs" {
  bucket = var.bucket
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# ---------------- app image ----------------

resource "aws_ecr_repository" "app" {
  name         = local.name
  force_delete = true
}

resource "docker_image" "app" {
  name = "${aws_ecr_repository.app.repository_url}:${substr(local.source, 0, 12)}"
  build {
    context  = local.root
    platform = "linux/amd64"
  }
  triggers = { source = local.source }
}

resource "docker_registry_image" "app" {
  name          = docker_image.app.name
  keep_remotely = false
}

# ---------------- keys (random, kept in Secrets Manager) ----------------

resource "random_password" "api_key" {
  length  = 32
  special = false
}

resource "random_password" "bureau_key" {
  length  = 32
  special = false
}

resource "random_password" "core_banking_key" {
  length  = 32
  special = false
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = local.name
  recovery_window_in_days = 0 # delete at once on destroy, so the next apply can reuse the name
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    API_KEYS              = random_password.api_key.result
    CREDIT_BUREAU_API_KEY = random_password.bureau_key.result
    CORE_BANKING_API_KEY  = random_password.core_banking_key.result
    POSTGRES_PASSWORD     = random_password.db.result
    DATABASE_URL          = "postgresql://copilot:${random_password.db.result}@127.0.0.1:5432/copilot"
  })
}

# ---------------- permissions ----------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" { # lets ECS pull the image, read the secret, write logs
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secret" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = compact([aws_secretsmanager_secret.app.arn, local.openai_secret, local.langsmith_secret]) }]
  })
}

resource "aws_iam_role" "task" { # what the app itself may do: read documents and rules, add audit records
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task_s3" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:ListBucket", Resource = data.aws_s3_bucket.docs.arn,
      Condition = { StringLike = { "s3:prefix" = ["loan-docs/*", "loan-policy/*"] } } },
      { Effect = "Allow", Action = "s3:GetObject",
      Resource = ["${data.aws_s3_bucket.docs.arn}/loan-docs/*", "${data.aws_s3_bucket.docs.arn}/loan-policy/*"] },
      { Effect = "Allow", Action = "s3:PutObject", Resource = "${data.aws_s3_bucket.docs.arn}/loan-docs/*/audit/*" },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "task_xray" { # the ADOT collector sends traces to X-Ray
  count      = var.tracing ? 1 : 0
  role       = aws_iam_role.task.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess"
}

# ---------------- network: a load balancer open to one IP ----------------

resource "aws_security_group" "lb" {
  name   = "${local.name}-lb"
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["${local.my_ip}/32"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name   = "${local.name}-app"
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.lb.id]
  }
  egress { # pull images, reach S3, OpenAI
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "app" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.lb.id]
  subnets            = data.aws_subnets.default.ids
  idle_timeout       = 300 # a case with GPT can take a minute
}

resource "aws_lb_target_group" "app" {
  name        = local.name
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  health_check {
    path    = "/health"
    matcher = "200"
  }
  deregistration_delay = 5
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ---------------- the app: one Fargate task with 3 containers ----------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7
}

resource "aws_ecs_cluster" "app" {
  name = local.name
}

locals {
  secret  = aws_secretsmanager_secret.app.arn
  storage = [{ name = "STORAGE_BACKEND", value = "s3" }, { name = "S3_BUCKET", value = var.bucket }, { name = "AWS_REGION", value = var.region }]
  logs = { for c in ["db", "bank-systems", "api", "otel"] : c => {
    logDriver = "awslogs"
    options   = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = c }
  } }
  bank_keys = ["CREDIT_BUREAU_API_KEY", "CORE_BANKING_API_KEY"]
  # The collector takes OTLP from the app on 127.0.0.1:4318 and forwards it to X-Ray. Indexed attributes become
  # X-Ray annotations, so traces can be filtered by applicant, tier or case in the console.
  otel_config = yamlencode({
    receivers  = { otlp = { protocols = { http = { endpoint = "127.0.0.1:4318" } } } }
    processors = { batch = {} }
    exporters = { awsxray = { region = var.region, indexed_attributes = [
    "copilot.applicant_id", "copilot.risk_tier", "copilot.case_id", "copilot.agent", "copilot.decision"] } }
    service = { pipelines = { traces = { receivers = ["otlp"], processors = ["batch"], exporters = ["awsxray"] } } }
  })
  tracing_env = var.tracing ? [{ name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://127.0.0.1:4318" },
  { name = "OTEL_SERVICE_NAME", value = "loan-copilot" }, { name = "OTEL_AWS_XRAY_IDS", value = "true" }] : []
  langsmith_env = local.langsmith_secret != null ? concat([{ name = "LANGSMITH_TRACING", value = "true" },
    { name = "LANGSMITH_PROJECT", value = "loan-copilot" }],
  var.langsmith_endpoint != "" ? [{ name = "LANGSMITH_ENDPOINT", value = var.langsmith_endpoint }] : []) : []
  image = "${aws_ecr_repository.app.repository_url}@${docker_registry_image.app.sha256_digest}"
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode(concat([
    { # Postgres for cases waiting for the underwriter (gone on destroy; decisions stay in the S3 audit log)
      name              = "db", image = "public.ecr.aws/docker/library/postgres:16-alpine", essential = true
      memoryReservation = 256
      command           = ["postgres", "-c", "listen_addresses=127.0.0.1"]
      environment       = [{ name = "POSTGRES_USER", value = "copilot" }, { name = "POSTGRES_DB", value = "copilot" }]
      secrets           = [{ name = "POSTGRES_PASSWORD", valueFrom = "${local.secret}:POSTGRES_PASSWORD::" }]
      healthCheck       = { command = ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U copilot"], interval = 5, timeout = 3, retries = 10, startPeriod = 10 }
      logConfiguration  = local.logs["db"]
    },
    { # stand-in for the bank's systems: credit bureau and core banking (in PROD: the real APIs instead)
      name             = "bank-systems", image = local.image, essential = true, memoryReservation = 128
      command          = ["uvicorn", "copilot.bank_systems_mock:app", "--host", "127.0.0.1", "--port", "9000"]
      environment      = local.storage
      secrets          = [for k in local.bank_keys : { name = k, valueFrom = "${local.secret}:${k}::" }]
      logConfiguration = local.logs["bank-systems"]
    },
    { # the API and the underwriter screen
      name         = "api", image = local.image, essential = true, memoryReservation = 512
      portMappings = [{ containerPort = 8000 }]
      environment = concat(local.storage, [{ name = "CREDIT_BUREAU_URL", value = "http://127.0.0.1:9000/credit-report" },
      { name = "CORE_BANKING_URL", value = "http://127.0.0.1:9000/core-banking/history" }], local.tracing_env, local.langsmith_env)
      secrets = concat([for k in concat(["API_KEYS", "DATABASE_URL"], local.bank_keys) : { name = k, valueFrom = "${local.secret}:${k}::" }],
        local.openai_secret != null ? [{ name = "OPENAI_API_KEY", valueFrom = local.openai_secret }] : [],
      local.langsmith_secret != null ? [{ name = "LANGSMITH_API_KEY", valueFrom = local.langsmith_secret }] : [])
      dependsOn        = [{ containerName = "db", condition = "HEALTHY" }, { containerName = "bank-systems", condition = "START" }]
      logConfiguration = local.logs["api"]
    },
    ], var.tracing ? [{ # AWS Distro for OpenTelemetry: forwards the app's traces to X-Ray. Not essential: if it stops, the app keeps working
      name              = "otel", image = "public.ecr.aws/aws-observability/aws-otel-collector:v0.50.0", essential = false
      memoryReservation = 64
      environment       = [{ name = "AOT_CONFIG_CONTENT", value = local.otel_config }]
      logConfiguration  = local.logs["otel"]
  }] : []))
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true # to pull images without a NAT gateway; only the load balancer can reach port 8000
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = 8000
  }
  wait_for_steady_state = true
  depends_on            = [aws_lb_listener.http, aws_secretsmanager_secret_version.app, aws_iam_role_policy.execution_secret]
}

# ---------------- what you need after apply ----------------

output "screen_url" {
  value = "http://${aws_lb.app.dns_name}/screen"
}

output "api_key" {
  description = "Paste into the screen when asked. Show it with: terraform output -raw api_key"
  value       = random_password.api_key.result
  sensitive   = true
}

output "open_to" {
  value = "${local.my_ip} only"
}

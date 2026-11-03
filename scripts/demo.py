"""Start and stop a short-lived demo of the Copilot on AWS, at a cost of a few cents per hour.

    python scripts/demo.py up            # build + push the image, start the demo, print the screen link
    python scripts/demo.py status        # is anything running?
    python scripts/demo.py key           # the API key the screen asks for
    python scripts/demo.py down          # stop everything that runs (do this after every demo)

What "up" creates (only once; later runs reuse it). Nothing here costs money while no demo is running,
apart from cents for the secret, the stored image and logs:
  - ECR repository  loan-copilot          (the app image)
  - Secret          loan-copilot-demo     (API key, bureau key, database password, your OpenAI key)
  - IAM roles       loan-copilot-demo-execution, loan-copilot-demo-task (S3: read documents, add audit records only)
  - Log group       /ecs/loan-copilot-demo  (kept 7 days)
  - ECS cluster     loan-copilot-demo
  - Security group  loan-copilot-demo     (port 8000 open to YOUR IP only)

Each demo is ONE Fargate task with 4 containers: the API, the mock bureau, a Postgres for paused cases (gone when the
task stops; the audit log stays in S3), and a timer that stops the whole task after --minutes (default 60), so a
forgotten demo can't run up a bill. There is no load balancer and no RDS (both charge even when idle).
Plain HTTP to your IP only: fine for a demo, not for customer data.

Needs: Python with boto3, AWS credentials for your account (aws configure, or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY),
and Docker Desktop running (only for building the image; or pass --image to use one already pushed).
"""
import argparse
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
NAME = "loan-copilot-demo"
REPO = "loan-copilot"
FAMILY = NAME
PORT = 8000


def say(msg: str):
    print(f"  {msg}", flush=True)


def exists(fn, *codes):
    """Run fn; return None instead of raising for the 'not found' error codes given."""
    try:
        return fn()
    except ClientError as e:
        if e.response["Error"]["Code"] in codes:
            return None
        raise


class Demo:
    def __init__(self, region: str, bucket: str):
        self.region, self.bucket = region, bucket
        session = boto3.session.Session(region_name=region)
        self.sts, self.ecr, self.sm = session.client("sts"), session.client("ecr"), session.client("secretsmanager")
        self.iam, self.logs, self.ecs = session.client("iam"), session.client("logs"), session.client("ecs")
        self.ec2 = session.client("ec2")
        self.account = self.sts.get_caller_identity()["Account"]

    # ---------- one-time pieces (created if missing) ----------

    def repository(self) -> str:
        found = exists(lambda: self.ecr.describe_repositories(repositoryNames=[REPO])["repositories"][0],
                       "RepositoryNotFoundException")
        if not found:
            found = self.ecr.create_repository(repositoryName=REPO, imageScanningConfiguration={"scanOnPush": True})["repository"]
            self.ecr.put_lifecycle_policy(repositoryName=REPO, lifecyclePolicyText=json.dumps({"rules": [{
                "rulePriority": 1, "description": "keep the last 5 images",
                "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 5},
                "action": {"type": "expire"}}]}))
            say(f"created ECR repository {REPO}")
        return found["repositoryUri"]

    def secret_values(self) -> dict:
        return json.loads(self.sm.get_secret_value(SecretId=NAME)["SecretString"])

    def secret(self, openai_key: str | None) -> str:
        found = exists(lambda: self.sm.describe_secret(SecretId=NAME), "ResourceNotFoundException")
        if found:
            if openai_key:
                values = self.secret_values()
                values["OPENAI_API_KEY"] = openai_key
                self.sm.put_secret_value(SecretId=NAME, SecretString=json.dumps(values))
                say("updated the OpenAI key in the secret")
            return found["ARN"]
        password = secrets.token_urlsafe(24)
        values = {"API_KEYS": secrets.token_urlsafe(24), "CREDIT_BUREAU_API_KEY": secrets.token_urlsafe(24),
                  "POSTGRES_PASSWORD": password,
                  "DATABASE_URL": f"postgresql://copilot:{password}@127.0.0.1:5432/copilot",
                  "OPENAI_API_KEY": openai_key or ""}
        arn = self.sm.create_secret(Name=NAME, SecretString=json.dumps(values),
                                    Description="Loan Underwriting Copilot demo keys")["ARN"]
        say(f"created secret {NAME} (new random API key, bureau key and database password)")
        return arn

    def role(self, name: str, policy: dict, managed: str | None = None) -> str:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                        "Principal": {"Service": "ecs-tasks.amazonaws.com"}}]}
        found = exists(lambda: self.iam.get_role(RoleName=name)["Role"], "NoSuchEntity")
        if not found:
            found = self.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]
            say(f"created IAM role {name}")
            time.sleep(10)                                   # new roles take a moment to be usable by ECS
        if managed:
            self.iam.attach_role_policy(RoleName=name, PolicyArn=managed)
        self.iam.put_role_policy(RoleName=name, PolicyName=f"{name}-policy", PolicyDocument=json.dumps(policy))
        return found["Arn"]

    def roles(self, secret_arn: str) -> tuple[str, str]:
        execution = self.role(f"{NAME}-execution", {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arn}]},
            managed="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
        task_policy = json.loads((ROOT / "infra" / "iam" / "task-role-policy.json").read_text())
        task_policy = json.loads(json.dumps(task_policy).replace("loan-bucket2026", self.bucket))
        return execution, self.role(f"{NAME}-task", task_policy)

    def log_group(self) -> str:
        group = f"/ecs/{NAME}"
        if not self.logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]:
            self.logs.create_log_group(logGroupName=group)
            self.logs.put_retention_policy(logGroupName=group, retentionInDays=7)
            say(f"created log group {group}")
        return group

    def cluster(self):
        active = [c for c in self.ecs.describe_clusters(clusters=[NAME])["clusters"] if c["status"] == "ACTIVE"]
        if not active:
            self.ecs.create_cluster(clusterName=NAME)
            say(f"created ECS cluster {NAME}")

    def network(self, my_ip: str) -> tuple[list[str], str]:
        vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            sys.exit("No default VPC in this region. Create one in the VPC console (Actions > Create default VPC).")
        vpc = vpcs[0]["VpcId"]
        subnets = [s["SubnetId"] for s in self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]]
        groups = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [NAME]},
                                                            {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
        if groups:
            sg = groups[0]["GroupId"]
            old = [r for p in groups[0]["IpPermissions"] for r in p.get("IpRanges", [])]
            if old:
                self.ec2.revoke_security_group_ingress(GroupId=sg, IpPermissions=groups[0]["IpPermissions"])
        else:
            sg = self.ec2.create_security_group(GroupName=NAME, VpcId=vpc,
                                                Description="Loan Copilot demo: port 8000 from one IP")["GroupId"]
            say(f"created security group {NAME}")
        self.ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": PORT, "ToPort": PORT,
            "IpRanges": [{"CidrIp": f"{my_ip}/32", "Description": "demo viewer"}]}])
        say(f"port {PORT} open to {my_ip} only")
        return subnets, sg

    # ---------- image ----------

    def build_and_push(self, uri: str) -> str:
        tag = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True).stdout.strip() or time.strftime("%Y%m%d%H%M")
        image = f"{uri}:{tag}"
        if exists(lambda: self.ecr.describe_images(repositoryName=REPO, imageIds=[{"imageTag": tag}]),
                  "ImageNotFoundException"):
            say(f"image {tag} already in ECR, not rebuilding")
            return image
        auth = self.ecr.get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        say("building the image (a few minutes the first time)")
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", image, "."], cwd=ROOT, check=True)
        subprocess.run(["docker", "login", "--username", user, "--password-stdin", auth["proxyEndpoint"]],
                       input=password, text=True, check=True, capture_output=True)
        subprocess.run(["docker", "push", image], check=True)
        return image

    # ---------- task ----------

    def task_definition(self, image: str, secret_arn: str, execution: str, task_role: str, group: str,
                        minutes: int, with_openai: bool) -> str:
        def secret(name):
            return {"name": name, "valueFrom": f"{secret_arn}:{name}::"}

        def logs(prefix):
            return {"logDriver": "awslogs", "options": {"awslogs-group": group, "awslogs-region": self.region,
                                                        "awslogs-stream-prefix": prefix}}
        storage = [{"name": "STORAGE_BACKEND", "value": "s3"}, {"name": "S3_BUCKET", "value": self.bucket},
                   {"name": "AWS_REGION", "value": self.region}]
        containers = [
            {"name": "db", "image": "public.ecr.aws/docker/library/postgres:16-alpine", "essential": True,
             "memoryReservation": 256,
             "command": ["postgres", "-c", "listen_addresses=127.0.0.1"],
             "environment": [{"name": "POSTGRES_USER", "value": "copilot"}, {"name": "POSTGRES_DB", "value": "copilot"}],
             "secrets": [secret("POSTGRES_PASSWORD")],
             "healthCheck": {"command": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U copilot"],
                             "interval": 5, "timeout": 3, "retries": 10, "startPeriod": 10},
             "logConfiguration": logs("db")},
            {"name": "bureau", "image": image, "essential": True, "memoryReservation": 128,
             "command": ["uvicorn", "copilot.bureau_mock:app", "--host", "127.0.0.1", "--port", "9000"],
             "environment": storage, "secrets": [secret("CREDIT_BUREAU_API_KEY")], "logConfiguration": logs("bureau")},
            {"name": "api", "image": image, "essential": True, "memoryReservation": 512,
             "portMappings": [{"containerPort": PORT}],
             "environment": storage + [{"name": "CREDIT_BUREAU_URL", "value": "http://127.0.0.1:9000/credit-report"}],
             "secrets": [secret("API_KEYS"), secret("CREDIT_BUREAU_API_KEY"), secret("DATABASE_URL")]
                        + ([secret("OPENAI_API_KEY")] if with_openai else []),     # no key: plain-code reader and memo
             "dependsOn": [{"containerName": "db", "condition": "HEALTHY"},
                           {"containerName": "bureau", "condition": "START"}],
             "logConfiguration": logs("api")},
            {"name": "timer", "image": image, "essential": True, "memoryReservation": 64,  # when it exits, the whole task stops
             "command": ["python", "-c", f"import time; time.sleep({minutes * 60})"],
             "logConfiguration": logs("timer")},
        ]
        return self.ecs.register_task_definition(
            family=FAMILY, requiresCompatibilities=["FARGATE"], networkMode="awsvpc", cpu="512", memory="1024",
            executionRoleArn=execution, taskRoleArn=task_role, containerDefinitions=containers,
            runtimePlatform={"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"},
        )["taskDefinition"]["taskDefinitionArn"]

    def running(self) -> list[str]:
        if not [c for c in self.ecs.describe_clusters(clusters=[NAME])["clusters"] if c["status"] == "ACTIVE"]:
            return []
        return self.ecs.list_tasks(cluster=NAME, desiredStatus="RUNNING")["taskArns"]

    def public_ip(self, task_arn: str) -> str | None:
        task = self.ecs.describe_tasks(cluster=NAME, tasks=[task_arn])["tasks"][0]
        eni = next((d["value"] for a in task.get("attachments", []) for d in a["details"]
                    if d["name"] == "networkInterfaceId"), None)
        if not eni:
            return None
        nic = self.ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"][0]
        return nic.get("Association", {}).get("PublicIp")


def my_public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


def up(demo: Demo, args):
    if demo.running():
        sys.exit("A demo is already running. Use 'status' to see it, or 'down' to stop it.")
    print(f"Starting the demo in {demo.region} (account {demo.account}), bucket {demo.bucket}")
    uri = demo.repository()
    secret_arn = demo.secret(args.openai_key)
    execution, task_role = demo.roles(secret_arn)
    group = demo.log_group()
    demo.cluster()
    subnets, sg = demo.network(args.my_ip or my_public_ip())
    image = args.image or demo.build_and_push(uri)
    with_openai = bool(demo.secret_values().get("OPENAI_API_KEY"))
    if not with_openai:
        say("no OpenAI key stored: payslips and memos use the plain-code versions (add one with --openai-key)")
    task_def = demo.task_definition(image, secret_arn, execution, task_role, group, args.minutes, with_openai)
    task = demo.ecs.run_task(cluster=NAME, taskDefinition=task_def, launchType="FARGATE", count=1,
                             networkConfiguration={"awsvpcConfiguration": {
                                 "subnets": subnets, "securityGroups": [sg], "assignPublicIp": "ENABLED"}})
    if task.get("failures"):
        sys.exit(f"ECS could not start the task: {task['failures']}")
    arn = task["tasks"][0]["taskArn"]
    say("task started, waiting for it to be ready (usually 1-2 minutes)")
    ip = None
    for _ in range(60):
        ip = ip or demo.public_ip(arn)
        status = demo.ecs.describe_tasks(cluster=NAME, tasks=[arn])["tasks"][0]["lastStatus"]
        if status == "STOPPED":
            sys.exit(f"The task stopped. Look at the logs in CloudWatch: /ecs/{NAME}")
        if ip and status == "RUNNING":
            try:
                urllib.request.urlopen(f"http://{ip}:{PORT}/health", timeout=3)
                break
            except OSError:
                pass
        time.sleep(5)
    print(f"\nReady. Open:  http://{ip}:{PORT}/screen")
    print(f"It stops by itself in {args.minutes} minutes. Stop it sooner with:  python scripts/demo.py down")
    print("The screen asks for an API key. Show it with:  python scripts/demo.py key")


def status(demo: Demo, _args):
    tasks = demo.running()
    if not tasks:
        print("Nothing is running. (No demo costs are being charged.)")
        return
    for arn in tasks:
        t = demo.ecs.describe_tasks(cluster=NAME, tasks=[arn])["tasks"][0]
        print(f"Running since {t.get('startedAt', '?')}: http://{demo.public_ip(arn)}:{PORT}/screen")


def down(demo: Demo, _args):
    tasks = demo.running()
    for arn in tasks:
        demo.ecs.stop_task(cluster=NAME, task=arn, reason="demo.py down")
    print(f"Stopped {len(tasks)} task(s). Nothing is running now." if tasks else "Nothing was running.")


def key(demo: Demo, _args):
    if not exists(lambda: demo.sm.describe_secret(SecretId=NAME), "ResourceNotFoundException"):
        sys.exit("No demo secret yet: run 'up' first.")
    print(demo.secret_values()["API_KEYS"].split(",")[0])


def main():
    p = argparse.ArgumentParser(description="Start or stop a short demo of the Copilot on AWS.")
    p.add_argument("action", choices=["up", "status", "key", "down"])
    p.add_argument("--region", default="eu-north-1")
    p.add_argument("--bucket", default="loan-bucket2026")
    p.add_argument("--minutes", type=int, default=60, help="the demo stops itself after this long (default 60)")
    p.add_argument("--openai-key", help="store/replace your OpenAI key in the secret (optional)")
    p.add_argument("--image", help="use an image already in ECR instead of building one")
    p.add_argument("--my-ip", help="IP allowed to open the screen (default: this computer's public IP)")
    args = p.parse_args()
    if not 5 <= args.minutes <= 240:
        sys.exit("--minutes must be between 5 and 240")
    {"up": up, "status": status, "key": key, "down": down}[args.action](Demo(args.region, args.bucket), args)


if __name__ == "__main__":
    main()

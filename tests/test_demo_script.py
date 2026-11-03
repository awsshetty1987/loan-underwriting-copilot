"""scripts/demo.py against a fake AWS (moto): what 'up' creates, that it reuses it, and that 'down' stops the task."""
import importlib.util
import json
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")


@pytest.fixture
def demo(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "eu-north-1",
                 "MOTO_IAM_LOAD_MANAGED_POLICIES": "true"}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("demo", Path(__file__).parent.parent / "scripts" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: Ok())
    with moto.mock_aws():
        d = module.Demo("eu-north-1", "loan-bucket2026")
        d.public_ip = lambda arn: "203.0.113.7"
        describe = d.ecs.describe_tasks

        def running_while_desired(**kw):              # moto moves tasks on by itself; real ECS stays RUNNING
            r = describe(**kw)
            for t in r["tasks"]:
                if t["desiredStatus"] == "RUNNING":
                    t["lastStatus"] = "RUNNING"
            return r
        d.ecs.describe_tasks = running_while_desired
        yield module, d


class Args:
    openai_key = None
    image = "123456789012.dkr.ecr.eu-north-1.amazonaws.com/loan-copilot:test"
    my_ip = "198.51.100.1"
    minutes = 60


def test_up_status_down(demo, capsys):
    module, d = demo
    module.up(d, Args)
    assert "http://203.0.113.7:8000/screen" in capsys.readouterr().out
    assert len(d.running()) == 1
    with pytest.raises(SystemExit):                       # a second demo is refused while one runs
        module.up(d, Args)

    task_def = d.ecs.describe_task_definition(taskDefinition="loan-copilot-demo")["taskDefinition"]
    containers = {c["name"]: c for c in task_def["containerDefinitions"]}
    assert set(containers) == {"db", "bureau", "api", "timer"}
    assert "time.sleep(3600)" in containers["timer"]["command"][-1]
    assert "OPENAI_API_KEY" not in {s["name"] for s in containers["api"]["secrets"]}   # none stored yet
    sg = d.ec2.describe_security_groups(GroupNames=["loan-copilot-demo"])["SecurityGroups"][0]
    assert [r["CidrIp"] for p in sg["IpPermissions"] for r in p["IpRanges"]] == ["198.51.100.1/32"]

    module.down(d, Args)
    assert d.running() == []


def test_second_up_reuses_everything_and_moves_the_ip(demo, capsys):
    module, d = demo
    module.up(d, Args)
    module.down(d, Args)
    first_key = json.loads(d.sm.get_secret_value(SecretId="loan-copilot-demo")["SecretString"])["API_KEYS"]

    class Later(Args):
        my_ip = "198.51.100.2"
        openai_key = "sk-test"
    capsys.readouterr()
    module.up(d, Later)
    assert "created" not in capsys.readouterr().out          # nothing new was created
    values = json.loads(d.sm.get_secret_value(SecretId="loan-copilot-demo")["SecretString"])
    assert values["API_KEYS"] == first_key and values["OPENAI_API_KEY"] == "sk-test"
    sg = d.ec2.describe_security_groups(GroupNames=["loan-copilot-demo"])["SecurityGroups"][0]
    assert [r["CidrIp"] for p in sg["IpPermissions"] for r in p["IpRanges"]] == ["198.51.100.2/32"]
    task_def = d.ecs.describe_task_definition(taskDefinition="loan-copilot-demo")["taskDefinition"]
    api = next(c for c in task_def["containerDefinitions"] if c["name"] == "api")
    assert "OPENAI_API_KEY" in {s["name"] for s in api["secrets"]}
    module.down(d, Later)

"""The S3 backend reads, lists and writes the same keys as the local folder (moto fakes S3, no AWS account needed)."""
import os

import pytest

moto = pytest.importorskip("moto")


@pytest.fixture
def s3_bucket(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    for k in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
        monkeypatch.setenv(k, "testing")
    with moto.mock_aws():
        import boto3

        from copilot import storage
        monkeypatch.setattr(storage, "_s3", None)
        s3 = boto3.client("s3", region_name="eu-north-1")
        s3.create_bucket(Bucket="test-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        root = os.environ["LOCAL_DATA_DIR"]
        for sub in ["loan-docs/APP_1008", "loan-policy"]:
            for dirpath, _, files in os.walk(os.path.join(root, sub)):
                for name in files:
                    path = os.path.join(dirpath, name)
                    s3.upload_file(path, "test-bucket", os.path.relpath(path, root).replace(os.sep, "/"))
        yield storage
        monkeypatch.setattr(storage, "_s3", None)


def test_s3_backend(s3_bucket):
    from copilot import tools
    assert tools.list_applicants() == ["APP_1008"]
    assert tools.get_policy_rules()["min_credit_score"] == 650
    assert tools.find_missing_documents("APP_1008") == []
    with pytest.raises(FileNotFoundError):
        tools.get_document("APP_1009", "kyc")
    tools.log_audit_action("APP_1008", "test", {"decision": "approve"})
    assert tools.read_audit_entries("APP_1008")[0]["details"]["decision"] == "approve"

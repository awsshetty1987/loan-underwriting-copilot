"""Where documents, rules and audit records live: an S3 bucket in PROD, a local folder for development and tests.
Keys are the same in both, e.g. loan-docs/APP_1001/income/salary_certificate.txt"""
from pathlib import Path

from .config import settings

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=settings().aws_region)
    return _s3


def _local(key: str) -> Path:
    root = Path(settings().local_data_dir).resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError(f"Key outside the data folder: {key}")
    return path


def get_bytes(key: str) -> bytes:
    """The file's content. Raises FileNotFoundError when it doesn't exist."""
    if settings().storage_backend == "local":
        path = _local(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()
    s3 = _client()
    try:
        return s3.get_object(Bucket=settings().s3_bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        raise FileNotFoundError(key) from None


def get_text(key: str) -> str:
    return get_bytes(key).decode("utf-8")


def put_text(key: str, text: str) -> None:
    if settings().storage_backend == "local":
        path = _local(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    _client().put_object(Bucket=settings().s3_bucket, Key=key, Body=text.encode("utf-8"))


def list_keys(prefix: str) -> list[str]:
    """Every key that starts with prefix, sorted."""
    if settings().storage_backend == "local":
        root = Path(settings().local_data_dir).resolve()
        base = _local(prefix) if prefix else root
        folder = base if base.is_dir() else base.parent
        if not folder.exists():
            return []
        keys = (p.relative_to(root).as_posix() for p in folder.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))
    keys = []
    for page in _client().get_paginator("list_objects_v2").paginate(Bucket=settings().s3_bucket, Prefix=prefix):
        keys += [obj["Key"] for obj in page.get("Contents", [])]
    return sorted(keys)

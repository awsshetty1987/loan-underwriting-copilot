"""All settings come from environment variables (on AWS, Terraform fills them from Secrets Manager). No keys live in the code."""
import os
from dataclasses import dataclass, field


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    storage_backend: str = field(default_factory=lambda: os.environ.get("STORAGE_BACKEND", "s3"))   # s3 | local
    local_data_dir: str = field(default_factory=lambda: os.environ.get("LOCAL_DATA_DIR", "sample_data"))
    s3_bucket: str = field(default_factory=lambda: os.environ.get("S3_BUCKET", "loan-bucket2026"))
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "eu-north-1"))
    bureau_url: str = field(default_factory=lambda: os.environ.get("CREDIT_BUREAU_URL", "http://127.0.0.1:9000/credit-report"))
    bureau_api_key: str = field(default_factory=lambda: os.environ.get("CREDIT_BUREAU_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))           # empty = in-memory cases
    api_keys: list[str] = field(default_factory=lambda: _list(os.environ.get("API_KEYS", "")))
    cors_origins: list[str] = field(default_factory=lambda: _list(os.environ.get("CORS_ORIGINS", "")))


def settings() -> Settings:
    """Read fresh each time, so tests and the MCP subprocess see the same environment."""
    return Settings()

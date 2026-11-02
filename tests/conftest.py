"""Tests run fully offline: documents from a copy of sample_data, the mock bureau on a free local port,
no OpenAI key (the plain-text payslip reader and the plain-code memo are used)."""
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session", autouse=True)
def offline_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    shutil.copytree(ROOT / "sample_data", data, dirs_exist_ok=True)
    port = _free_port()
    os.environ.update({"STORAGE_BACKEND": "local", "LOCAL_DATA_DIR": str(data),
                       "CREDIT_BUREAU_URL": f"http://127.0.0.1:{port}/credit-report",
                       "CREDIT_BUREAU_API_KEY": "test-bureau-key", "API_KEYS": "test-api-key"})
    os.environ.pop("OPENAI_API_KEY", None)
    if not os.environ.get("TEST_DATABASE_URL"):
        os.environ.pop("DATABASE_URL", None)
    from copilot.bureau_mock import app as bureau
    server = uvicorn.Server(uvicorn.Config(bureau, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield data
    server.should_exit = True

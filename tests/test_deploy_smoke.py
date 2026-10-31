"""개인판 컨테이너 스모크: build -> up -> /healthz·/api/status -> down -v.
Docker 데몬이 필요해 `YTC_TEST_DOCKER=1`일 때만 돈다(그 밖에는 skip)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.environ.get("YTC_TEST_DOCKER") != "1",
                                reason="YTC_TEST_DOCKER=1일 때만 Docker 스모크를 돈다")


def test_local_stack_smoke() -> None:
    image = f"ytc:smoke-test-{os.getpid()}"
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "deploy" / "smoke.py"), "local",
                               "--project", f"ytc-smoke-test-{os.getpid()}", "--image", image],
                              capture_output=True, text=True, encoding="utf-8", timeout=1500)
    finally:
        subprocess.run(["docker", "rmi", image], capture_output=True)       # 이 테스트가 만든 이미지만 지운다
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]

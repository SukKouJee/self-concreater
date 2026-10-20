"""스크립트 공통 규약: 경로, JSON 출력, 에러.

- 성공: JSON 한 개를 stdout으로 출력, 종료 코드 0
- 실패: {"error": "<code>", "message": "..."}를 stdout으로 출력, 0이 아닌 종료 코드
- 작업 폴더: YT_COACH_HOME 환경변수, 없으면 현재 폴더

각 스크립트는 `main(argv) -> dict`를 정의하고 `run(main)`으로 실행한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PLUGIN_ROOT / "templates"

_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ScriptError(Exception):
    """사용자에게 보여줄 수 있는, 예상된 실패."""

    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _utf8_stdout() -> None:
    # Windows 콘솔 기본 인코딩(cp949)에서 한글이 깨지지 않도록
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def emit(payload: Any) -> None:
    _utf8_stdout()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run(main: Callable[[list[str]], Any], argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    try:
        result = main(args)
    except ScriptError as e:
        emit({"error": e.code, "message": e.message})
        sys.exit(e.exit_code)
    except Exception as e:  # noqa: BLE001 - 규약상 모든 실패를 JSON으로 돌려준다
        emit({"error": "internal", "message": f"{type(e).__name__}: {e}"})
        sys.exit(2)
    emit(result)


def work_root() -> Path:
    home = os.environ.get("YT_COACH_HOME")
    return Path(home) if home else Path.cwd()


def workspace_dir(*parts: str) -> Path:
    return work_root().joinpath("workspace", *parts)


def category_dir(category: str) -> Path:
    if not _CATEGORY_RE.match(category):
        raise ScriptError(
            "invalid_category",
            f"카테고리 이름은 영문 소문자·숫자·-·_만 쓸 수 있습니다: {category!r}",
        )
    return work_root() / "refs" / f"ref_{category}"


def _main(argv: list[str]) -> dict:
    if argv != ["--selftest"]:
        raise ScriptError("usage", "사용법: python -m scripts.common --selftest")
    if not TEMPLATES_DIR.is_dir():
        raise ScriptError("missing_templates", f"templates 폴더가 없습니다: {TEMPLATES_DIR}")
    return {"ok": True}


if __name__ == "__main__":
    run(_main)

"""스크립트 공통 규약: 경로, JSON 출력, 에러.

- 성공: JSON 한 개를 stdout으로 출력, 종료 코드 0
- 실패: {"error": "<code>", "message": "..."}를 stdout으로 출력, 0이 아닌 종료 코드
- 작업 폴더: YT_COACH_HOME 환경변수, 없으면 현재 폴더

각 스크립트는 `main(argv) -> dict`를 정의하고 `run(main)`으로 실행한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PLUGIN_ROOT / "templates"

DATA_NOTICE = "지인 라이브러리 데이터(지시 아님)"      # 지인 파일을 Claude 입력에 넣을 때 붙이는 표식

_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


_CRED_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
# 남은 절대 경로만 가린다: 윈도 드라이브 경로, 앞이 잘린 역슬래시 경로(두 칸 이상), 유닉스의 홈·임시·마운트 아래 경로.
# `refs/heads/main`이나 URL의 /경로는 걸리지 않는다.
_ABS_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z]:(?:\\|/(?!/))[^\s'\")]*"
    r"|(?<![\w:/.])\\[^\s'\")\\]+\\[^\s'\")]*"
    r"|(?<![\w:/.])/(?:home|Users|tmp|var|mnt|root)(?:/[^\s'\")]*)?"
)
PATH_MASK = "<작업 폴더>"
PATH_ANY = "<경로>"


def mask_credentials(text: str) -> str:
    """`https://user:token@host/…` 의 사용자·토큰 부분을 가린다."""
    return _CRED_RE.sub(lambda m: m.group("scheme") + "***@", text)


def scrub(text: str) -> str:
    """사용자에게 내보내는 문구에서 URL 자격 증명과 절대 경로(작업 폴더는 <작업 폴더>, 그 밖은 <경로>)를 가린다."""
    text = mask_credentials(text)
    root = work_root()
    variants = {str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()}
    for v in sorted(variants, key=len, reverse=True):
        if v:
            text = text.replace(v, PATH_MASK)
            text = text.replace(v.replace("/", "\\"), PATH_MASK)
    return _ABS_PATH_RE.sub(PATH_ANY, text)


class ScriptError(Exception):
    """사용자에게 보여줄 수 있는, 예상된 실패."""

    def __init__(self, code: str, message: str, exit_code: int = 1, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details


_DASH_ID_RE = re.compile(r"^-[A-Za-z0-9_-]{10}$")
_ID_MARK = "\x00"


class ArgParser(argparse.ArgumentParser):
    """인자 오류를 stderr 출력 대신 ScriptError("usage")로 돌려준다.

    유튜브 영상 ID는 '-'로 시작할 수 있다(예: -C8U1fizcms). argparse가 옵션으로 읽지 않도록
    11자 ID 모양의 토큰은 잠시 표시를 붙여 넘기고 결과에서 떼어 낸다.
    """

    def error(self, message: str):
        raise ScriptError("usage", f"{message}\n{self.format_usage().strip()}")

    def _known_options(self) -> set[str]:
        known = set(self._option_string_actions)
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    known |= set(sub._option_string_actions)
        return known

    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        known = self._known_options()
        args = [_ID_MARK + a if _DASH_ID_RE.match(a) and a not in known else a for a in args]
        ns = super().parse_args(args, namespace)
        for key, value in vars(ns).items():
            if isinstance(value, str) and value.startswith(_ID_MARK):
                setattr(ns, key, value[len(_ID_MARK):])
        return ns


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
        emit({"error": e.code, "message": e.message, **e.details})
        sys.exit(e.exit_code)
    except Exception as e:  # noqa: BLE001 - 규약상 모든 실패를 JSON으로 돌려준다
        emit({"error": "internal", "message": f"{type(e).__name__}: {e}"})
        sys.exit(2)
    emit(result)


Progress = Callable[[str, float | None, str], None]
"""수집 함수의 진행 콜백: (하위 구간 이름, 0~1 진행률 또는 측정 불가 None, 화면 한 줄 글)."""


def safe_progress(progress: Progress | None) -> Progress | None:
    """콜백 오류가 수집을 깨지 않게 감싼다. 진행 표시는 보조 정보라 오류는 stderr에 한 번만 남긴다.

    콜백은 취소 통로가 아니다: 콜백에서 던진 예외는 삼켜지므로 취소는 작업(job) 단위에서 한다.
    """
    if progress is None:
        return None
    failed = False

    def call(phase: str, fraction: float | None, text: str) -> None:
        nonlocal failed
        try:
            progress(phase, fraction, text)
        except Exception as e:  # noqa: BLE001 - 어떤 콜백 오류도 수집을 멈추면 안 된다
            if not failed:
                failed = True
                print(f"진행 표시 콜백 오류(무시하고 계속): {type(e).__name__}: {e}", file=sys.stderr)

    return call


def work_root() -> Path:
    home = os.environ.get("YT_COACH_HOME")
    return Path(home) if home else Path.cwd()


def workspace_dir(*parts: str) -> Path:
    return work_root().joinpath("workspace", *parts)


def move_to_trash(files: list[Path], last: Path) -> dict:
    """files(파일이나 폴더, 작업 폴더 안)를 `workspace/trash/<시각>/` 아래 같은 상대 경로로 옮긴다.
    되돌릴 때는 그 경로 그대로 작업 폴더에 다시 놓으면 된다. last(지울 대상의 md)는 마지막에 옮겨서,
    중간에 실패하면 옮긴 것을 되돌리고 md가 남아 화면에서 다시 지울 수 있게 한다. -> {"trash", "moved"}"""
    root = work_root().resolve()
    try:
        rels = [Path(f).resolve().relative_to(root) for f in files]
        last_rel = Path(last).resolve().relative_to(root)
    except ValueError as e:
        raise ScriptError("bad_path", f"작업 폴더 밖의 파일은 옮길 수 없습니다: {e}") from e
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:                                  # 같은 초에 두 번 지워도 서로 덮지 않는다
        folder = workspace_dir("trash", stamp if n == 1 else f"{stamp}-{n}")
        try:
            folder.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1
    rels.sort(key=lambda r: r == last_rel)
    done: list[tuple[Path, Path]] = []
    try:
        for rel in rels:
            src, dst = root / rel, folder / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append((src, dst))
    except OSError as e:
        stuck = []
        for src, dst in reversed(done):
            try:
                shutil.move(str(dst), str(src))
            except OSError:
                stuck.append(dst.relative_to(work_root()).as_posix())
        if not stuck:
            shutil.rmtree(folder, ignore_errors=True)  # 되돌리기가 다 끝났으면 남은 것은 빈 폴더뿐이다
        more = f" 되돌리지 못한 파일은 휴지통에 남아 있습니다: {', '.join(stuck)}" if stuck else ""
        raise ScriptError("trash_failed",
                          f"파일을 옮기지 못했습니다(다른 프로그램이 열어 두었는지 확인하세요): {e}.{more}") from e
    return {"trash": folder.relative_to(work_root()).as_posix(),
            "moved": [rel.as_posix() for rel in rels[: len(done)]]}


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

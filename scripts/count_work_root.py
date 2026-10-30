"""`scripts/` 아래에서 작업 폴더 경로를 직접 만드는 호출(`work_root(`·`workspace_dir(`·`category_dir(`)을 모듈별로 센다.

서버판 준비(S3)에서 파일 입출력을 저장소 인터페이스로 옮기며 이 수를 0으로 줄인다. 함수 정의(`def work_root(`)와
이 스크립트 자신은 세지 않는다. 주석·문자열 안의 이름도 함께 세는 단순한 텍스트 측정이다.

    python -m scripts.count_work_root
    -> {"modules": {"scripts/conti.py": {"work_root": 3, "workspace_dir": 5, "category_dir": 2, "all": 10}, ...},
        "total": {"work_root": ..., "workspace_dir": ..., "category_dir": ..., "all": ...}}
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.common import PLUGIN_ROOT, ArgParser, run

NAMES = ("work_root", "workspace_dir", "category_dir")
_CALL_RE = re.compile(r"(?<![\w.])(?<!def )(?:\w+\.)?(" + "|".join(NAMES) + r")\(")


def count_calls(text: str) -> dict[str, int]:
    counts = dict.fromkeys(NAMES, 0)
    for m in _CALL_RE.finditer(text):
        counts[m.group(1)] += 1
    return {**counts, "all": sum(counts.values())}


def main(argv: list[str]) -> dict:
    ArgParser(prog="count_work_root").parse_args(argv)
    scripts_dir = PLUGIN_ROOT / "scripts"
    me = Path(__file__).resolve()
    modules: dict[str, dict[str, int]] = {}
    for path in sorted(scripts_dir.rglob("*.py")):
        if path.resolve() == me:
            continue
        counts = count_calls(path.read_text(encoding="utf-8"))
        if counts["all"]:
            modules[path.relative_to(PLUGIN_ROOT).as_posix()] = counts
    total = {name: sum(c[name] for c in modules.values()) for name in (*NAMES, "all")}
    return {"modules": modules, "total": total}


if __name__ == "__main__":
    run(main)

"""`scripts/` 아래에서 작업 폴더 경로를 직접 만드는 호출을 모듈별로 센다(S3 설계 §5 규칙 1).

세는 이름: `work_root(`·`workspace_dir(`·`category_dir(`·`existing_category_dir(`·`move_to_trash(`. 함수 정의(`def work_root(`)와
이 스크립트 자신은 세지 않는다. 호출을 숨기는 우회 형태(`from … import work_root as w`, `getattr(x, "work_root")`)도 그 이름으로 센다. 주석·문자열 안의 이름도 함께 세는 단순한 텍스트 측정이다.
허용 목록 모듈(경로를 직접 다뤄도 되는 곳, 설계 §5)의 합계는 `allowed`, 그 밖의 합계는 `outside`로 나누어 낸다.
`tests/test_no_direct_paths.py`가 `outside == 0`을 강제한다.

    python -m scripts.count_work_root
    -> {"modules": {"scripts/common.py": {"work_root": 3, ..., "all": 3, "allowed": true}, ...},
        "allowed": {"work_root": ..., "all": ...}, "outside": {"work_root": ..., "all": ...}, "total": {...}}
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.common import PLUGIN_ROOT, ArgParser, run

NAMES = ("work_root", "workspace_dir", "category_dir", "existing_category_dir", "move_to_trash")
# 경로를 직접 다뤄도 되는 모듈(설계 §5 허용 목록). scripts/ 기준 POSIX 상대 경로
ALLOWED = frozenset({
    "scripts/ctx.py", "scripts/common.py", "scripts/filestore.py", "scripts/keys.py",
    "scripts/count_work_root.py", "scripts/ui/server.py", "scripts/ui/__main__.py",
})
_NAMES_RE = "|".join(NAMES)
_CALL_RE = re.compile(r"(?<![\w.])(?<!def )(?:\w+\.)?(" + _NAMES_RE + r")\(")
_GETATTR_RE = re.compile(r"\bgetattr\([^,()]+,\s*[\"'](" + _NAMES_RE + r")[\"']")
_IMPORT_RE = re.compile(r"^[ \t]*from[ \t]+[\w.]+[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.MULTILINE)
_IMPORTED_RE = re.compile(r"(?<![\w.])(" + _NAMES_RE + r")(?!\w)")


def count_calls(text: str) -> dict[str, int]:
    counts = dict.fromkeys(NAMES, 0)
    for m in _CALL_RE.finditer(text):
        counts[m.group(1)] += 1
    for m in _GETATTR_RE.finditer(text):
        counts[m.group(1)] += 1
    for m in _IMPORT_RE.finditer(text):
        for name in _IMPORTED_RE.findall(m.group(1)):
            counts[name] += 1
    return {**counts, "all": sum(counts.values())}


def _sum(rows: list[dict[str, int]]) -> dict[str, int]:
    return {name: sum(r[name] for r in rows) for name in (*NAMES, "all")}


def main(argv: list[str]) -> dict:
    ArgParser(prog="count_work_root").parse_args(argv)
    scripts_dir = PLUGIN_ROOT / "scripts"
    me = Path(__file__).resolve()
    modules: dict[str, dict] = {}
    for path in sorted(scripts_dir.rglob("*.py")):
        if path.resolve() == me:
            continue
        counts = count_calls(path.read_text(encoding="utf-8"))
        if counts["all"]:
            rel = path.relative_to(PLUGIN_ROOT).as_posix()
            modules[rel] = {**counts, "allowed": rel in ALLOWED}
    allowed = _sum([c for m, c in modules.items() if c["allowed"]])
    outside = _sum([c for m, c in modules.items() if not c["allowed"]])
    return {"modules": modules, "allowed": allowed, "outside": outside, "total": _sum([allowed, outside])}


if __name__ == "__main__":
    run(main)

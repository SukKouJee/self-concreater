"""레퍼런스 저장소: md 프런트매터 읽기/쓰기, 카테고리·단계 관리.

사용법 (작업 폴더에서):
  python -m scripts.refstore scaffold            작업 폴더 기본 구조 생성
  python -m scripts.refstore init <category>      refs/ref_<category>/ 생성 (기존 파일은 건드리지 않음)
  python -m scripts.refstore categories           카테고리 목록과 ref/mine 수
  python -m scripts.refstore count <category>     {"ref", "mine", "stage"}
  python -m scripts.refstore update-stage <cat>   index.md의 '## 단계' 섹션 갱신

원칙: 본문은 바이트 단위로 보존하고, '## 내 메모'는 어떤 경우에도 덮어쓰지 않는다.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts.common import (
    TEMPLATES_DIR, ArgParser, ScriptError, category_dir, run, work_root,
)

PROTECTED_SECTIONS = {"## 내 메모"}
WORK_DIRS = ["refs", "mine", "drafts", "missions", "reviews", "workspace"]

_FRONTMATTER_RE = re.compile(r"\A---(\r?\n)(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z", re.DOTALL)

_STAGE_INFO = {
    1: ("0~4개", "영상별 분석, 기법 카탈로그, 기법 모티브"),
    2: ("5~9개", "예비 관찰, 각도 모티브, 예비 진단"),
    3: ("10개 이상", "정식 유형화, 흐름·조합 모티브, 진단"),
}


# --- 프런트매터 ---------------------------------------------------------------

def split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ScriptError("no_frontmatter", "md 파일이 '---' 프런트매터로 시작하지 않습니다")
    try:
        fm = yaml.safe_load(m.group(2)) or {}
    except yaml.YAMLError as e:
        raise ScriptError("bad_frontmatter", f"프런트매터 YAML 오류: {e}") from e
    if not isinstance(fm, dict):
        raise ScriptError("bad_frontmatter", "프런트매터가 키: 값 형식이 아닙니다")
    return fm, m.group(3)


def join_frontmatter(fm: dict, body: str, nl: str = "\n") -> str:
    # 최상위 키는 한 줄씩(block), 안쪽 리스트·딕셔너리는 한 줄(flow)로 둬서 사람이 고치기 쉽게
    parts = [
        yaml.safe_dump({k: v}, sort_keys=False, allow_unicode=True, width=1000,
                       default_flow_style=None if isinstance(v, (dict, list)) and v else False)
        for k, v in fm.items()
    ]
    dumped = "".join(parts).rstrip("\n").replace("\n", nl)
    return f"---{nl}{dumped}{nl}---{nl}{body}"


def _read_text(path: Path) -> str:
    # utf-8-sig: PowerShell 등이 붙인 BOM을 허용
    return path.read_bytes().decode("utf-8-sig")


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def read_md(path: Path) -> tuple[dict, str]:
    try:
        return split_frontmatter(_read_text(path))
    except ScriptError as e:
        raise ScriptError(e.code, f"{path}: {e.message}") from e


def update_frontmatter(path: Path, updates: dict[str, Any]) -> dict:
    text = _read_text(path)
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = split_frontmatter(text)
    fm.update(updates)
    _write_text(path, join_frontmatter(fm, body, nl))
    return fm


# --- 본문 섹션 ----------------------------------------------------------------

def replace_section(body: str, heading: str, content: str) -> str:
    """`heading` 줄부터 다음 '## ' 제목 전까지의 내용을 `content`로 바꾼다."""
    if heading.strip() in PROTECTED_SECTIONS:
        raise ScriptError("protected_section", f"'{heading}' 섹션은 사용자 영역이라 덮어쓸 수 없습니다")
    nl = "\r\n" if "\r\n" in body else "\n"
    lines = body.split(nl)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        raise ScriptError("section_not_found", f"'{heading}' 섹션이 없습니다")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), None)
    new = content.rstrip("\r\n").splitlines()
    tail = lines[end:] if end is not None else []
    return nl.join(lines[: start + 1] + new + [""] + tail)


# --- 단계 ---------------------------------------------------------------------

def _stage_limits() -> tuple[int, int]:
    stages = json.loads((TEMPLATES_DIR / "thresholds.json").read_text("utf-8"))["stages"]
    return stages["preliminary"], stages["formal"]


def stage_for(ref_count: int) -> int:
    preliminary, formal = _stage_limits()
    if ref_count < preliminary:
        return 1
    return 2 if ref_count < formal else 3


def stage_text(ref_count: int, mine_count: int) -> str:
    preliminary, formal = _stage_limits()
    stage = stage_for(ref_count)
    span, features = _STAGE_INFO[stage]
    lines = [
        f"- ref: {ref_count}개 / mine: {mine_count}개",
        f"- 현재 단계: {stage} ({span}) — {features}",
    ]
    if stage == 1:
        lines.append(f"- 다음 단계까지: ref {preliminary - ref_count}개 더 ({preliminary}개부터 예비 관찰)")
    elif stage == 2:
        lines.append(f"- 다음 단계까지: ref {formal - ref_count}개 더 ({formal}개부터 정식 유형화)")
    else:
        lines.append("- 마지막 단계입니다. 레퍼런스가 늘수록 판정이 안정됩니다.")
    return "\n".join(lines)


# --- 작업 폴더 / 카테고리 ------------------------------------------------------

def _rel(path: Path) -> str:
    return path.relative_to(work_root()).as_posix()


def scaffold() -> dict:
    root = work_root()
    created = []
    for name in WORK_DIRS:
        d = root / name
        if not d.exists():
            d.mkdir(parents=True)
            created.append(name + "/")

    gitignore = root / ".gitignore"
    lines = _read_text(gitignore).splitlines() if gitignore.exists() else []
    if "workspace/" not in lines:
        _write_text(gitignore, "\n".join(lines + ["workspace/"]) + "\n")
        created.append(".gitignore")

    return {
        "root": str(root),
        "created": created,
        "profile_exists": (root / "profile.md").exists(),
        "git": (root / ".git").exists(),
        "categories": [c["category"] for c in list_categories()],
    }


def _fill_template(name: str, category: str) -> str:
    text = (TEMPLATES_DIR / name).read_text("utf-8")
    text = re.sub(r"^category: null\b", f"category: {category}", text, count=1, flags=re.M)
    text = re.sub(r"^created: null\b", f"created: {date.today().isoformat()}", text, count=1, flags=re.M)
    return text.replace("{{category}}", category)


def init_category(category: str) -> dict:
    cat = category_dir(category)
    (cat / "videos").mkdir(parents=True, exist_ok=True)
    created, skipped = [], []
    for name in ["index.md", "techniques.md"]:
        path = cat / name
        if path.exists():
            skipped.append(_rel(path))
            continue
        text = _fill_template(name, category)
        if name == "index.md":
            fm, body = split_frontmatter(text)
            text = join_frontmatter(fm, replace_section(body, "## 단계", stage_text(0, 0)))
        _write_text(path, text)
        created.append(_rel(path))
    return {"category": category, "path": _rel(cat), "created": created, "skipped": skipped}


def _existing_category_dir(category: str) -> Path:
    cat = category_dir(category)
    if not cat.is_dir():
        raise ScriptError("category_not_found", f"카테고리가 없습니다: {category} (먼저 init)")
    return cat


def _role_counts(cat: Path) -> tuple[int, int]:
    ref = mine = 0
    for path in sorted((cat / "videos").glob("*.md")):
        role = read_md(path)[0].get("role")
        if role == "ref":
            ref += 1
        elif role == "mine":
            mine += 1
    return ref, mine


def count(category: str) -> dict:
    ref, mine = _role_counts(_existing_category_dir(category))
    return {"ref": ref, "mine": mine, "stage": stage_for(ref)}


def list_categories() -> list[dict]:
    refs = work_root() / "refs"
    if not refs.is_dir():
        return []
    return [
        {"category": d.name.removeprefix("ref_"), **count(d.name.removeprefix("ref_"))}
        for d in sorted(refs.glob("ref_*")) if d.is_dir()
    ]


def update_stage(category: str) -> dict:
    cat = _existing_category_dir(category)
    index = cat / "index.md"
    ref, mine = _role_counts(cat)
    text = _read_text(index)
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = split_frontmatter(text)
    section = stage_text(ref, mine)
    _write_text(index, join_frontmatter(fm, replace_section(body, "## 단계", section), nl))
    return {"ref": ref, "mine": mine, "stage": stage_for(ref), "text": section}


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str]) -> Any:
    parser = ArgParser(prog="python -m scripts.refstore")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scaffold")
    sub.add_parser("categories")
    for name in ["init", "count", "update-stage"]:
        sub.add_parser(name).add_argument("category")
    args = parser.parse_args(argv)

    if args.cmd == "scaffold":
        return scaffold()
    if args.cmd == "categories":
        return {"categories": list_categories()}
    if args.cmd == "init":
        return init_category(args.category)
    if args.cmd == "count":
        return count(args.category)
    return update_stage(args.category)


if __name__ == "__main__":
    run(main)

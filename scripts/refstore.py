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
import os
import re
import threading
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts import ctx, keys
from scripts.common import TEMPLATES_DIR, ArgParser, ScriptError, run

INDEX_LOCK = threading.RLock()        # index.md 읽기-고치기-쓰기를 한 번에 하나씩 (프로세스 안)
PROTECTED_SECTIONS = {"## 내 메모"}
WORK_DIRS = ["refs", "mine", "drafts", "missions", "reviews", "workspace"]

_FRONTMATTER_RE = re.compile(r"\A---(\r?\n)(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z", re.DOTALL)

_STAGE_INFO = {
    1: ("0~4개", "영상별 분석, 기법 카탈로그, 기법 모티브"),
    2: ("5~9개", "예비 관찰, 각도 모티브, 예비 진단"),
    3: ("10개 이상", "정식 유형화, 흐름·조합 모티브, 진단"),
}


# --- 프런트매터 ---------------------------------------------------------------

# 읽기는 libyaml(C) 파서가 있으면 그것으로(순수 파이썬보다 몇 배 빠르다). 쓰기는 순수 파이썬 SafeDumper 그대로:
# libyaml 출력기는 이모지 같은 BMP 밖 문자(🏆)를 "\U0001F3C6" 같은 이스케이프로 바꿔 써서 지금 파일과 바이트가 달라진다.
YAML_LOADER: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ScriptError("no_frontmatter", "md 파일이 '---' 프런트매터로 시작하지 않습니다")
    try:
        fm = yaml.load(m.group(2), Loader=YAML_LOADER) or {}
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


def _decode(raw: bytes) -> str:
    # utf-8-sig: PowerShell 등이 붙인 BOM을 허용
    return raw.decode("utf-8-sig")


REPLACE_RETRIES = 5


def escape_headings(text: str) -> str:
    """줄바꿈을 LF로 맞추고, 진짜 md 제목(`#`~`######` + 공백/줄끝)이나 표처럼 보이는 줄(`|`) 앞에 백슬래시를 붙인다.
    `#해시태그`는 그대로. 읽을 때(conti.parse_draft)는 이 백슬래시를 되돌린다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"^([ \t]*)(#{1,6}(?=\s|$)|\|)", r"\1\\\2", text, flags=re.MULTILINE)


def text_bytes(text: str, newline: str | None = None) -> bytes:
    """`Path.write_text(text, encoding="utf-8", newline=newline)`이 쓰는 것과 같은 바이트.
    newline이 None이면 "\n"을 os.linesep으로(Windows CRLF), ""·"\n"이면 그대로, 그 밖이면 "\n"을 그 값으로 바꾼다."""
    nl = os.linesep if newline is None else newline
    if nl not in ("", "\n"):
        text = text.replace("\n", nl)
    return text.encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 교체한다. 읽는 쪽이 반쯤 쓴 파일을 보지 않는다.

    Windows에서는 다른 프로그램(읽는 쪽, 백신, 동기화)이 잠깐 열고 있으면 교체가 거부되므로 짧게 다시 시도한다.
    """
    # 같은 파일을 두 스레드·프로세스가 동시에 써도 서로의 임시 파일을 덮지 않게 pid·tid를 붙인다.
    # 점으로 시작하고 .tmp로 끝나서 `*.md`·`NNNN.json` 목록에 걸리지 않는다.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def parse_md(raw: bytes, path: Path | str) -> tuple[dict, str]:
    """이미 읽은 md 바이트 → (프런트매터, 본문). 오류 메시지에 경로(또는 저장소의 `Doc.label`)를 붙인다."""
    try:
        return split_frontmatter(raw.decode("utf-8-sig"))
    except ScriptError as e:
        raise ScriptError(e.code, f"{path}: {e.message}") from e


def _doc_key(key: Any) -> str:
    if not isinstance(key, str):
        raise TypeError(f"문서 키(str)만 받습니다: {key!r}")
    return key


def read(key: str) -> tuple[dict, str]:
    """문서 키 → (프런트매터, 본문). 없으면 FileNotFoundError, 파싱 오류는 `<label>: ...` ScriptError."""
    return ctx.here().store.read(_doc_key(key)).parsed


def _updated(raw: bytes, updates: dict[str, Any], remove: Iterable[str]) -> tuple[dict, bytes]:
    text = _decode(raw)
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    fm, body = split_frontmatter(text)
    fm.update(updates)
    for name in remove:
        fm.pop(name, None)
    return fm, join_frontmatter(fm, body, nl).encode("utf-8")


def update_frontmatter(key: str, updates: dict[str, Any], remove: Iterable[str] = (), *,
                       expect_version: str | None = None) -> dict:
    """문서 키의 프런트매터에 `updates`를 적용한 뒤 `remove`의 키를 지운다(없는 키는 무시). 본문은 그대로 둔다.
    `expect_version`: 쓰는 순간 바이트의 sha256이 다르면 `VersionConflict`."""
    store = ctx.here().store
    fm, data = _updated(store.read(_doc_key(key)).raw, updates, remove)
    store.write(key, data, expect_version=expect_version)
    return fm


# --- 내 메모 가드 --------------------------------------------------------------

MEMO_HEADING = "## 내 메모"


def memo_text(raw: bytes) -> str | None:
    """md 바이트의 '## 내 메모' 내용(메모 가드의 비교 기준). 제목 줄이 없으면 None.

    프런트매터 뒤 본문에서 `## 내 메모` 줄(줄 끝 공백 무시) 다음부터 다음 `## ` 제목 줄 전까지를 LF로 맞추고,
    줄마다 끝 공백을 지운 뒤 앞뒤 빈 줄을 뺀다. 메모를 옮겨 쓰는 흐름마다 앞 빈 줄·끝 공백·줄바꿈 방식·메모 뒤 절을
    다루는 방식이 조금씩 달라서 그 차이는 같은 메모로 본다. 깨진 UTF-8은 대체 문자로 읽는다(같은 바이트면 같은 결과)."""
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    lines = (m.group(3) if m else text).split("\n")
    span = memo_span(lines)
    if span is None:
        return None
    return "\n".join(line.rstrip() for line in lines[span[0] + 1:span[1]]).strip("\n")


def memo_span(lines: list[str]) -> tuple[int, int] | None:
    """본문 줄 목록에서 메모 절의 (제목 줄 번호, 끝 번호(다음 `## ` 줄 또는 줄 수)). 없으면 None.
    제목은 줄 끝 공백만 무시하고 정확히 `## 내 메모`인 첫 줄(`## 내 메모장`·`### 내 메모`는 아니다). 줄 끝 `\\r`도 공백으로 본다."""
    start = next((i for i, line in enumerate(lines) if line.rstrip() == MEMO_HEADING), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


_MEMO_LINE_RE = re.compile(r"^" + re.escape(MEMO_HEADING) + r"[^\S\n]*$", re.MULTILINE)


def memo_after(body: str) -> str | None:
    """본문에서 메모 제목 줄(`memo_span`과 같은 판정) 바로 뒤부터 끝까지(제목 줄의 줄바꿈부터). 제목이 없으면 None."""
    m = _MEMO_LINE_RE.search(body)
    return None if m is None else body[m.end():]


def check_memo(key: str, old: bytes, new: bytes) -> None:
    """기존 문서(old)에 내용 있는 '## 내 메모'가 있으면 새 바이트(new)에도 같은 메모가 있어야 한다. 아니면 memo_lost."""
    before = memo_text(old)
    if before and memo_text(new) != before:
        raise ScriptError("memo_lost", f"이 작업은 '{MEMO_HEADING}' 내용을 지우거나 바꾸게 되어 저장하지 않았습니다: {key}. "
                                       "문서와 메모는 그대로 남아 있습니다")


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

def scaffold() -> dict:
    store, objects = ctx.here().store, ctx.here().objects
    created = []
    for name in WORK_DIRS:
        if not store.folder_exists(name + "/"):
            store.ensure_folder(name + "/")
            created.append(name + "/")

    try:
        lines = _decode(objects.get(keys.GITIGNORE)).splitlines()
    except FileNotFoundError:
        lines = []
    if keys.WORKSPACE not in lines:
        objects.put(keys.GITIGNORE, ("\n".join(lines + [keys.WORKSPACE]) + "\n").encode("utf-8"))
        created.append(".gitignore")

    return {
        "root": store.label(),
        "created": created,
        "profile_exists": store.exists(keys.PROFILE),
        # worktree·submodule에서는 .git이 파일이다
        "git": objects.folder_exists(keys.GIT_DIR) or objects.exists(keys.GIT_DIR.rstrip("/")),
        "categories": [c["category"] for c in list_categories()],
    }


def _fill_template(name: str, category: str) -> str:
    text = (TEMPLATES_DIR / name).read_text("utf-8")
    text = re.sub(r"^category: null\b", f"category: {category}", text, count=1, flags=re.M)
    text = re.sub(r"^created: null\b", f"created: {date.today().isoformat()}", text, count=1, flags=re.M)
    return text.replace("{{category}}", category)


def init_category(category: str) -> dict:
    store = ctx.here().store
    prefix = keys.category(category)                  # 이름 검사가 폴더 만들기보다 먼저
    store.ensure_folder(keys.videos_dir(category))
    created, skipped = [], []
    for name, key in [("index.md", keys.index_md_at(prefix)), ("techniques.md", keys.techniques_md_at(prefix))]:
        if store.exists(key):
            skipped.append(key)
            continue
        text = _fill_template(name, category)
        if name == "index.md":
            fm, body = split_frontmatter(text)
            text = join_frontmatter(fm, replace_section(body, "## 단계", stage_text(0, 0)))
        store.write(key, text.encode("utf-8"))
        created.append(key)
    return {"category": category, "path": prefix.rstrip("/"), "created": created, "skipped": skipped}


def require_category(category: str) -> str:
    """카테고리 폴더 키(`refs/ref_<cat>/`). 이름이 틀리면 invalid_category, 폴더가 없으면 category_not_found."""
    prefix = keys.category(category)
    if not ctx.here().store.folder_exists(prefix):
        raise ScriptError("category_not_found", f"카테고리가 없습니다: {category} (먼저 init)")
    return prefix


def _role_counts(prefix: str) -> tuple[int, int]:
    ref = mine = 0
    for meta in ctx.here().store.list(keys.videos_dir_at(prefix), "*.md"):
        role = read(meta.key)[0].get("role")
        if role == "ref":
            ref += 1
        elif role == "mine":
            mine += 1
    return ref, mine


def count(category: str) -> dict:
    ref, mine = _role_counts(require_category(category))
    return {"ref": ref, "mine": mine, "stage": stage_for(ref)}


def find_video(video_id: str) -> list[dict]:
    """모든 카테고리에서 videos/{video_id}.md를 찾는다."""
    store = ctx.here().store
    found = []
    for folder in store.folders(keys.REFS, keys.CATEGORY_GLOB):
        for meta in store.list(keys.videos_dir_at(folder), f"{video_id}.md"):
            found.append({
                "category": keys.category_name(folder),
                "role": read(meta.key)[0].get("role"),
                "md": meta.key,
            })
    return found


def list_categories() -> list[dict]:
    return [
        {"category": name, **count(name)}
        for name in (keys.category_name(f) for f in ctx.here().store.folders(keys.REFS, keys.CATEGORY_GLOB))
    ]


def update_stage(category: str) -> dict:
    store = ctx.here().store
    prefix = require_category(category)
    index = keys.index_md_at(prefix)
    with INDEX_LOCK:
        ref, mine = _role_counts(prefix)       # 락 안에서 세야 늦게 쓰는 쪽이 낡은 단계를 덮어쓰지 않는다
        section = stage_text(ref, mine)
        text = _decode(store.read(index).raw)
        nl = "\r\n" if text.startswith("---\r\n") else "\n"
        fm, body = split_frontmatter(text)
        store.write(index, join_frontmatter(fm, replace_section(body, "## 단계", section), nl).encode("utf-8"))
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

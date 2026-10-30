"""저장소 키(S3 설계 §2, ADR-1·ADR-5): 작업 폴더 기준 POSIX 상대 경로 문자열.

키는 지금 프런트매터·CLI 출력·UI API·`/files/<rel>` URL에 저장·노출되는 문자열 그대로다. 키를 만드는 곳은 이 모듈 한 곳이고,
순수 함수만 둔다(IO 없음). 폴더(접두사) 키는 `/`로 끝난다(`category`, `frames_dir`, `history_dir` …).
"""
from __future__ import annotations

import posixpath
from typing import Literal

from scripts.common import _CATEGORY_RE, ScriptError

# 작업 폴더 최상위
REFS = "refs/"
DRAFTS = "drafts/"
MINE = "mine/"
MISSIONS = "missions/"
REVIEWS = "reviews/"
REPORTS = "reports/"
PROFILE = "profile.md"
CONFIG = "yt-coach.json"
LIBRARY_CONFIG = "library.json"
GITIGNORE = ".gitignore"
GIT_DIR = ".git/"
TRASH = "workspace/trash/"

CONTI_SUFFIX = "-conti"
SHARED_PREFIXES = ("workspace/raw/", "workspace/cuts/", "workspace/frames/", "workspace/shots/")
TYPIFY_VARIANTS = (None, "applied", "preview")

Kind = Literal["doc", "shared", "object"]


# --- 검사 ---------------------------------------------------------------------------------

def check(key: str) -> str:
    """상대 POSIX 경로인지 본다(폴더 키의 끝 `/` 하나는 허용). 빈 조각·`.`·`..`·`\\`·`:`·NUL은 ScriptError("bad_path")."""
    body = key[:-1] if key.endswith("/") else key
    if (not body or "\\" in key or ":" in key or "\x00" in key
            or any(part in ("", ".", "..") for part in body.split("/"))):
        raise ScriptError("bad_path", f"잘못된 경로입니다: {key!r}")
    return key


def kind(key: str) -> Kind:
    """키가 속한 저장소: md는 문서(DocStore), 영상 ID 기준 캐시는 shared, 나머지는 object."""
    if key.endswith(".md"):
        return "doc"
    if key.startswith(SHARED_PREFIXES):
        return "shared"
    return "object"


# --- 카테고리 ------------------------------------------------------------------------------

def category(cat: str) -> str:
    if not _CATEGORY_RE.match(cat):
        raise ScriptError(
            "invalid_category",
            f"카테고리 이름은 영문 소문자·숫자·-·_만 쓸 수 있습니다: {cat!r}",
        )
    return f"{REFS}ref_{cat}/"


def category_name(prefix: str) -> str:
    """`refs/ref_<cat>/` → `<cat>`."""
    return prefix.rstrip("/").rsplit("/", 1)[-1].removeprefix("ref_")


def videos_dir(cat: str) -> str:
    return f"{category(cat)}videos/"


def video_md(cat: str, vid: str) -> str:
    return f"{videos_dir(cat)}{vid}.md"


def index_md(cat: str) -> str:
    return f"{category(cat)}index.md"


def techniques_md(cat: str) -> str:
    return f"{category(cat)}techniques.md"


# --- 기획안 · 콘티 ---------------------------------------------------------------------------

def stem(key: str) -> str:
    """파일 키의 확장자 뺀 이름(`Path(key).stem`과 같다)."""
    name = posixpath.basename(key)
    root, ext = posixpath.splitext(name)
    return root if ext else name


def draft_md(name: str) -> str:
    return f"{DRAFTS}{name}.md"


def conti_md(draft_key: str) -> str:
    """기획안 키 → 같은 폴더의 `<이름>-conti.md`(`conti.conti_path`)."""
    if not draft_key.endswith(".md"):
        raise ValueError(f"기획안 md 키가 아닙니다: {draft_key!r}")
    return posixpath.join(posixpath.dirname(draft_key), f"{stem(draft_key)}{CONTI_SUFFIX}.md")


def _conti_base(conti_key: str) -> tuple[str, str]:
    """콘티 키 → (폴더, `-conti` 뺀 이름). 콘티 키가 아니면 ValueError."""
    s = stem(conti_key)
    if not conti_key.endswith(".md") or not s.endswith(CONTI_SUFFIX):
        raise ValueError(f"콘티 md 키가 아닙니다: {conti_key!r}")
    return posixpath.dirname(conti_key), s[: -len(CONTI_SUFFIX)]


def _sibling(conti_key: str, suffix: str) -> str:
    folder, base = _conti_base(conti_key)
    return posixpath.join(folder, f"{base}{suffix}")


def conti_html(conti_key: str) -> str:
    _conti_base(conti_key)
    return conti_key[: -len(".md")] + ".html"


def srt(conti_key: str, suffix: str) -> str:
    """suffix: `captions` | `lines`(`conti.SRT_TRACKS`)."""
    return _sibling(conti_key, f"-{suffix}.srt")


def captions_mp4(conti_key: str) -> str:
    return _sibling(conti_key, "-captions.mp4")


def remaining_html(conti_key: str) -> str:
    return _sibling(conti_key, "-remaining.html")


def footage_csv(conti_key: str) -> str:
    return _sibling(conti_key, "-footage.csv")


def cut_files_dir(conti_key: str) -> str:
    return _sibling(conti_key, "-cuts/")


# --- 그 밖의 문서 -------------------------------------------------------------------------

def mine_md(name: str) -> str:
    return f"{MINE}{name}.md"


def mission_md(day: str) -> str:
    return f"{MISSIONS}{day}.md"


def review_md(week: str) -> str:
    return f"{REVIEWS}{week}.md"


def report_html(cat: str, day: str) -> str:
    category(cat)
    return f"{REPORTS}{cat}-{day}.html"


# --- workspace ------------------------------------------------------------------------------

def raw(vid: str) -> str:
    return f"workspace/raw/{vid}.json"


def cuts(vid: str) -> str:
    return f"workspace/cuts/{vid}.json"


def frames_dir(vid: str) -> str:
    return f"workspace/frames/{vid}/"


def shots(vid: str) -> str:
    return f"workspace/shots/{vid}.json"


def typify(cat: str, variant: str | None = None) -> str:
    """`workspace/typify/<cat>.json`, variant가 applied·preview면 `<cat>.<variant>.json`."""
    if variant not in TYPIFY_VARIANTS:
        raise ValueError(f"알 수 없는 유형화 파일 종류입니다: {variant!r}")
    category(cat)
    return f"workspace/typify/{cat}{'' if variant is None else '.' + variant}.json"


def history_dir(conti_key: str) -> str:
    """콘티 이력 폴더. 지금처럼 콘티 이름(stem)만 쓴다(drafts 밖 콘티가 없다는 가정, 설계 §7-8)."""
    return f"workspace/conti_history/{stem(conti_key)}/"


def history_version(conti_key: str, n: int) -> str:
    return f"{history_dir(conti_key)}{int(n):04d}.json"


def art(conti_key: str, cut_id: str, basis: str, sha6: str) -> str:
    """장면 그림 `workspace/conti-art/<콘티 stem>/<컷 id>-<basis 8자>-<svg 해시 6자>.svg`."""
    return f"workspace/conti-art/{stem(conti_key)}/{cut_id}-{basis}-{sha6}.svg"


def footage_dir(fid: str) -> str:
    return f"workspace/footage/{fid}/"


def footage_match(conti_key: str) -> str:
    return f"workspace/footage_match/{stem(conti_key)}.json"

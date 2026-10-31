"""판별 문구(static/copy.js)와 정적 화면 게이트.

- 로컬판 문구는 바뀌지 않는다: COPY의 local 값은 이 파일의 LOCAL_GOLDEN(바꾸기 전 화면에 있던 글자 그대로)과 같다.
- 서버판 문구에는 내 PC 전용 말(tests/helpers/copy_gate.FORBIDDEN)이 없다.
- 정적 JS·HTML의 나머지 문자열(두 판 공통)에도 그런 말이 없다. 남는 것은 ALLOWED에 이유와 함께 적은 로컬판 전용 자리뿐.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ui.server import STATIC_DIR
from tests.helpers.copy_gate import FORBIDDEN, hits, js_strings, scan

NODE = shutil.which("node")
COPY_JS = STATIC_DIR / "copy.js"
ROOT = Path(__file__).resolve().parents[1]

# Every edition-specific sentence as the local edition showed it before copy.js existed (pinned: must not change).
LOCAL_GOLDEN: dict[str, str] = {
    "home.noCategory": "Claude에게 “처음 설정해줘”라고 말하면 첫 카테고리를 함께 만들어요.",
    "home.init.title": "아직 작업 폴더가 아닙니다",
    "home.init.lead": "지금 연 폴더를 작업 폴더로 만들면 레퍼런스와 기획안이 여기에 저장돼요.",
    "home.init.button": "이 폴더를 yt-coach 작업 폴더로 만들기",
    "home.init.done": "작업 폴더를 만들었습니다",
    "menu.noCategory": "카테고리를 먼저 만들어요",
    "report.noCategory": "카테고리를 먼저 만들어 주세요.",
    "page.noCategory": "카테고리가 없습니다. 홈에서 먼저 카테고리를 만드세요.",
    "add.unsaved": "저장하지 않은 결과는 UI 서버를 끄면 사라져요",
    "add.noCategory.after": "에서 작업 폴더와 카테고리를 만들어 주세요.",
    "refs.noCategory": "카테고리가 아직 없습니다. 홈에서 작업 폴더와 카테고리를 먼저 만드세요.",
    "refs.trashHint": "기법 모음에서도 이 영상 출처를 뺍니다. 다른 카테고리에 같은 영상이 있으면 분석 데이터는 그대로 둡니다. "
                      "잘못 지웠으면 Claude에게 “휴지통에서 되살려줘”라고 말하세요.",
    "trash.restoreHint": "잘못 지웠으면 Claude에게 “휴지통에서 되살려줘”라고 말하세요.",
    "conti.changedElsewhere": "다른 곳(편집기·터미널)에서 이 콘티 파일이 바뀌어 저장하지 않았습니다. 다시 불러온 뒤 고쳐 주세요. "
                              "입력해 둔 내용은 남아 있습니다.",
    "conti.exportSummary": " CapCut용 SRT · 움직이는 자막 영상",
    "conti.artBlocked": "Claude를 쓸 수 없어 장면 그림을 다시 그리지 못했어요",
    "mission.profileNote": "프로필을 만들면 미션이 맞춤으로 나옵니다. Claude에게 “처음 설정해줘”라고 말하면 함께 만들어요",
    "motif.profileNote": "프로필이 없어 카드가 일반적으로 나옵니다 — Claude에게 “처음 설정해줘”라고 말하면 만들 수 있어요",
    "report.noProfile": "프로필이 없어 제안을 반영할 수 없습니다. Claude에게 “처음 설정해줘”라고 말하면 프로필을 먼저 만들어요",
    "motif.catalogHint": "기법 모음 정리(같은 기법 합치기)는 Claude에게 “기법 모음 정리해줘”라고 말하면 돼요",
    "help.chatEmpty": "이 화면에 대해 궁금한 점을 물어보세요. 답은 도움말과 지금 작업 폴더 상태를 바탕으로 만들어집니다.",
}

# Shared static strings that keep a local-only word, each with why the server edition never shows it.
ALLOWED: dict[tuple[str, str], str] = {
    ("scripts/ui/static/api.js", "터미널"): "bad_token: only the local stdlib server answers it (the server edition uses login)",
    ("scripts/ui/static/app.js", "Claude에게"): "banner after 서버 끄기, which the server edition does not render",
    ("scripts/ui/static/footage-area.js", "플러그인"): "footage area: data-feature=footage, off on the server edition",
    ("scripts/ui/static/footage-area.js", "pip install"): "footage area: data-feature=footage, off on the server edition",
    ("scripts/ui/static/pages/conti.js", "ytc "): "caption video setup command: data-feature=caption_video, off on the server",
}


def _js(expr: str, edition: str = "local"):
    code = (f"import({json.dumps(COPY_JS.as_uri())}).then(m => {{ m.setEdition({json.dumps(edition)}); "
            f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))); }})")
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@needs_node
def test_local_copy_is_unchanged():
    assert _js("Object.fromEntries(Object.keys(m.COPY).map(k => [k, m.t(k)]))") == LOCAL_GOLDEN


@needs_node
def test_server_copy_has_no_local_only_words_and_differs():
    server = _js("Object.fromEntries(Object.keys(m.COPY).map(k => [k, m.t(k)]))", "server")
    assert set(server) == set(LOCAL_GOLDEN)
    for key, text in server.items():
        if text is None:                     # hidden on the server edition
            continue
        assert isinstance(text, str) and text.strip(), key
        assert hits(text) == [], (key, text)
        assert text != LOCAL_GOLDEN[key], key


@needs_node
def test_edition_defaults_to_local_and_rejects_unknown_values():
    assert _js("m.edition()") == "local"
    assert _js("[m.edition(), m.isServer()]", "server") == ["server", True]
    assert _js("m.edition()", "weird") == "local"
    assert _js('(() => { try { m.t("nope"); return "no error"; } catch (e) { return String(e.message); } })()') \
        == "unknown copy key: nope"


@needs_node
def test_hidden_entries_are_null_on_server():
    assert _js('m.t("motif.catalogHint")', "server") is None


def test_every_copy_key_is_used_by_a_screen():
    sources = "\n".join(p.read_text(encoding="utf-8") for p in STATIC_DIR.rglob("*.js") if p != COPY_JS)
    for key in LOCAL_GOLDEN:
        assert re.search(rf"\bt\(\"{re.escape(key)}\"\)", sources), key


def test_copy_js_keeps_both_editions_for_every_key():
    text = COPY_JS.read_text(encoding="utf-8")
    for key in LOCAL_GOLDEN:
        assert f'"{key}"' in text, key


def _static_files() -> list[Path]:
    files = [p for p in sorted(STATIC_DIR.rglob("*.js")) if p != COPY_JS]
    files += [STATIC_DIR / "index.html", *sorted((ROOT / "server" / "web").glob("*"))]
    return files


def test_static_strings_have_no_local_only_words_outside_the_allowlist():
    found = scan(_static_files(), js_strings, ROOT)
    unexpected = [f for f in found if (f[0], f[2]) not in ALLOWED]
    assert unexpected == []
    # the allowlist stays tight: every entry still matches something
    assert {(f[0], f[2]) for f in found} == set(ALLOWED)


def test_js_string_scanner_skips_comments_and_reads_all_quotes():
    src = 'const a = "작업 폴더"; // 터미널\n/* ytc ui */ const b = `x ${y} 슬래시`; const c = \'it\\\'s\';'
    assert js_strings(src) == [(1, "작업 폴더"), (2, "x ${y} 슬래시"), (2, "it\\'s")]


def test_forbidden_list_covers_the_dispatch_terms():
    for term in ("작업 폴더", "터미널", "ytc ", "localhost", "127.0.0.1", "Claude Code", "슬래시"):
        assert term in FORBIDDEN

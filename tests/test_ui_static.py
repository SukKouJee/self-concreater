import re

from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401  (fixture 재사용)

MENU_NAMES = ["홈", "레퍼런스 추가", "레퍼런스 목록", "유형화", "리포트", "모티브", "기획안", "콘티", "진단", "미션"]
COMMANDS = ["/yt-coach:yt-review", "/yt-coach:yt-inspire",
            "/yt-coach:yt-draft", "/yt-coach:yt-diagnose", "/yt-coach:yt-mission"]


def test_index_loads_modules(client):
    status, _, raw = client.call("GET", f"/?key={client.app.token}", token=False)
    assert status == 200
    html = raw.decode("utf-8")
    assert re.search(r'<script[^>]*type="module"[^>]*src="/static/app\.js"', html) or \
        re.search(r'<script[^>]*src="/static/app\.js"[^>]*type="module"', html)
    assert "/static/app.css" in html


def test_static_files_served(client):
    for rel in ["app.css", "app.js", "api.js", "pages/home.js", "pages/placeholder.js", "pages/add.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200, rel
    status, headers, _ = client.call("GET", "/static/app.js")
    assert "javascript" in headers["Content-Type"]


def test_menu_definition_has_all_items():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in MENU_NAMES:
        assert f'"{name}"' in text, name
    for cmd in COMMANDS:
        assert cmd in text, cmd


def test_no_external_resources():
    for path in STATIC_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"https?://", text), path.name


def test_page_contract_guards_present():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for token in ["isCurrent", "signal", "onCategory", 'startsWith("#/")', "preventDefault"]:
        assert token in app, token
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert home.count("isCurrent()") >= 4
    assert "renderTopbar" not in app


ADD_OPTION_LABELS = [
    "샷 분류도 함께", "이미 분석한 영상도 다시 분석",
    "자막·컷을 새로 받기(요청이 늘어 차단될 수 있어요)", "추가 시작", "샷 분류만 보강",
]
SECTION_LABELS = ["hook", "intro", "body", "demo", "transition", "cta", "outro"]


def test_add_page_has_option_labels():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    for label in ADD_OPTION_LABELS:
        assert label in text, label


def test_add_page_confirm_card_and_guards():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert ", ".join(f'"{x}"' for x in SECTION_LABELS) in text
    for token in ["isCurrent()", "/api/jobs/current?kind=collect", "encodeURIComponent", "filesUrl",
                  "clearTimeout", "음성 인식(STT)으로", "화면만 보고 분석", "남은 영상 모두 저장", "reviewed: true"]:
        assert token in text, token
    for banned in ["innerHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


def test_add_page_shots_opt_in_and_polling_safety():
    text = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert "shots: false" in text and "shots: true" not in text
    for token in ["job_not_found", "POLL_MAX_MS", 'aria-live']:
        assert token in text, token
    assert 'role: "status" }, text' not in text


RECREATE_WARNING = "다시 만들면 화면에서 바꾼 대사·가이드·고정한 참고 프레임이 사라지고 내 메모만 남습니다"


def test_conti_page_served_with_recreate_warning(client):
    status, headers, raw = client.call("GET", "/static/pages/conti.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert RECREATE_WARNING in raw.decode("utf-8")


def test_conti_page_contract_and_safety():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in [
        "isCurrent()", "/api/drafts", "/api/conti/create", "/api/conti/save", "/api/conti/scenes",
        "/api/conti/alternatives", "/api/conti/cut", "/api/jobs/current?kind=conti", "conti_exists",
        "conti_changed", "파일이 밖에서 바뀌었습니다 — 다시 불러오기", "저장했습니다 · 스토리보드도 다시 그렸습니다",
        "레퍼런스 실제 장면", "대사 대안", "대안 더 보기", "이걸로 바꾸기", "고정 해제", "콘티 만들기", "다시 만들기",
        "스토리보드 HTML", "exclude", "hash", "DOMParser", "filesUrl", "clearTimeout", "aria-live", "noopener",
    ]:
        assert token in text, token
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


def test_conti_svg_sanitizer_strips_dangerous_parts():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in ["foreignObject", "script", 'startsWith("on")', "importNode"]:
        assert token in text, token


def test_section_colors_match_storyboard_mapping():
    # conti.render_html colors a section var(--s{LABELS.index(label)+1}); the UI must use the same slots.
    from scripts.conti import LABELS
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for i, label in enumerate(LABELS, start=1):
        rules = re.findall(rf"\.lb-{label}\s*\{{\s*background:\s*var\((--[\w-]+)\)", css)
        assert rules == [f"--s{i}"], (label, rules)


def test_conti_alternatives_are_requested_only_by_button():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for gone in ["AUTO_ALT_DELAY_MS", "scheduleAutoAlts", "clearAutoAlts", "altTimer", "altSkipped", "잠시 뒤 대사 대안을 요청합니다"]:
        assert gone not in text, gone
    render = text[text.index("function renderPanel"):text.index("// ---- alternatives")]
    assert "requestAlts(" not in render                     # 컷을 열기만 해서는 Claude를 부르지 않는다
    alts = text[text.index("function renderAlts"):text.index("// ---- edit form")]
    assert '"대안 받기"' in alts and "누르면 Claude 호출이 한 번 일어납니다" in alts
    assert "대안을 만들지 못했습니다" in alts and '"다시 시도"' in alts
    assert '"대안 더 보기"' in alts
    assert "if (rec.loading) return;" in text               # 같은 컷 중복 요청 막기는 그대로

def _luminance(hex_color):
    rgb = [int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    hi, lo = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_on_danger_and_ok_meets_contrast_in_both_schemes():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    for block in (css[:dark_at], css[dark_at:css.index("}", css.index("--danger", dark_at)) + 1]):
        tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", block))
        assert _contrast(tokens["--on-danger"], tokens["--danger"]) >= 4.5
        assert _contrast(tokens["--on-ok"], tokens["--ok"]) >= 4.5
    for selector in (".banner", ".toast-error", ".btn-danger"):
        rule = re.search(rf"^{re.escape(selector)}\s*\{{([^}}]*)\}}", css, re.M).group(1)
        assert "color: var(--on-danger)" in rule, selector
    assert "color: var(--on-ok)" in re.search(r"\.step-done \.step-dot\s*\{([^}]*)\}", css).group(1)


def test_refs_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'route: "#/refs"' in app
    assert 'soon: "list"' not in app
    assert 'case "refs"' in app


def test_refs_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    # "signal" is not asserted: ctx.api (bound per page in app.js) already carries ctx.signal, so using it is the contract.
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/refs", "레퍼런스 추가로 가기", "경로 복사",
                  "최근 추가순", "길이순", "제목순", "noopener", "lb-"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js, bad


def test_refs_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/refs.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/refs" in raw.decode("utf-8")


def test_refs_card_focus_ring_is_not_clipped():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.ref-card:focus-within\s*\{([^}]*)\}", css)
    assert rule and "box-shadow" in rule.group(1)
    link = re.search(r"\.ref-link:focus-visible\s*\{([^}]*)\}", css)
    assert link and "box-shadow: none" in link.group(1) and "outline-offset" in link.group(1)
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert 'role: "list"' in js and "COUNT_DEBOUNCE_MS" in js and "marginLeft" in js


def test_refs_page_review_fixes():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert "String(v.title || v.id)" in js                      # numeric titles must not break localeCompare
    assert "String(role)" in js                                 # unknown role shown as-is
    assert js.count("sortByStart((") == 2                      # band + detail section list
    assert "params.id" in js.split("function loadError")[1].split("// ---- list")[0]   # back link only in detail


def test_refs_edit_mode_tokens():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    for token in ["라벨 고치기", "저장", "취소", "/labels", "ref_changed", "다시 불러오기",
                  "분석 당시 기록", "직접 고침", "(없음)", "hash"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js
    assert "http://" not in js and "https://" not in js


def _native_calls_with_null(src: str) -> list[str]:
    """replaceChildren/.append calls whose own (depth 0) arguments contain `: null` (native DOM renders it as the text "null")."""
    bad = []
    for m in re.finditer(r"(?:replaceChildren|\.append|\.prepend)\(", src):
        i, depth, top = m.end(), 1, []
        while depth and i < len(src):
            ch = src[i]
            depth += (ch in "([{") - (ch in ")]}")
            if depth == 1:
                top.append(ch)
            i += 1
        if re.search(r":\s*null\b", "".join(top)):
            bad.append(src[m.start():m.start() + 60])
    return bad


def test_pages_do_not_pass_null_to_native_dom_calls():
    for path in sorted((STATIC_DIR / "pages").glob("*.js")):
        assert _native_calls_with_null(path.read_text(encoding="utf-8")) == [], path.name
    refs = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert "fill(S.el," in refs and "export function fill" in (STATIC_DIR / "dom.js").read_text(encoding="utf-8")


def test_refs_edit_mode_focus_and_quiet_reload():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    for token in ["quiet: true", "scrollTo(", "opts.focus", ".focus(", "controls", "keep"]:
        assert token in js, token
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert re.search(r"max-width: 520px\) \{[^@]*\.ref-edit-row", css)


def test_typify_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'route: "#/typify"' in app
    assert 'soon: "typify"' not in app
    assert 'case "typify"' in app
    assert '"#/typify"' in home


def test_typify_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "typify.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/typify", "/api/typify/run", "/api/typify/save", "kind=typify",
                  "유형화 실행", "저장", "typify_changed", "invalid_notes", "레퍼런스 추가로 가기",
                  "이전 이름 그대로", "lb-", "#/refs/"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js, bad


def test_typify_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/typify.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/typify" in raw.decode("utf-8")


def test_typify_verdict_labels_match_server_and_edits_survive_rerun():
    js = (STATIC_DIR / "pages" / "typify.js").read_text(encoding="utf-8")
    from scripts.cluster import VERDICT_KO
    for key, label in VERDICT_KO.items():
        assert f'{key}: "{label}"' in js, key
    for token in ["carry", "editedNotes", "renderAlerts", "data-discard-yes", "data-rerun-yes", "S.preview.titles", "applied.titles"]:
        assert token in js, token
    assert "confirm(" not in js.replace("inline-confirm", "").replace("renderConfirm", "")

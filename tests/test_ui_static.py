import re

from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401  (fixture 재사용)

MENU_NAMES = ["홈", "레퍼런스 추가", "레퍼런스 목록", "공용 라이브러리", "유형화", "리포트", "모티브", "기획안", "콘티", "진단", "미션", "가이드"]
# yt-draft is no longer a terminal-only item: the draft screen is a live route.
# diagnose is live too: its terminal command no longer appears in the menu definition.
# mission and report are live too: no menu item shows a terminal command any more.


def test_index_loads_modules(client):
    status, _, raw = client.call("GET", f"/?key={client.app.token}", token=False)
    assert status == 200
    html = raw.decode("utf-8")
    assert re.search(r'<script[^>]*type="module"[^>]*src="/static/app\.js"', html) or \
        re.search(r'<script[^>]*src="/static/app\.js"[^>]*type="module"', html)
    assert "/static/app.css" in html


def test_static_files_served(client):
    for rel in ["app.css", "app.js", "api.js", "pages/home.js", "pages/placeholder.js", "pages/add.js",
                "help-panel.js", "help-utils.js", "progress-utils.js", "pages/guide.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200, rel
    status, headers, _ = client.call("GET", "/static/app.js")
    assert "javascript" in headers["Content-Type"]


def test_menu_definition_has_all_items():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in MENU_NAMES:
        assert f'"{name}"' in text, name
    menu_block = text.split("const MENU = [", 1)[1].split("];", 1)[0]
    assert "soon:" not in menu_block                      # every menu item opens a live screen
    assert menu_block.count("route:") == len(MENU_NAMES)


def test_no_external_resources():
    for path in STATIC_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8").replace('"http://www.w3.org/2000/svg"', "")  # SVG 이름공간은 주소가 아니다
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
        "스토리보드 HTML", "CapCut용 자막(SRT) 받기", "/api/conti/srt", "download", "exclude", "hash", "DOMParser", "filesUrl", "clearTimeout", "aria-live", "noopener",
    ]:
        assert token in text, token
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


def test_conti_srt_fit_contract_and_no_path_storage():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in ["/api/conti/srt-fit", "kind=srtfit", "편집한 영상 파일 경로", "실제 컷에 맞춰 SRT 받기",
                  "대사도 맞춰 받기", "컷을 찾는 중…", " 받기", "download"]:
        assert token in text, token
    # The personal video path must never be persisted: the srt-fit section may not touch any storage.
    start = text.index("// ---- srt fit")
    end = text.index("// ---- end srt fit")
    section = text[start:end]
    for banned in ["localStorage", "sessionStorage", "innerHTML", "confirm(", "alert(", "prompt(", "http://", "https://"]:
        assert banned not in section, banned


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


def test_motif_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'route: "#/motif"' in app
    assert 'soon: "motive"' not in app
    assert 'case "motif"' in app


def test_motif_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "motif.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/motif", "/api/motif/run", "/api/motif/save", "/api/motif/star",
                  "kind=motif", "새 카드 뽑기", "선택한 카드 저장", "저장하면서 ★", "기획안 만들기",
                  "yt-inspire", "#/typify", "#/add", "#/refs/",
                  "프로필이 없어 카드가 일반적으로 나옵니다", "유형화를 먼저 하면 흐름·조합 카드가 나옵니다",
                  "먼저 레퍼런스를 넣어 주세요", "즐겨찾기만", "res.warning", "job_id: mine.jobId"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js, bad
    for bad in ["confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad


def test_motif_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/motif.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/motif" in raw.decode("utf-8")


def test_draft_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'route: "#/draft"' in app
    assert 'soon: "draft"' not in app
    assert 'case "draft"' in app
    assert '"#/draft"' in home
    assert "#/draft/view/" in home


def test_draft_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/draft/sources", "/api/draft/list", "/api/draft/run",
                  "/api/draft/save", "kind=draft", "job_id", "기획안 만들기", "샷 추가", "샷 길이 합",
                  "콘티 보기", "콘티 만들기", "#/conti/", "#/motif", "#/typify", "익스트림 클로즈업"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js, bad
    for bad in ["confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad
    assert "http://" not in js and "https://" not in js


def test_draft_page_has_polish_flow_and_stays_safe():
    js = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    for token in ["훅·제목·썸네일 다듬기", "자막 읽기", "/api/polish/run", "/api/polish/save", "kind=polish", "draft_changed",
                  "기획안에 반영", "지금 기획안", "콘티는 이전 훅 그대로", "fieldset", "legend",
                  "/api/polish/lint", "점수를 다시 계산하지 못했습니다", "job_id", "markHandled",
                  "최신 기획안 기준으로 저장합니다", "덮어써집니다", "결과를 읽지 못했습니다"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML"]:
        assert bad not in js, bad
    for bad in ["confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad
    assert "http://" not in js and "https://" not in js


def test_motif_links_to_draft():
    js = (STATIC_DIR / "pages" / "motif.js").read_text(encoding="utf-8")
    assert "#/draft/new/" in js and "기획안 명령 복사" not in js


def test_draft_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/draft.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/draft" in raw.decode("utf-8")

def test_draft_shot_sizes_match_server_and_invalid_input_is_blocked():
    import json
    from scripts.features import SHOT_SIZES
    js = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    literal = re.search(r"const SHOT_SIZES = (\[[^\]]*\]);", js).group(1)
    assert json.loads(literal) == SHOT_SIZES
    for token in ["크기 선택", "의 크기를 고르세요", "목표 길이는 숫자로 적어 주세요", "validity.badInput"]:
        assert token in js, token
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".dr-sec .table-wrap { max-height: none; }" in css



def test_md_renderer_is_safe_and_wired_into_detail_screens():
    md = (STATIC_DIR / "md.js").read_text(encoding="utf-8")
    assert "export function renderMarkdown" in md and "export function parseMarkdown" in md
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "new Function",
                "http://", "https://", "confirm(", "alert(", "prompt("]:
        assert bad not in md, bad
    for token in ['protocol === "https:"', "new URL(", "noopener", "youtu.be"]:
        assert token in md, token
    for page in ("refs.js", "draft.js"):
        js = (STATIC_DIR / "pages" / page).read_text(encoding="utf-8")
        assert re.search(r'import \{[^}]*\brenderMarkdown\b[^}]*\} from "/static/md\.js"', js), page
        assert "renderMarkdown(part.text" in js, page


def test_md_css_covers_rendered_elements():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for selector in [".md h3", ".md blockquote", ".md pre", ".md hr", ".md ul", ".md table", ".md-table-wrap"]:
        assert selector in css, selector


def test_md_served(client):
    status, headers, raw = client.call("GET", "/static/md.js")
    assert status == 200 and "javascript" in headers["Content-Type"]


def test_md_table_cells_scroll_instead_of_breaking_and_draft_drops_repeated_title():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.md th, \.md td \{[^}]*word-break: keep-all[^}]*min-width: 4em", css)
    draft = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    assert "dropTitle: String(d.title)" in draft


def test_diagnose_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'route: "#/diagnose"' in app
    assert 'soon: "diagnose"' not in app
    assert 'case "diagnose"' in app
    assert '"#/diagnose"' in home
    assert "#/soon/diagnose" not in home


def test_diagnose_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "diagnose.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/diagnose/targets", "/api/diagnose/compare",
                  "/api/diagnose/explain", "/api/diagnose/save", "/api/diagnose/intentional",
                  "/api/diagnose/record", "kind=diagnose", "job_id", "비교하기", "설명 받기",
                  "일부러 다르게 한 거예요", "renderMarkdown", "/yt-coach:yt-mission today",
                  "aria-pressed", "data-discard-yes", "aborted", "axis_ko", "정보 없음", "예비 진단",
                  "설명 다시 받기", "refreshCategories", "진단을 기록했습니다", "#/typify", "#/add", "#/draft"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad
    assert not re.search(r"https?://", js)


def test_diagnose_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/diagnose.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/diagnose/targets" in raw.decode("utf-8")


def test_diagnose_fix_round_contract():
    js = (STATIC_DIR / "pages" / "diagnose.js").read_text(encoding="utf-8")
    for token in ["일부러 다르게 한 거예요: ${name}", "갭이 바뀌었습니다 — 설명 다시 받기 후 저장하세요",
                  "저장된 설명이 지금 갭과 다릅니다 — 설명 다시 받기", "다시 비교", "일부러 다르게 한 갭",
                  "dropTitle", "aria-disabled", "cmpSeq", "watchOtherJob", "compareNow(true)"]:
        assert token in js, token


def test_mission_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'route: "#/mission"' in app
    assert 'soon: "mission"' not in app
    assert 'case "mission"' in app
    assert '"#/mission"' in home
    assert "#/soon/mission" not in home


def test_mission_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "mission.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "isCurrent()", "ctx.api(",
                  "/api/mission", "/api/mission/draft", "/api/mission/save", "/api/mission/feedback",
                  "/api/mission/submit", "/api/mission/skip", "kind=mission", "kind=mission_feedback", "job_id",
                  "오늘 미션 받기", "이걸로 할게요", "이걸로 바꿀게요", "다른 걸로", "완료 제출", "피드백 받기",
                  "건너뛰기", "바꾸기", "renderMarkdown", "dropTitle", "fieldset", "legend", "mission_exists",
                  "bad_file", "already_submitted", "aborted", "markHandled", "/yt-coach:yt-init",
                  "날짜가 바뀌었습니다", "다시 제출하면 이전 제출을 덮어씁니다", "원본은 건드리지 않고",
                  "adjust", "role: \"alert\"", "scope"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad
    assert not re.search(r"https?://", js)


def test_mission_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/mission.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/mission/draft" in raw.decode("utf-8")


def test_report_menu_is_live_route():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'route: "#/report"' in app
    assert 'soon: "report"' not in app
    assert 'case "report"' in app
    assert '"#/report"' in home


def test_report_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "report.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory", "isCurrent()",
                  "ctx.api(", "/api/report/list", "/api/report/build", "/api/review/weeks", "/api/review",
                  "/api/review/write", "/api/review/save", "/api/review/apply", "kind=review", "job_id",
                  "리포트 만들기", "새 탭에서 열기", "sandbox", "회고 쓰기", "다시 쓰기", "프로필에 반영",
                  "tablist", "aria-selected", "ArrowRight", "renderMarkdown", "dropTitle", "markHandled",
                  "aborted", "/yt-coach:yt-init", "#/typify", "이번 주 미션 기록이 없습니다", "회고 있음"]:
        assert token in js, token
    assert "allow-scripts" not in js
    assert re.search(r'sandbox:\s*""', js)
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "confirm(", "alert(", "prompt("]:
        assert bad not in js.replace("inline-confirm", ""), bad
    assert not re.search(r"https?://", js)


def test_report_page_served(client):
    status, headers, raw = client.call("GET", "/static/pages/report.js")
    assert status == 200
    assert "javascript" in headers["Content-Type"]
    assert "/api/report/build" in raw.decode("utf-8")


def test_conti_caption_video_contract():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in ["/api/conti/caption-video", "/api/captions/check", "kind=caption_video", "자막 영상 만들기",
                  "움직이는 자막 영상", "크로마키", "ytc captions setup", "download", "aria-live",
                  "/cancel", "멈추기", "멈췄습니다", "다시 확인", "컷을 찾는 중", ".focus()"]:
        assert token in text, token
    section = text[text.index("// ---- caption video"):text.index("// ---- end caption video")]
    for banned in ["localStorage", "sessionStorage", "innerHTML", "confirm(", "alert(", "prompt(", "http://", "https://"]:
        assert banned not in section, banned


# ---- 공용 라이브러리 화면 -----------------------------------------------------------------

def test_library_menu_route_and_page_served(client):
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'route: "#/library"' in app and 'case "library"' in app
    status, headers, raw = client.call("GET", "/static/pages/library.js")
    assert status == 200 and "javascript" in headers["Content-Type"]
    assert "/api/library/status" in raw.decode("utf-8")


def test_library_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "library.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "renderMarkdown", "clearTimeout", "encodeURIComponent",
                  "/api/library/connect", "/api/library/pull", "/api/library/items", "/api/library/preview",
                  "/api/library/summarize", "/api/library/confirm", "/api/library/discard",
                  "/api/jobs/current?kind=library_summary", "ytc.library.handled", "ytc.motif.library_ids",
                  "GitHub 저장소 주소 (git@… 또는 깃허브 주소)", "모티브 재료로 쓰기", "이 틀로 기획안 만들기",
                  "섹션 요약 받기", "내가 올림", "잘 먹힘", "올리기 확인", "그래도 새로 올리기", "이미 올린 항목입니다",
                  "push_unknown", "role: \"tablist\"", "aria-selected"]:
        assert token in js, token
    for banned in ["innerHTML", "insertAdjacentHTML", "outerHTML", "window.confirm", "confirm(", "alert(", "prompt(",
                   "http://", "https://", "eval(", "new Function"]:
        assert banned not in js, banned


def test_library_upload_only_runs_from_a_click_on_the_confirm_buttons():
    js = (STATIC_DIR / "pages" / "library.js").read_text(encoding="utf-8")
    assert js.count("/api/library/confirm") == 1                       # one place sends the upload
    calls = [ln for ln in js.splitlines() if re.search(r"(?<![A-Za-z])upload\(", ln) and "async function upload" not in ln]
    assert calls, "upload must be reachable"
    for ln in calls:
        assert 'addEventListener("click"' in ln, ln                   # never from mount, polling or a timer
    assert "askUpload" in js and "data-upload-yes" in js and "data-dup-yes" in js


def test_motif_page_has_library_chooser_that_sends_library_ids():
    js = (STATIC_DIR / "pages" / "motif.js").read_text(encoding="utf-8")
    for token in ["ytc.motif.library_ids", "/api/library/status", "/api/library/items?kind=technique",
                  "library_ids", "지인 기법", "sessionStorage"]:
        assert token in js, token
    assert "if (!st.connected) return null" in js                      # no chooser unless the library is connected


def test_draft_page_has_friend_template_optgroup():
    js = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    for token in ["library_templates", "optgroup", "지인 틀", "x.source"]:
        assert token in js, token


def test_api_error_carries_response_body():
    js = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    assert "data," in js                                               # library screen reads shared_id / push_unknown



def test_api_error_object_includes_response_body_and_library_reads_it():
    api = (STATIC_DIR / "api.js").read_text(encoding="utf-8")
    assert re.search(r"const err = \{\s*status: res\.status,.*?\n\s+data,", api, re.S)
    lib = (STATIC_DIR / "pages" / "library.js").read_text(encoding="utf-8")
    assert "e.data || {}" in lib and "data.push_unknown" in lib


def test_library_review_fixes_present():
    lib = (STATIC_DIR / "pages" / "library.js").read_text(encoding="utf-8")
    for token in ['ev.key === "Home"', 'ev.key === "End"', "mineSeq", "data-summarize-btn", "data-summary",
                  "getElementById(`lib-detail-"]:
        assert token in lib, token
    assert '"lib-list", "aria-live"' not in lib                         # live region is the count paragraph only
    select = lib[lib.index("function selectTab"):lib.index("function renderPanel")]
    assert ".focus()" in select                                          # focus follows the rebuilt tab
    draft = (STATIC_DIR / "pages" / "draft.js").read_text(encoding="utf-8")
    for token in ["공용 라이브러리가 연결되지 않아 지인 틀을 쓸 수 없습니다", "이 지인 틀을 찾을 수 없습니다", "#/library"]:
        assert token in draft, token
    render_new = draft[draft.index("function renderNew"):draft.index("function renderBody")]
    assert render_new.index("sourceNoteNode()") < render_new.index("고를 수 있는 출처가 없습니다")   # note also when !any


def test_app_routes_guide():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'case "guide"' in app
    assert "/static/help-panel.js" in app
    menu_utils = (STATIC_DIR / "menu-utils.js").read_text(encoding="utf-8")
    assert '"#/guide"' in menu_utils.split("PREFIX_ROUTES = [", 1)[1].split("]", 1)[0]  # 하위 탭에서도 메뉴가 켜진다
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="help-panel"' in html


def test_help_panel_contract():
    panel = (STATIC_DIR / "help-panel.js").read_text(encoding="utf-8")
    for needle in ["helpPage", "trimHistory", "createHelpPanel", "ytc.helpOpen", "/api/help/chat", "/cancel",
                   "aria-expanded", "aria-live", 'role: "tablist"', "isComposing", '"Escape"']:
        assert needle in panel, needle
    assert "innerHTML" not in panel


def test_guide_page_contract():
    guide = (STATIC_DIR / "pages" / "guide.js").read_text(encoding="utf-8")
    for needle in ["export async function mount", "export function unmount", "guide-shorts", "guide-long",
                   "/api/status", "isCurrent"]:
        assert needle in guide, needle
    assert "innerHTML" not in guide


def test_help_panel_restores_question_on_send_failure():
    panel = (STATIC_DIR / "help-panel.js").read_text(encoding="utf-8")
    assert "job_running" in panel
    assert "앞 질문을 정리하는 중입니다" in panel
    assert "input.value = q" in panel


def test_help_panel_resets_chat_on_category_change():
    panel = (STATIC_DIR / "help-panel.js").read_text(encoding="utf-8")
    on_category = panel[panel.index("function onCategory"):panel.index("function toggle")]
    assert "resetChat()" in on_category
    assert "return { setPage, onCategory, toggle }" in panel
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    set_category = app[app.index("setCategory(name) {"):app.index("refreshCategories,\n};")]
    assert "helpPanel.onCategory(ctx.category)" in set_category


def test_help_panel_sits_at_right_edge():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.help-panel \{([^}]*)\}", css).group(1)
    assert "margin-left: auto" in rule


def test_add_page_step_progress_wiring():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "/static/progress-utils.js" in js
    assert "clearInterval" in js and "stopClock()" in js
    for needle in ['"progressbar"', "aria-valuenow", "aria-valuetext", "확인을 기다리는 중", "예상보다 길어지는 중", "곧 끝날 예정"]:
        assert needle in js, needle
    assert ".is-over .bar-fill" in css and "prefers-reduced-motion" in css


def test_add_page_step_rows_use_own_state_classes():
    # 가이드 단계(.step-done/.step-current)와 겹치지 않게 줄 상태는 row-* 클래스를 쓴다.
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "step-${row.state}" not in js and "row-${row.state}" in js
    for sel in [".row-done .step-time", ".row-failed .step-time", ".row-pending .step-name", ".row-done .bar-fill"]:
        assert sel in css, sel
    assert not re.search(r"\.step-(done|failed|pending|skipped) \.(step-time|step-name|bar-fill)", css)


def test_add_page_over_text_and_shown_percent():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    paint = js[js.index("function paintStepRow"):js.index("function stepRows")]
    assert "v.byFraction" in paint
    assert "pctOf(row)" not in paint                     # % 글자와 aria-valuenow는 보이는 막대 값을 쓴다
    assert paint.index("monotonicFraction") < paint.index("rowText(")


# --- 사이드바: 어두운 차콜, 아이콘 머리, 그룹 상자, 접고 펴기 ---------------------

def _root_tokens(css):
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    light = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", css[:dark_at]))
    dark = dict(light, **dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", css[dark_at:css.index("}", dark_at)])))
    return light, dark


def test_sidebar_text_contrast_on_charcoal():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    light, dark = _root_tokens(css)
    for tokens in (light, dark):           # 사이드바 색은 두 모드에서 같아야 하고 글자 대비는 4.5 이상
        for bg in ("--side-bg", "--side-grp", "--side-on-bg", "--side-hover"):
            for fg in ("--side-ink", "--side-sub"):
                assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)
        for bg in ("--side-bg", "--side-grp", "--side-on-bg", "--side-hover"):   # 현재 막대·포커스 테두리(비글자)
            assert _contrast(tokens["--side-accent"], tokens[bg]) >= 3.0, bg
    assert {k: v for k, v in light.items() if k.startswith("--side-")} == \
        {k: v for k, v in dark.items() if k.startswith("--side-")}


def test_light_page_text_contrast():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    light, dark = _root_tokens(css)
    for tokens in (light, dark):
        for bg in ("--page", "--surface"):
            for fg in ("--ink", "--ink2", "--muted"):
                assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)


def test_sidebar_groups_are_collapsible_buttons_with_icons():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    menu_block = app.split("const MENU = [", 1)[1].split("];", 1)[0]
    assert menu_block.count("icon:") == 6                 # 홈, 모으기, 분석, 만들기, 성장, 가이드
    imports = re.search(r'import \{([^}]*)\} from "/static/menu-utils\.js"', app).group(1)
    for name in ("activeGroup", "enterExpand", "isActive", "parseCollapsed", "toggleCollapsed"):
        assert name in imports, name
    assert '"aria-expanded"' in app and '"aria-controls"' in app
    assert 'const SVG_NS = "http://www.w3.org/2000/svg"' in app and "createElementNS(SVG_NS" in app
    assert "ytc.menu.collapsed" in app
    assert re.search(r"try \{ return parseCollapsed\(localStorage\.getItem\(MENU_KEY\)\); \} catch \{ return \[\]; \}", app)
    assert re.search(r"try \{ localStorage\.setItem\(MENU_KEY", app)


def test_sidebar_styles_use_side_tokens():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    menu_rule = re.search(r"^\.menu \{([^}]*)\}", css, re.M).group(1)
    assert "background: var(--side-bg)" in menu_rule
    focus = re.search(r"\.menu-group-toggle:focus-visible[^{]*\{([^}]*)\}", css).group(1)
    assert "box-shadow: var(--side-focus)" in focus
    assert "outline: 2px solid transparent" in focus          # 고대비(forced-colors) 모드에서 테두리가 남는다
    assert "--side-focus: 0 0 0 2px var(--side-accent)" in css
    assert re.search(r"\.menu-group\.closed \.menu-list\s*\{[^}]*display: none", css)

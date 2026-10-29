import re

from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401  (fixture 재사용)

# Every screen (name + route): app.js SCREENS, the same list the help chatbot gets (api_help.MENU).
MENU_NAMES = ["홈", "레퍼런스", "공용 라이브러리", "유형", "모티브", "내 영상", "기획안", "콘티", "진단", "오늘 미션",
              "주간 리포트", "가이드"]   # the sidebar's names


def test_index_loads_modules(client):
    status, _, raw = client.call("GET", f"/?key={client.app.token}", token=False)
    assert status == 200
    html = raw.decode("utf-8")
    assert re.search(r'<script[^>]*type="module"[^>]*src="/static/app\.js"', html) or \
        re.search(r'<script[^>]*src="/static/app\.js"[^>]*type="module"', html)
    assert "/static/app.css" in html


def test_static_files_served(client):
    for rel in ["app.css", "app.js", "api.js", "pages/home.js", "pages/placeholder.js", "pages/add.js",
                "help-panel.js", "help-utils.js", "progress-utils.js", "pages/guide.js", "status-chip.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200, rel
    status, headers, _ = client.call("GET", "/static/app.js")
    assert "javascript" in headers["Content-Type"]


def _home_js():
    return "\n".join((STATIC_DIR / rel).read_text(encoding="utf-8") for rel in ("pages/home.js", "home-utils.js"))


def test_screen_list_has_all_routes_and_names_the_page():
    text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    block = text.split("const SCREENS = [", 1)[1].split("];", 1)[0]
    assert re.findall(r'name: "([^"]+)"', block) == MENU_NAMES
    assert "soon:" not in block                           # every screen is a live route
    assert "document.title" in text                       # the browser tab names the current screen


def test_shell_markup_sidebar_tabbar_and_slots():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="menu"' in html and 'aria-label="주 메뉴"' in html
    assert re.search(r'<nav id="tabbar" class="tabbar" aria-label="하단 메뉴"', html)
    assert 'id="subnav"' in html
    assert 'id="scrim"' not in html                       # no drawer any more: phones use the bottom tabs
    assert '<meta name="color-scheme" content="light">' in html
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    imports = re.search(r'import \{([^}]*)\} from "/static/menu-utils\.js"', app).group(1)
    for name in ("SIDEBAR", "PHONE_TABS", "isActive", "phoneTab", "videoProgress", "sidebarVideos", "subnavItems",
                 "isVideoActive", "videoLinks"):
        assert name in imports, name
    assert 'const SVG_NS = "http://www.w3.org/2000/svg"' in app and "createElementNS(SVG_NS" in app
    # tab icons are inline stroke SVGs, one per bottom tab
    for icon in ("home:", "spark:", "film:", "target:"):
        assert icon in app.split("const ICONS = {", 1)[1].split("};", 1)[0], icon
    # header: logo, category, the status-chip slot (filled later), help menu; no local path
    assert 'id: "status-chip"' in app
    assert "work_root" not in app and "work-root" not in app
    assert "menu-toggle" not in app and "drawer" not in app
    # 서버 끄기 lives in the help menu
    menu = app[app.index("function helpMenu("):app.index("// ---- router")]
    for text in ('"도움말 열기"', '"사용 가이드"', '"서버 끄기"', '"aria-expanded"', '"Escape"'):
        assert text in menu, text
    assert "ytc ui" not in app and "터미널" not in app
    # sidebar counts come from existing APIs only
    for path in ("/api/status", "/api/typify", "/api/motif"):
        assert path in app, path
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    phone = "\n".join(_media_blocks(css, 640))
    assert "display: none" in _rule(phone, ".menu")
    assert "display: grid" in _rule(phone, ".tabbar")
    assert re.search(r"^\.tabbar \{[^}]*display: none", css, re.M)            # desktop: no bottom tabs
    assert re.search(r"^\.menu \{[^}]*background: var\(--surface\)", css, re.M)  # white sidebar
    width = re.search(r"--menu-w: (\d+)px", css).group(1)
    assert 220 <= int(width) <= 260


SPEC_TOKENS = {
    "--bg": "#F5F6F8", "--surface": "#FFFFFF", "--ink": "#15171C", "--ink-2": "#3D424D", "--muted": "#5B616E",
    "--border": "#E3E6EB", "--border-strong": "#D5D9E0", "--rec": "#C8322B", "--done": "#1F7A4D", "--link": "#2F5FD0",
    "--kind-flow-bg": "#E8EEFB", "--kind-flow-fg": "#1E449E", "--kind-tech-bg": "#E3F3EA", "--kind-tech-fg": "#155C39",
    "--kind-angle-bg": "#FBF0DC", "--kind-angle-fg": "#7A4800", "--kind-combo-bg": "#EFE8FB",
    "--kind-combo-fg": "#4E2C93", "--sec-hook-bg": "#FBE6E4", "--sec-hook-fg": "#8E231E",
    "--sec-check-bg": "#FBF0DC", "--sec-check-fg": "#7A4800", "--radius-card": "10px", "--radius-ctl": "8px",
}


def test_tokens_match_spec_and_dark_theme_is_shoot_mode_only():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    for name, value in SPEC_TOKENS.items():
        assert f"{name}: {value};" in root, name
    assert "'Noto Sans KR'" in root and "'JetBrains Mono'" in root
    assert "prefers-color-scheme" not in css
    assert re.search(r"^html \{ color-scheme: light; \}", css, re.M)
    shoot = _root_tokens(css)[1]
    assert shoot["--bg"].upper() == "#0E1013"
    # pages keep using the older names; they point at the spec tokens so both themes follow
    for old, new in (("--page", "--bg"), ("--ink2", "--ink-2"), ("--grid", "--border"), ("--ok", "--done")):
        assert f"{old}: var({new});" in css, old


def test_tag_and_tab_colors_from_tokens_with_contrast():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    light, _ = _root_tokens(css)
    kinds = {"flow": "kind-flow", "tech": "kind-tech", "angle": "kind-angle", "combo": "kind-combo",
             "hook": "sec-hook", "check": "sec-check", "intro": "kind-flow", "body": "kind-tech", "outro": "kind-combo"}
    for kind, token in kinds.items():
        rule = re.search(rf"\.ui-tag-{kind}[^{{]*\{{([^}}]*)\}}", css).group(1)
        assert f"var(--{token}-bg)" in rule and f"var(--{token}-fg)" in rule, kind
        assert _contrast(light[f"--{token}-fg"], light[f"--{token}-bg"]) >= 4.5, kind
    assert _contrast(light["--on-rec"], light["--rec"]) >= 4.5
    assert _contrast(light["--link"], light["--surface"]) >= 4.5
    assert "var(--rec)" in re.search(r'\.ui-tab\[aria-selected="true"\] \{([^}]*)\}', css).group(1)
    assert "var(--pick-bg)" in re.search(r'\.ui-chip\[aria-pressed="true"\] \{([^}]*)\}', css).group(1)
    # desktop buttons 32px / 13px; main actions on phones at least 44px
    btn = re.search(r"^\.btn \{([^}]*)\}", css, re.M).group(1)
    assert "min-height: 32px" in btn and "font-size: 13px" in btn
    phone = "\n".join(_media_blocks(css, 640))
    assert "min-height: 44px" in _rule(phone, ".btn-primary, .btn-rec")


# Strings shown to the user must not carry file names, local paths or terminal commands.
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`')
_HIDDEN = (".md", "-conti.html", "/yt-coach:", "workspace/", "work_root", "ytc ")
# conti.js is edited on another branch: its captions setup command stays until the conti tab is rebuilt.
_ALLOWED = {("conti.js", "ytc captions setup")}


def test_pages_hide_paths_file_names_and_commands():
    bad = []
    for path in sorted((STATIC_DIR / "pages").glob("*.js")):
        text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("//"))
        for lit in _STRING.findall(text):
            if any(h in lit for h in _HIDDEN) and (path.name, lit.strip("\"'`")) not in _ALLOWED:
                bad.append((path.name, lit[:80]))
    assert bad == []
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert "st.work_root" not in home and "m.path" not in home     # no file path on the mission card
    for name in ("refs.js", "draft.js"):
        js = (STATIC_DIR / "pages" / name).read_text(encoding="utf-8")
        assert "md 파일" not in js and "경로 복사" not in js, name
    assert "res.trash}" not in "".join(p.read_text(encoding="utf-8") for p in (STATIC_DIR / "pages").glob("*.js"))


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
    "샷 분류도 함께", "영상마다 Claude 호출이 한 번 더 일어나요", "다시 분석", "자막·컷 새로 받기",
    "자막이 없을 때", "음성 인식(기본)", "화면만 보고 분석", "건너뛰기", "카테고리에 넣습니다",
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
                  "clearTimeout", "레퍼런스 화면에서 영상을 열어 고칠 수 있어요", "/review/"]:
        assert token in text, token
    for gone in ["/decision", "save_all", "남은 영상 모두 저장", "음성 인식(STT)으로"]:
        assert gone not in text, gone
    for banned in ["innerHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in text, banned


def test_menu_has_one_refs_entry_and_add_route_opens_refs():
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '"레퍼런스 추가"' not in app and '"레퍼런스 목록"' not in app
    assert 'case "add": return { page: "refs", params: { add: true } }' in app
    menu = (STATIC_DIR / "menu-utils.js").read_text(encoding="utf-8")
    assert '"#/add"' in menu                                  # #/add also lights the 레퍼런스 menu


def test_add_area_is_a_component_of_refs_not_a_route_page():
    refs = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    add = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert re.search(r'import \{[^}]*mountAddArea[^}]*\} from "/static/pages/add\.js"', refs)
    assert "export function mountAddArea" in add and "export async function mount(" not in add
    for token in ["+ 링크 추가", '"aria-expanded"', '"aria-controls"', "/api/add/shots-pending", "샷 분류 보강 (",
                  "startShotsOnly"]:
        assert token in refs, token
    for token in ["/api/add/check", '"details"', "no_transcript", "startLabel(", "aria-live", "/static/add-utils.js"]:
        assert token in add, token
    assert "add-category" not in add and add.count("setCategory") == 1     # 카테고리는 위쪽 선택칸 하나만 쓴다("보기"만 그 칸을 바꾼다)
    assert "300" in add                                           # check debounce


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
        "스토리보드 HTML", "CapCut용 자막(SRT) 받기", "/api/conti/srt", "/api/conti/trash", "콘티 삭제", "workspace/trash/", "download", "exclude", "hash", "DOMParser", "filesUrl", "clearTimeout", "aria-live", "noopener",
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
    for tokens in _root_tokens(css):
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
                  "isCurrent()", "ctx.api(", "/api/refs",
                  "최근 추가순", "길이순", "제목순", "noopener", "lb-",
                  "/trash?category=", "레퍼런스", "aria-expanded", "workspace/trash/"]:
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
    assert 'role: "list"' in js and "COUNT_DEBOUNCE_MS" in js


def test_refs_page_review_fixes():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert "String(v.title || v.id)" in js                      # numeric titles must not break localeCompare
    assert "String(role)" in js                                 # unknown role shown as-is
    assert js.count("sortByStart((") == 1                      # detail section list (the band moved to section-band.js)
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
    home = _home_js()                                 # the page and its helpers
    assert 'route: "#/typify"' in app
    assert 'soon: "typify"' not in app
    assert 'case "typify"' in app
    assert '"#/typify"' in home


def test_typify_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "typify.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/typify", "/api/typify/run", "/api/typify/save", "kind=typify",
                  "유형화 실행", "저장", "typify_changed", "invalid_notes", "레퍼런스 넣기",
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
                  "kind=motif", "새 카드 뽑기", "선택한 카드 저장", "저장하면서 ★", "이 카드로 새 영상 시작",
                  "기법 모음 정리해줘", "#/typify", "#/add", "#/refs/",
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
    home = _home_js()                                 # the page and its helpers
    assert 'route: "#/draft"' in app
    assert 'soon: "draft"' not in app
    assert 'case "draft"' in app
    assert '"#/motif"' in home                        # a new video starts from a motif card (spec 4.3)
    assert "videoHref(videoId(" in home               # in-progress videos open the video workspace
    assert "#/draft/view/" not in home


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
    for token in ["크기 선택", "의 크기를 고르세요", "목표 길이는 숫자로 적어 주세요", "validity.badInput",
                  "/api/draft/trash", "기획안 삭제", "workspace/trash/", "aria-expanded"]:
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
    home = _home_js()                                 # the page and its helpers
    assert 'route: "#/diagnose"' in app
    assert 'soon: "diagnose"' not in app
    assert 'case "diagnose"' in app
    assert 'videoHref(id, "diagnose")' in home       # diagnosing opens the video's 진단 tab
    assert "#/soon/diagnose" not in home


def test_diagnose_page_contract_and_safety():
    js = (STATIC_DIR / "pages" / "diagnose.js").read_text(encoding="utf-8")
    for token in ["export async function mount", "export function unmount", "export function onCategory",
                  "isCurrent()", "ctx.api(", "/api/diagnose/targets", "/api/diagnose/compare",
                  "/api/diagnose/explain", "/api/diagnose/save", "/api/diagnose/intentional",
                  "/api/diagnose/record", "kind=diagnose", "job_id", "비교하기", "설명 받기",
                  "일부러 다르게 한 거예요", "renderMarkdown", "#/mission",
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
                  "bad_file", "already_submitted", "aborted", "markHandled", "처음 설정해줘",
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
    home = _home_js()                                 # the page and its helpers
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
                  "aborted", "처음 설정해줘", "#/typify", "이번 주 미션 기록이 없습니다", "회고 있음"]:
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


def test_conti_cut_editing_contract():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in ["/api/conti/cuts", "draggable", '"aria-haspopup"', '"여기에 장면 넣기"', '"되돌리기"', '"위로"', '"아래로"',
                  "fmtDelta(", "dropTarget(", "stepMove(", "canRemove(", "snapSec(", "장면 설명", "길이(초)", "샷 크기",
                  "위에 넣기", "아래에 넣기", "복제", "콘티가 바뀌었어요 — 다시 받아 주세요", "UNDO_MS"]:
        assert token in text, token
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "window.confirm", "confirm(", "alert(", "prompt(",
                   "http://", "https://"]:
        assert banned not in text, banned
    assert "/static/conti-utils.js" in text


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


# --- 색 토큰: 라이트 고정, 촬영 모드(.shoot-dark)만 어두운 값 ---------------------

def _token_block(css, selector):
    out = {}
    for m in re.finditer(rf"^{re.escape(selector)} \{{(.*?)^\}}", css, re.S | re.M):
        out.update(re.findall(r"(--[\w-]+):\s*([^;]+);", m.group(1)))
    return out


def _resolve(raw):
    def value(name, seen=()):
        v = raw[name].strip()
        m = re.fullmatch(r"var\((--[\w-]+)\)", v)
        if m and m.group(1) not in seen:
            return value(m.group(1), seen + (name,))
        return v
    return {k: value(k) for k in raw}


def _root_tokens(css):
    """(light, shoot) tokens with var() aliases resolved; aliases sit in a ":root, .shoot-dark" block."""
    base = _token_block(css, ":root")
    alias = _token_block(css, ":root, .shoot-dark")
    shoot = _token_block(css, ".shoot-dark")
    light = _resolve({**base, **alias})
    dark = _resolve({**base, **shoot, **alias})
    hexes = lambda d: {k: v for k, v in d.items() if re.fullmatch(r"#[0-9a-fA-F]{6}", v)}
    return hexes(light), hexes(dark)


def test_light_page_text_contrast():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for tokens in _root_tokens(css):
        for bg in ("--page", "--surface"):
            for fg in ("--ink", "--ink2", "--muted"):
                assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)


def test_sidebar_items_and_focus():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "--side-" not in css                                   # the charcoal sidebar is gone
    link = re.search(r'^\.menu-link\[aria-current="page"\] \{([^}]*)\}', css, re.M).group(1)
    assert "font-weight: 700" in link
    assert re.search(r"^\.vid-bar > span \{[^}]*background: var\(--done\)", css, re.M)
    light, _ = _root_tokens(css)
    assert _contrast(light["--muted"], light["--border"]) >= 4.5     # counts on the current (gray) item
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '"aria-current", "page"' in app
    # one current item: "전체 N편" (#/draft) matches only the list itself, not every draft screen
    assert "a.dataset.exact ? a.dataset.href === hash" in app and '"data-exact": "1"' in app
    assert '"aria-valuetext": `${stage} 단계 · ${pct}%`' in app
    # re-rendering on every route keeps keyboard focus on the same link
    side = app[app.index("function renderSidebar("):app.index("function renderTabbar(")]
    assert "document.activeElement" in side and ".focus()" in side
    # counts refresh on route changes and category switches only (no duplicate startup requests)
    refresh = app[app.index("async function refreshCategories("):app.index("// ---- icons")]
    assert "refreshSidebar()" not in refresh
    # the help menu closes when focus leaves it
    assert '"focusout"' in app[app.index("function helpMenu("):app.index("function fillHelpPop(")]
    assert 'role: "progressbar"' in app or '"progressbar"' in app



# --- 카드 새 모양: 한국어 유형 이름, 레퍼런스 카드 정보, 선택 상태 색 ---------------------

def test_pages_show_korean_type_names_not_codes():
    pages = STATIC_DIR / "pages"
    for name in ("add.js", "draft.js", "library.js", "refs.js"):
        js = (pages / name).read_text(encoding="utf-8")
        assert 'from "/static/labels.js"' in js, name
        assert not re.search(r"\$\{[\w.]*\.(hook_type|narrative_type)\}", js), name   # 영어 코드를 그대로 찍지 않는다
    refs = (pages / "refs.js").read_text(encoding="utf-8")
    assert "vocab.hook_type, hookName)" in refs and "vocab.narrative_type, narrativeName)" in refs   # 고치기 선택지도 한국어


def test_refs_card_shows_chips_tags_stats_and_flow():
    js = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    card = js.split("function thumb(v)", 1)[1].split("// ---- detail", 1)[0]
    for token in ["formatName(v.format)", 'VIEW.role === "all"', "직접 고침", "fmtTime(v.duration_sec)",
                  "hookName(v.hook_type)", "narrativeName(v.narrative_type)", "fmtCut(v.avg_cut_sec)",
                  "topCamera(v.camera)", "bandCaption(v.sections)", '"dl"', '"aria-labelledby": tid',
                  '"aria-describedby": `${tid}-d ${tid}-b`', 'band.setAttribute("aria-hidden", "true")']:
        assert token in card, token
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert re.search(r"@media \(forced-colors: active\) \{[^}]*aria-pressed", css)


def test_selected_states_use_pick_colors_with_contrast():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    light, dark = _root_tokens(css)
    for tokens in (light, dark):
        assert _contrast(tokens["--pick-fg"], tokens["--pick-bg"]) >= 4.5
        assert _contrast(tokens["--pick-bg"], tokens["--page"]) >= 3.0          # 선택된 버튼이 바탕과 구분된다
        assert _contrast(tokens["--ink2"], tokens["--chip"]) >= 4.5             # 꼬리표 글자
        assert _contrast(tokens["--muted"], tokens["--chip"]) >= 4.5
        assert _contrast(tokens["--muted"], tokens["--page"]) >= 4.5           # 숫자 칸(--page 바탕)의 이름
    for selector in ('.filter-btn[aria-pressed="true"]',
                     '.diag-toggle[aria-pressed="true"]', ".seg-opt input:checked + span",
                     '.lib-tab[aria-selected="true"]', '.ui-chip[aria-pressed="true"]'):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)
        assert "var(--pick-bg)" in rule and "var(--pick-fg)" in rule, selector
    assert "box-shadow: var(--elev-1)" in re.search(r"^\.card \{([^}]*)\}", css, re.M).group(1)


def test_pages_show_korean_section_names():
    pages = STATIC_DIR / "pages"
    for name in ("add.js", "draft.js", "library.js", "refs.js", "conti.js", "diagnose.js"):
        js = (pages / name).read_text(encoding="utf-8")
        assert 'from "/static/labels.js"' in js, name
        # 섹션 라벨·세부 유형을 글자로 찍는 곳은 이름 함수를 거친다 (lb-<label> 색 클래스만 코드 그대로)
        text = js.replace("lb-${s.label}", "")
        assert "${s.label}" not in text and "${s.sub}" not in text, name
        assert "cut.label}" not in text, name                    # 콘티 음성 안내도 이름 함수를 거친다
    assert "LABEL_KO" not in (pages / "diagnose.js").read_text(encoding="utf-8")   # 한 곳(labels.js)에서만 정의


def test_home_layout_tokens():
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    for token in ['"다음 할 일"', '"만들고 있는 영상"', '"오늘 미션"', '"최근 결과물"', '"/api/drafts/filmed"', '"/api/mission"',
                  '"aria-pressed"', '"draft_changed"', '"다시 불러오기"', "개 더 보기", 'from "/static/home-utils.js"',
                  'from "/static/shoot-utils.js"', "shootStore(videoId(", "recentLine(", "missionView(", "nextStep("]:
        assert token in home, token
    # Desktop main buttons go through primary() (init screen, next card); the phone next card has the one red button.
    assert home.count("btn-primary") == 1
    assert re.search(r'function primary\(tag, attrs, text\) \{\s*return h\(tag, \{ \.\.\.attrs, class: "btn btn-primary" \}', home)
    assert len(re.findall(r"(?<!function )\bprimary\(", home)) == 2
    assert home.count("btn-rec") == 1
    assert "innerHTML" not in home
    # no file names on the recent list, no category stage line / other categories (spec 4.1 lists four sections)
    assert "filesUrl(it.path)" not in home and "toLocaleString" not in home
    assert "cat-line" not in home and "otherCategories" not in home
    # focus key is taken out at the start of mount; peers of one video are locked together
    assert "const pendingFocus = focusAfter;" in home and "restoreFocus(el, pendingFocus, cat ? cat.videos : [])" in home
    restore = home[home.index("function restoreFocus("):home.index("function render(")]
    assert "isFolded(videos, target.path)" in restore
    toggle = home[home.index("async function toggleFilmed("):home.index("// ---- desktop")]
    assert re.search(r'const changed = e && e\.error === "draft_changed";\s*'
                     r'if \(!changed\) for \(const b of peers\) b\.disabled = false;', toggle)
    assert home.count('"data-film-path": video.path') == 2
    assert '", 다음 단계"' in home
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for sel in [".home-next", ".vid-steps", ".home-next-phone", ".home-pv", ".home-dot", ".home-thumb"]:
        assert re.search(rf"^{re.escape(sel)}[\s{{.,:\[]", css, re.M), sel
    assert not re.search(r"^\.cat-(line|tags?|stage|others)\b", css, re.M)          # removed with the stage line
    narrow = [m.group(1) for m in re.finditer(r"@container vid-list \(max-width: \d+px\) \{(.*?)\n\}", css, re.S)]
    assert any(".vid-row" in block for block in narrow)


def test_home_order_and_layout_switch():
    home = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    # sections are laid out from HOME_ORDER (home-utils, spec 4.1), one builder per key
    assert 'HOME_ORDER[phone ? "phone" : "desktop"]' in home
    build = home[home.index("const build = {"):home.index("const order =")]
    for key in ("next", "videos", "mission", "recent"):
        assert re.search(rf"^\s+{key}: \(\) =>", build, re.M), key
    # the layout follows the width without new requests; the listener goes away with the page
    assert 'const PHONE = "(max-width: 640px)";' in home
    assert 'media.addEventListener("change", view.onMedia)' in home
    assert 'H.media.removeEventListener("change", H.onMedia)' in home
    assert "export function unmount()" in home
    # phone: the next video's scene count and the listed videos' photos come from their contis, after the paint
    assert "loadContis(view, cat)" in home and "/api/conti?path=" in home
    assert "img.remove()" in home                                        # broken photo: the grey box stays


def _media_blocks(css, width):
    return [m.group(1) for m in re.finditer(rf"@media \(max-width: {width}px\) \{{(.*?)\n\}}", css, re.S)]


def _container_blocks(css, name, width):
    return [m.group(1) for m in re.finditer(rf"@container {name} \(max-width: {width}px\) \{{(.*?)\n\}}", css, re.S)]


def _rule(block, sel):
    m = re.search(rf"^\s*{re.escape(sel)} \{{([^}}]*)\}}", block, re.M)
    assert m, sel
    return m.group(1)


def test_home_narrow_layout_wraps_instead_of_overflowing():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    # Home rows follow their own width, not the viewport: the menu (240px) and the help panel (360px beside
    # the content from 1100px) narrow the column while the window stays wide.
    assert re.search(r"^\.home \{[^}]*container: home / inline-size", css, re.M)
    assert re.search(r"^\.vid-list \{[^}]*container: vid-list / inline-size", css, re.M)
    home_js = (STATIC_DIR / "pages" / "home.js").read_text(encoding="utf-8")
    assert 'class: `home${phone ? " home-phone" : ""}`' in home_js
    for width in (720, 560):
        for block in _media_blocks(css, width):
            for sel in (".vid-row", ".vid-head", ".vid-step", ".vid-steps", ".vid-film", ".vid-end",
                        ".home-next", ".home-next-body"):
                assert not re.search(rf"^\s*{re.escape(sel)}[ ,>]", block, re.M), (width, sel)

    rows_mid = "\n".join(_container_blocks(css, "vid-list", 640))     # title on top, dots + button below
    assert "grid-template-columns: minmax(0, 1fr) auto" in _rule(rows_mid, ".vid-row")
    assert "grid-column: 1 / -1" in _rule(rows_mid, ".vid-head")
    rows = "\n".join(_container_blocks(css, "vid-list", 480))         # one column, dots share the width
    assert "grid-template-columns: minmax(0, 1fr);" in _rule(rows, ".vid-row")
    assert "flex: 1 1 0" in _rule(rows, ".vid-step") and "min-width: 0" in _rule(rows, ".vid-step")
    assert "justify-self: start" in _rule(rows, ".vid-end")
    cards = "\n".join(_container_blocks(css, "home", 560))
    assert "flex-direction: column" in _rule(cards, ".home-next")     # main button goes under the text
    # phone cards: long titles cut with an ellipsis instead of widening the row
    assert "text-overflow: ellipsis" in _rule(css, ".home-pv-title") and "min-width: 0" in _rule(css, ".home-pv-body")
    assert "minmax(min(300px, 100%), 1fr)" in re.search(r"^\.grid-2 \{([^}]*)\}", css, re.M).group(1)


def test_add_progress_summary_first_then_proportional_steps():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    block = js[js.index("function stepRows"):js.index("// ---- review queue")]
    # 위에서부터: 전체 진행 원 -> 남은 시간·끝 시각 -> 지금 하는 일, 그 아래 단계 칸, 걸린 시간
    for token in ['"prog-ring"', '"이 영상 전체 진행률"', "itemProgress(", "쯤 끝", "지금: ", "stepWeights(",
                  "gridTemplateColumns", "걸린 시간", "예상 전체", "남은 시간 계산 중"]:
        assert token in block, token
    assert block.index('"prog-hero"') < block.index('"steps prog-steps"') < block.index('"prog-detail"')
    assert "monotonicFraction(ringMax" in block                  # 전체 원도 뒤로 가지 않는다
    assert "S.ringMax" in block and "Math.min(99" in block       # 카드를 새로 만들어도 유지, 끝나기 전엔 100%를 보이지 않는다
    assert "prog.overdue" in block                               # 예상 넘긴 측정 불가 단계는 0초 남음 대신 문구
    assert "r.sub" not in js and "step-total" not in js            # 단계 아래 긴 줄과 맨 아래 합계 줄은 위쪽 요약으로 옮겼다
    for sel in [".prog-ring", ".prog-remain", ".steps.prog-steps", ".row-running .step-name", "@container (max-width: 420px)"]:
        assert sel in css, sel


def test_conti_history_panel_contract():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in [
        "/api/conti/history", "/api/conti/history/diff", "/api/conti/history/name", "/api/conti/history/restore",
        "이름 붙이기", "지금 상태에 이름 붙이기", "나란히 보기", "되돌리기", "이 버전으로 되돌리기", "지금과 비교", "둘 비교",
        "지금 상태도 이력에 남아 있어 다시 되돌릴 수 있어요", "aria-expanded", "inert", "collapseChanges", "저장하지 않은 입력", "저장 중이에요. 잠시 뒤 다시 눌러 주세요", '"Escape"', "conti_changed", "alignCuts", "changeText",
    ]:
        assert token in text, token
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for token in [".hist-panel", ".hist-pair", ".is-diff", ".hist-tag"]:
        assert token in css, token


def test_add_area_resume_only_for_jobs_that_need_attention():
    add = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    assert "needsAttention(data.job)" in add
    refs = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert "autoOpened" in refs


def test_add_area_review_queue_and_progress_card_wiring():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for token in ["saveAllPlan(", "/review/", "/cancel", "모두 저장", "자세히", "확인 대기", "aria-expanded",
                  "UI 서버를 끄면 사라져요", "멈추기", "고칠 점 적고 다시 분석", "작업이 멈춰 다시 분석할 수 없어요",
                  "카테고리에서", "보기", "tabindex", "reviewItems(", "queueLine(", "/static/section-band.js",
                  "onSaved"]:
        assert token in js, token
    for banned in ["innerHTML", "window.confirm", "confirm(", "alert(", "prompt("]:
        assert banned not in js, banned
    for sel in [".review-queue", ".review-card", ".review-head", ".review-notice", ".other-cat-line"]:
        assert sel in css, sel
    sb = (STATIC_DIR / "section-band.js").read_text(encoding="utf-8")
    assert "export function sectionBand" in sb and "marginLeft" in sb
    refs = (STATIC_DIR / "pages" / "refs.js").read_text(encoding="utf-8")
    assert "/static/section-band.js" in refs and "function sectionBand" not in refs


def test_save_all_uses_the_review_route_per_card_and_stop_keeps_focus():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    body = js[js.index("async function saveAll"):js.index("function lengthOf")]
    assert "saveAllPlan(" in body and "/review/" in body and "/save-all" not in body
    assert 'data-act": "stop"' in js and "hadFocus" in js and '"aria-disabled"' in js
    assert 'S.headSig = ""' in js[js.index("function ensurePanel"):js.index("function renderJobPanel")]


def test_new_job_keeps_state_of_adopted_review_cards_and_guards_before_apply():
    js = (STATIC_DIR / "pages" / "add.js").read_text(encoding="utf-8")
    start = js[js.index("async function startJob"):js.index("async function resume")]
    assert "staleIds(" in start and "edits.clear()" not in start
    save = js[js.index("async function saveAll"):js.index("function lengthOf")]
    fetched, guard, apply = (save.index("await fetchJob"), save.index("S !== mine", save.index("await fetchJob")),
                             save.index("applyJob(fresh)"))
    assert fetched < guard < apply
    prog = js[js.index("function renderProgress"):js.index("function tickClock")]
    assert 'i.step !== "confirm"' in prog


def test_topbar_fits_390px_phones():
    # 390px phones: logo, category, status slot and help button share one row (an overflow widens the whole page)
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    phone = "\n".join(_media_blocks(css, 640))
    assert "min-width: 0" in _rule(phone, ".cat-select")
    assert "display: none" in _rule(phone, ".cat-label")             # the "카테고리" word drops on phones
    assert "flex-shrink: 0" in re.search(r"^\.brand \{([^}]*)\}", css, re.M).group(1)
    assert "padding-bottom" in _rule(phone, ".main")                  # content clears the bottom tabs
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'h("select", { id: "category-select", "aria-label": "카테고리" })' in app   # name kept when the label hides
    assert "bottom:" in _rule(phone, ".toasts")                       # toasts sit above the bottom tabs


def test_footage_area_contract_and_safety():
    js = (STATIC_DIR / "footage-area.js").read_text(encoding="utf-8")
    for token in [
        "/api/footage?path=", "/api/footage/folders", "/api/footage/folders/remove", "/api/footage/process",
        "/api/footage/choose", "/api/footage/not-filmed", "/api/footage/reset", "/api/footage/suggestions/hide",
        "/api/footage/suggestions/insert", "/api/footage/remaining", "/api/footage/csv", "/api/footage/clips",
        "/api/jobs/", "/cancel", "cut_not_linked", "conti_changed", "isCurrent()", "clearTimeout", "aria-live",
        "aria-expanded", "aria-pressed", "filesUrl", "mediaUrl", "frameUrl",
        # copy from the brief and controller rulings, verbatim
        "촬영 영상", "폴더 더하기", "빼기", "새 파일 처리하고 맞추기", "멈추기", "지금 파일이 끝나면 멈춰요",
        "전체", "남은 컷", "확인 필요", "이걸로", "재생", "못 찍음으로", "이 컷 뒤에 넣기", "숨기기",
        "폰용 남은 컷 만들기", "CSV 받기", "컷 파일 만들기", "시작이 몇 초 앞당겨질 수 있어요",
        "이 영상은 브라우저에서 재생할 수 없어요", "원본 없음", "새 컷 후보",
    ]:
        assert token in js, token
    # a <video> that cannot play (HEVC in Chrome) falls back to the photo strip
    assert 'h("video"' in js and '"error"' in js
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "window.confirm", "confirm(", "alert(", "prompt(",
                   "http://", "https://", "localStorage"]:
        assert banned not in js, banned


def test_conti_editor_mounts_footage_area_and_shows_verdict_dots():
    js = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    for token in ['from "/static/footage-area.js"', "mountFootageArea", "verdict-dot", "sr-only"]:
        assert token in js, token
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for cls in [".footage", ".verdict-dot", ".v-badge", ".fc-card", ".fc-strip", ".fc-filter"]:
        assert cls in css, cls


def test_footage_files_served(client):
    for rel in ["footage-area.js", "footage-utils.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200 and "javascript" in headers["Content-Type"], rel


def test_footage_insert_keeps_editor_drafts_and_waits_for_saves():
    js = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    start = js.index("async function reloadAfterFootageInsert")
    body = js[start: js.index("\n}\n", start)]
    for token in ["remapByCutId(mine.forms", "remapByCutId(mine.alts", "mine.scenes.clear()", "mine.hash = data.hash"]:
        assert token in body, token
    assert "getEditorBusy: () => mine.editing || mine.saving || mine.conflict" in js
    area = (STATIC_DIR / "footage-area.js").read_text(encoding="utf-8")
    assert "편집을 저장한 뒤 넣어 주세요" in area and "getEditorBusy()" in area
    # a playing <video> survives repaints; the error handler checks the area is still mounted
    assert "F.players" in area and "if (!alive(mine)) return;\n    mine.unplayable.add(key);" in area


def test_footage_reload_error_shown_when_view_exists():
    js = (STATIC_DIR / "footage-area.js").read_text(encoding="utf-8")
    start = js.index("function cutsSlot()")
    body = js[start: js.index("\n}\n", start)]
    assert "F.error && F.view" in body
    assert 'h("p", { class: "error", role: "alert" }, F.error)' in body
    assert 'btn("다시 시도", { "data-fkey": "retry-view", "aria-disabled": busy("retry-view") }, () => retryLoad("retry-view"))' in body
    # the error goes first, before the no-folders early return
    assert body.index("F.error && F.view") < body.index("if (!v || !v.folders.length)")


def test_footage_retry_ignores_clicks_while_reloading():
    js = (STATIC_DIR / "footage-area.js").read_text(encoding="utf-8")
    start = js.index("async function retryLoad(")
    body = js[start: js.index("\n}\n", start)]
    assert "mine.busy.has(key)" in body and "await load()" in body and "markBusy(key, false)" in body
    assert js.count("() => retryLoad(") == 2 and "() => load())" not in js


# ---- video workspace (Phase 2) -------------------------------------------------------------------

def _js(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_video_page_has_four_tabs_and_progress_card():
    js = _js("pages/video.js")
    for token in ["export async function mount", "export function unmount", "export function update",
                  "export function unsavedNote", "export function onStay", "export function onCategory",
                  'from "/static/video-utils.js"', 'from "/static/ui.js"', "TABS", "tabs(", "stages(",
                  "nextAction(", "stageText(", "contiSummary(", "shootLine(", "videoMeta(", "shortTitle(",
                  '"진행 단계"', '"내 영상"', '"폰 촬영 모드"', '"다 찍음"', '"인쇄"', '"다시 만들기"',
                  '"+ 새 영상 시작"', "/api/drafts/filmed", "/api/status", "/api/draft?path=", "/api/conti?path=",
                  'role: "tabpanel"', "isCurrent()"]:
        assert token in js, token
    # the hosted pages: plan = draft view, conti, diagnose - mounted with their own mount/unmount and a flag
    for token in ['"/static/pages/draft.js"', '"/static/pages/conti.js"', '"/static/pages/diagnose.js"',
                  "embedded: true", ".unmount()"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "confirm(", "alert(", "prompt("]:
        assert bad not in js, bad
    utils = _js("video-utils.js")
    for label in ('"기획안"', '"콘티"', '"촬영 체크"', '"진단"', '"다듬기"', '"편집"'):
        assert label in utils, label


def test_router_has_video_routes_legacy_redirects_and_leave_guard():
    app = _js("app.js")
    assert re.search(r'import \{[^}]*legacyRoute[^}]*\} from "/static/video-utils\.js"', app)
    assert "parseVideoHash" in app and 'page: "video"' in app
    route = app[app.index("async function route("):app.index("// ---- start")]
    assert "legacyRoute(" in route and "location.replace(" in route        # old bookmarks keep working, no extra entry
    assert ".update(" in route                                              # tab switch in place, no remount
    # unsaved edits: asked before the route changes (no confirm()), hash put back with replaceState
    assert "unsavedNote" in app and "history.replaceState" in app and "confirmLeave" in app
    assert '"버리고 이동"' in app and '"계속 편집"' in app
    assert "confirm(" not in app.replace("confirmLeave(", "").replace("pop-confirm", "").replace("help-confirm", "")


def test_hosted_pages_hide_their_own_head_in_the_workspace():
    for name in ("draft.js", "conti.js", "diagnose.js"):
        js = _js(f"pages/{name}")
        assert "embedded" in js, name
    conti = _js("pages/conti.js")
    assert "export function unsavedNote" in conti              # the workspace and the router ask before leaving
    assert "unsavedCuts()" in conti.split("export function unsavedNote", 1)[1]


def test_sidebar_and_phone_tab_open_the_video_list():
    app = _js("app.js")
    assert 'href: "#/videos"' in app                           # 전체 N편
    assert '"#/videos"' in _js("menu-utils.js")


def test_video_css_title_rule_and_progress():
    css = _js("app.css")
    assert re.search(r"\.video-title \{[^}]*font-size: 30px[^}]*border-left: 6px solid var\(--rec\)", css)
    assert re.search(r"\.vp-steps \{[^}]*grid-template-columns: repeat\(6, minmax\(0, 1fr\)\)", css)
    assert ".leave-ask" in css


def test_leave_guard_keeps_history_clean_and_category_waits():
    app = _js("app.js")
    guard = app[app.index("// ---- leaving with unsaved input"):app.index("// ---- start")]
    # stay: back to the page's own entry (new entry dropped, back/forward undone), no duplicate replaceState entry
    assert "history.back()" in guard and "history.forward()" in guard and "ytcAt" in guard
    assert 'replaceState(null, "", state.hash)' not in app
    assert "function navigate(" in guard and "force" in guard and "location.replace(" in guard
    assert "stayed()" in guard
    video = _js("pages/video.js")
    assert "navigate: mine.ctx.navigate" in video
    on_cat = video[video.index("export function onCategory"):]
    assert "confirmLeave(" in on_cat and "setCategory(mine.category)" in on_cat and "force: true" in on_cat
    assert "get category() { return mine.category" in video


def test_hosted_pages_fix_round_1():
    conti = _js("pages/conti.js")
    assert '"#/videos" }, "내 영상"' in conti                      # workspace error states lead to the video list
    assert "navigate(contiHref(res.path), { replace: true })" in conti
    assert conti.count("[...S.forms].filter(") == 1                # one unsavedCuts() helper
    draft = _js("pages/draft.js")
    assert "export function unsavedNote" in draft and "pl.dirty" in draft.split("export function unsavedNote", 1)[1]
    video = _js("pages/video.js")
    assert "return mine.loading" in video                          # a failed load remounts on the next route
    assert "fetchLight(" in video


def test_sidebar_new_video_is_an_action_link():
    app = _js("app.js")
    assert "a.dataset.action ? false" in app


def test_css_fix_round_1():
    css = _js("app.css")
    assert re.search(r"\.leave-ask \{[^}]*position: sticky", css)
    assert re.search(r"\.subnav \{[^}]*overflow-y: hidden", css)


# ---- Phase 4: motif cards, detail panel / bottom sheet, start-video flow ----------------------

def test_motif_page_cards_detail_and_start_flow():
    js = _js("pages/motif.js")
    for token in ['from "/static/motif-utils.js"', 'from "/static/ui.js"', "oneLine(", "kindCounts(", "filterCards(",
                  "새 카드 뽑기 · 약 30초", "새 카드 주제 (선택)", "근거 영상 ", "이렇게 찍어 보세요", "이 카드로 만든 영상",
                  "이 카드로 새 영상 시작", "#/draft/new/", "/api/refs?category=", "/api/draft/list?category=",
                  "filesUrl(", "export function unsavedNote", "aria-expanded", "aria-controls"]:
        assert token in js, token
    # no terminal / command copy on the screen
    for bad in ["yt-inspire", "/yt-coach:", "터미널", '기획안 만들기"']:
        assert bad not in js, bad


def test_motif_detail_is_a_modal_sheet_on_phones():
    js = _js("pages/motif.js")
    for token in ['"role", "dialog"', '"aria-modal", "true"', '"Escape"', '"Tab"', "matchMedia(", "returnFocus",
                  "닫기"]:
        assert token in js, token
    css = _js("app.css")
    assert re.search(r"\.motif-cards \{[^}]*grid-template-columns: repeat\(auto-fill, minmax\(230px, 1fr\)\)", css)
    assert re.search(r"\.motif-layout \{[^}]*grid-template-columns: minmax\(0, 1fr\) 380px", css)
    # sheet below 900px (where the 380px panel no longer fits next to the grid); empty placeholder hidden there
    narrow = "\n".join(_media_blocks(css, 900))
    assert re.search(r"\.motif-detail \{[^}]*position: fixed", narrow)
    assert re.search(r"\.motif-detail:not\(\.is-open\) \{[^}]*display: none", narrow)
    assert 'const SHEET = "(max-width: 900px)"' in js
    assert not re.search(r"\.motif-detail \{[^}]*position: fixed", "\n".join(_media_blocks(css, 640)))


def _fn(js, name):
    start = js.index(f"function {name}(")
    return js[start:js.index("\n}\n", start)]


def test_motif_render_keeps_an_open_detail_and_its_focus():
    """I-001: a run finishing (render()) must not drop the open panel/sheet node, its Esc/Tab handler or focus."""
    js = _js("pages/motif.js")
    render = _fn(js, "render")
    assert "detail: S.nodes.detail" in render and "scrim: S.nodes.scrim" in render     # node survives S.nodes reset
    assert "focusDetailTitle()" in render                                               # focus comes back into it
    focus = _fn(js, "focusStatus")
    assert "inDetail()" in focus                                                        # never pulled out of the panel


def test_motif_closes_the_sheet_before_the_leave_question():
    """I-002: the leave question would open under the sheet's scrim: onStay() closes the sheet first."""
    js = _js("pages/motif.js")
    stay = js[js.index("export function onStay"):]
    assert "closeDetail()" in stay[:stay.index("\n}\n")]
    app = _js("app.js")
    assert app.index("state.current.onStay()") < app.index("confirmLeave(note, () => navigate(target")


def test_motif_detail_toggles_selection_in_place_and_follows_the_viewport():
    js = _js("pages/motif.js")
    for name in ("openDetail", "closeDetail"):
        body = _fn(js, name)
        assert "markSelected()" in body and "renderList(" not in body, name           # m-005: no grid rebuild
    assert "focusDetailTitle()" in _fn(js, "onMedia")                                   # m-007
    assert "lockedReason(" in js and 'href: "#/typify"' in js                          # m-003


def test_motif_running_state_is_the_status_chip():
    js = _js("pages/motif.js")
    # the header chip shows the running job; the page keeps failure / errors / the re-run question and a busy button
    assert 'data-job-kind": "motif"' in js and "header status chip" in js
    assert "STEP_TEXT" not in js and "motif-progress-line" not in js
    assert '"카드 뽑는 중…"' in js


def test_draft_save_lands_on_the_new_video_workspace():
    draft = _js("pages/draft.js")
    save = draft[draft.index("async function save()"):]
    save = save[:save.index("\n}\n")]
    assert 'navigate(videoHref(videoId(res.path), "plan"))' in save
    assert "location.hash = viewHref(res.path)" not in save


def test_conti_page_uses_new_ref_match_values():
    text = (STATIC_DIR / "pages" / "conti.js").read_text(encoding="utf-8")
    assert 'const MATCH_TEXT = { pinned: "참고(고정)", content: "참고", composition: "구도만 참고" };' in text
    badge = text[text.index("function matchBadge"):text.index("// Cut list")]
    assert 'match === "content"' in badge and "exact" not in badge
    assert "refCaption(cut.ref)" in text                     # 참고 · <설명> / 구도만 참고 · <설명> / 참고(고정)
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".badge.match-composition" in css


# ---- Phase 3: conti card board, scene actions --------------------------------------------------

def test_conti_board_files_served(client):
    for rel in ["conti-board.js", "conti-scenes.js"]:
        status, headers, _ = client.call("GET", f"/static/{rel}")
        assert status == 200 and "javascript" in headers["Content-Type"], rel


def test_conti_board_tool_row_and_cards():
    board = _js("conti-board.js")
    for token in ['"장면 검색"', '"구간 필터"', '"보기 방식"', '"카드"', '"리스트"', '"전체"', "aria-pressed", "localStorage",
                  "try {", "ytc.conti.view", "cardStamp(c)", "aboutSec(c.sec)", "refCaption(c.ref)", "filesUrl(c.ref.frame)",
                  "“${c.line}”", '"찾는 장면이 없어요"', '"필터 지우기"', "tag(sectionLabelName(c.label), tagKind(c.label))"]:
        assert token in board, token
    conti = _js("pages/conti.js")
    for token in ['from "/static/conti-board.js"', 'from "/static/conti-scenes.js"', "filterCuts(", "sectionGroups(",
                  "function renderCards", "function onGridKey", "selectCut(c.n)", "writeView(view)", '"앞으로 옮기기"',
                  '"뒤로 옮기기"', "has-sel", "panel-close", "장면 ${d.cuts.length}개"]:
        assert token in conti, token
    # the photo is the picture: the sketch only when a scene has no photo (list rows and the panel)
    assert conti.count("c.ref ? null : sanitizeSvg(c.sketch)") == 1 and "cut.ref ? null : sanitizeSvg(cut.sketch)" in conti
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "repeat(auto-fill, minmax(210px, 1fr))" in _rule(css, ".cb-grid")
    assert "height: 112px" in _rule(css, ".cb-frame")


def test_conti_scene_actions_contract():
    js = _js("conti-scenes.js")
    for token in ['"장면으로 합치기"', '"사진 설명 만들기"', '"사진 다시 고르기"', "/api/conti/merge/preview", "/api/conti/merge/apply",
                  "/api/refs/describe-status?category=", '"/api/refs/describe"', "/api/conti/refs/pick", "/cancel",
                  "nothing_to_merge", "conti_changed", "no_ref_descs", "data-job-kind", "conti_merge", "refs_describe",
                  "conti_refs", "mergeApplyCuts(preview)", "hash: preview.hash", "mergeView(preview)", "describeHint(",
                  "hasRepeats(", "isCurrent()", "clearTimeout", "aria-live", "markHandled", "장면으로 합치기 미리보기",
                  "컷 ${preview.before}개 → 장면 ${preview.after}개", "m.warnings"]:
        assert token in js, token
    for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "confirm(", "alert(", "prompt("]:
        assert banned not in js, banned
    # merge save redraws the editor from the answer with drafts following their scene by id
    conti = _js("pages/conti.js")
    merged = conti[conti.index("onMerged: (res) =>"):conti.index("onReload:")]
    for token in ["remapByCutId(mine.forms, before, res)", "remapByCutId(mine.alts, before, res)", "mine.scenes.clear()",
                  "mine.hash = res.hash"]:
        assert token in merged, token


def test_conti_tab_hides_file_names():
    conti = _js("pages/conti.js")
    for gone in ['split("/").pop()} (', "${name} 받기", "C:\\\\Users", "fm.title || d.path"]:
        assert gone not in conti, gone


def test_conti_fix_round_1_contracts():
    conti = _js("pages/conti.js")
    scenes = _js("conti-scenes.js")
    # I-001: rows from layout tops through the tested helper
    assert "gridMove(" in conti and "getBoundingClientRect().top" in conti and "offsetTop" not in conti
    # I-002: only the merge's own warnings are toasted
    assert "res.merged" in scenes and "line_too_long" not in scenes
    # I-003: no length snap on undo
    assert "fields.sec = snapSec(fields.sec)" not in conti
    # I-004: a pick finishing during a save reloads afterwards
    assert "reloadPending" in conti
    # I-005: no merge over unsaved panel input
    assert "저장하지 않은 입력이 있어요. 저장하거나 지운 뒤 합쳐 주세요" in scenes and "hasDrafts" in conti
    # m-002 search debounce, m-009 header link to the folded subtitle tools
    assert "SEARCH_MS" in conti and '"자막 내보내기"' in conti and "srtOpen = true" in conti
    # m-005: scene wording
    for gone in ["`컷 ${n} · ", "`컷 ${cut.n}`", "`컷 ${cut.n} 메뉴`", "저장하지 않은 입력(컷 "]:
        assert gone not in conti, gone
    # m-006: one closeMenu per redraw path
    start = conti.index("function renderCutList")
    assert conti[start:conti.index("function gapItem")].count("closeMenu(false)") == 1
    help_text = (STATIC_DIR.parent.parent.parent / "docs" / "help" / "conti.md").read_text(encoding="utf-8")
    assert "방향키" in help_text and "⋯" in help_text


# ---- Phase 5: phone shoot mode -----------------------------------------------------------------

def test_shoot_page_contract():
    js = _js("pages/shoot.js")
    for token in ["export async function mount", "export function unmount", 'from "/static/shoot-utils.js"',
                  "shootStore(", "mainLabel(", "tipsFrom(", "nextTodo(", "firstTodo(", "barSegments(", "sceneTime(",
                  "/api/draft?path=", "/api/conti?path=", "/api/drafts/filmed", "filmed: true",
                  '"촬영 모드"', '"촬영 모드 닫기"', '"이전 장면"', '"다음 장면"', '"찍음 ✓"', '"콘티 탭으로 가기"',
                  '"영상으로 돌아가기"', "filesUrl(", "isCurrent()", "shoot-dark"]:
        assert token in js, token
    for bad in ["innerHTML", "insertAdjacentHTML", "outerHTML", "confirm(", "alert(", "prompt(", "다음 컷"]:
        assert bad not in js, bad


def test_shoot_page_wake_lock_and_keys_are_scoped():
    js = _js("pages/shoot.js")
    # screen stays on while shooting (when supported), released on leave; failures ignored
    assert "navigator.wakeLock" in js and '.request("screen")' in js and ".release()" in js
    assert '"visibilitychange"' in js                      # the lock drops when the tab hides: ask again
    # arrow keys only on this screen (listener removed on leave) and never while typing
    assert '"ArrowLeft"' in js and '"ArrowRight"' in js
    assert 'addEventListener("keydown"' in js and 'removeEventListener("keydown"' in js
    assert "isContentEditable" in js and "TEXTAREA" in js


def test_router_opens_the_shoot_page_without_app_chrome():
    app = _js("app.js")
    assert 'page: "shoot"' in app
    assert "shoot-mode" in app and "shoot-dark" in app      # body classes: chrome hidden, dark tokens
    css = _js("app.css")
    assert re.search(r"body\.shoot-mode :is\(\.head, \.menu, \.subnav, \.tabbar, \.help-panel, \.help-scrim\) \{[^}]*display: none", css)
    assert re.search(r"\.shoot \{[^}]*max-width: 480px", css)
    assert re.search(r"\.shoot-frame \{[^}]*height: 216px", css)
    assert re.search(r"\.shoot-close \{[^}]*width: 44px[^}]*height: 44px", css)
    assert re.search(r"\.shoot-action \{[^}]*font-size: 22px[^}]*font-weight: 700", css)
    assert re.search(r"\.shoot-line \{[^}]*font-size: 18px[^}]*border-left: 4px solid var\(--rec\)", css)
    assert ".shoot-stub" not in css and "renderShoot" not in _js("pages/video.js")


def test_check_tab_uses_the_shoot_store():
    video = _js("pages/video.js")
    for token in ['from "/static/shoot-utils.js"', "shootStore(", "shootLine(", 'type: "checkbox"', '"폰 촬영 모드"',
                  '"다 찍음"', "남은 장면"]:
        assert token in video, token
    check = video[video.index("function renderCheck("):video.index("// ---- page contract")]
    assert "btn-rec" in check                                # the red shoot-mode button of the tab


# ---- Phase 5 fix round 1 -----------------------------------------------------------------------

REPEATS_NOTE = "같은 장면이 여러 컷으로 나뉘어 있어요. 콘티 탭에서 장면으로 합치면 찍기 편해요."


def test_shoot_bar_never_overflows_and_marks_a_filmed_current_scene():
    css = _js("app.css")
    assert re.search(r"\.shoot-bar \{[^}]*overflow: hidden", css)
    assert re.search(r"\.shoot-seg \{[^}]*min-width: 0", css)
    assert re.search(r"\.shoot-seg\.is-done-now \{[^}]*background: var\(--done\)[^}]*outline: 2px solid var\(--ink\)", css)
    assert "barStates" not in _js("shoot-utils.js") and "barSegments(" in _js("pages/shoot.js")


def test_shoot_dark_focus_ring_and_undo_target():
    css = _js("app.css")
    dark = css[css.index(".shoot-dark {"):]
    dark = dark[:dark.index("}")]
    assert "--focus-ring:" in dark                           # var() in :root resolved there: re-declare for the dark ring
    assert re.search(r"\.shoot-undo \{[^}]*min-height: 44px", css)


def test_shoot_marks_update_in_place_and_wake_lock_is_single_flight():
    js = _js("pages/shoot.js")
    assert "function renderScene(" in js and "function paintMarks(" in js
    on_main = js[js.index("function onMain("):js.index("async function finish(")]
    assert "paintMarks(mine)" in on_main and "renderScene(" not in on_main   # checking keeps the frame (photo)
    lock = js[js.index("async function lockScreen("):js.index("function unlock(")]
    assert "mine.locking" in lock


def test_repeats_note_in_shoot_mode_and_check_tab():
    for name in ("pages/shoot.js", "pages/video.js"):
        js = _js(name)
        assert "hasRepeats(" in js and REPEATS_NOTE in js, name


def test_check_tab_filter_filmed_note_and_no_duplicate_card_actions():
    video = _js("pages/video.js")
    check = video[video.index("// 촬영 체크: the scenes"):video.index("// ---- page contract")]   # notes, row, tab
    for token in ['"전체"', '"남은 장면"', "chip(",
                  "이 영상은 촬영 완료로 표시돼 있어요. 다시 찍을 장면만 체크하세요."]:
        assert token in check, token
    actions = video[video.index("function progressActions("):video.index("// Draft-level")]
    assert 'mine.tab === "check"' in actions and "return []" in actions   # the tab head carries count and buttons


# ---- Phase 7: remaining screens on the tokens ------------------------------------------------------

def test_refs_filters_are_chips_and_frame_captions_are_korean():
    js = _js("pages/refs.js")
    tools = js[js.index("function toolbar("):js.index("function renderCards(")]
    assert 'class: "ui-chip"' in tools and 'class: "btn", "aria-pressed"' not in tools
    frames = js[js.index("function framesBlock("):js.index("// ---- page contract")]
    assert "sectionName(" in frames and "${f.section}" not in frames


def test_library_hides_commands_and_repository_address():
    js = _js("pages/library.js")
    assert "gh auth" not in js and "git(SSH" not in js
    assert "st.remote ||" not in js and "repoName(st.remote)" in js     # the name of the repository, never a path
    assert 'class: "ui-chip lib-tab"' in js


def test_typify_uses_menu_name_and_korean_section_names():
    js = _js("pages/typify.js")
    assert 'from "/static/labels.js"' in js
    assert '"유형화")' not in js and 'h("h1", { id: "typify-title" }, "유형")' in js
    assert "ref ${" not in js and "내 영상(mine)" not in js and '"ref 부족"' not in js
    for fn, end in (("function skeletonChips(", "function ratioBand("), ("function ratioBand(", "function refLink(")):
        part = js[js.index(fn):js.index(end)]
        assert "skelName(" in part, fn                      # "body/story" -> "본문(이야기)" on screen and in names


def test_report_picker_is_chips_and_tabs_match_the_kit():
    js = _js("pages/report.js")
    picker = js[js.index("function reportList("):js.index("function reportViewer(")]
    assert "btn-primary" not in picker and "ui-chip" in picker
    assert "## 내 메모" not in js and "프런트매터" not in js
    css = _js("app.css")
    for sel in (".rp-tab", ".guide-tab"):
        on = re.search(re.escape(sel) + r'\[aria-(?:selected|current)="(?:true|page)"\] \{([^}]*)\}', css).group(1)
        assert "var(--rec)" in on and "font-weight: 700" in on, sel


def test_upload_diagnosis_goes_back_to_my_videos():
    js = _js("pages/diagnose.js")
    assert '"#/diagnose"' not in js                      # the old list redirects; link where it lands
    assert '"← 내 영상"' in js and 'href: "#/videos"' in js


def test_diagnose_skeletons_use_korean_section_names():
    js = _js("pages/diagnose.js")
    assert "skeletonText(near.skeleton)" in js and "skeletonText(c.target_sequence)" in js
    assert "near.skeleton ||" not in js and "c.target_sequence ||" not in js


def test_fix1_typify_humanizes_text_and_folds_long_member_lists():
    js = _js("pages/typify.js")
    assert "humanizeLabels" in js and 'from "/static/labels.js"' in js
    assert 'h("span", { class: "md-text" }, humanizeLabels(res.note' in js
    card = js[js.index("function archetypeCard("):js.index("function titleMap(")]
    assert "MEMBERS_SHOWN" in card and '"details"' in card and "더 보기" in card
    assert re.search(r"const MEMBERS_SHOWN = 5;", js)


def test_fix1_library_uses_the_tested_repo_name():
    js = _js("pages/library.js")
    assert 'import { repoName } from "/static/library-utils.js";' in js and "function repoName(" not in js


def test_fix1_one_chip_size():
    css = _js("app.css")
    chip = re.search(r"^\.ui-chip \{([^}]*)\}", css, re.M).group(1)
    seg = re.search(r"^\.seg-opt span \{([^}]*)\}", css, re.M).group(1)
    for rule in (chip, seg):
        assert "min-height: 32px" in rule and "font-size: 13px" in rule and "border-radius: 999px" in rule
    assert not re.search(r"^\.lib-tab \{[^}]*(min-height|font-size|padding)", css, re.M)   # lib tabs are plain chips


def test_fix1_mission_file_field_has_no_path_word():
    js = _js("pages/mission.js")
    label = re.search(r'h\("label", \{ for: "mi-file" \}, "([^"]+)"\)', js).group(1)
    assert "경로" not in label


# ---- final review fix wave -----------------------------------------------------------------------

def test_final_i1_conti_changes_repaint_the_video_header():
    video = _js("pages/video.js")
    host = video[video.index("const hostCtx = {"):video.index("fill(panel, h(\"p\"", video.index("const hostCtx = {"))]
    assert "contiChanged: () => { if (live()) refreshHead(mine, true); }" in host
    conti = _js("pages/conti.js")
    assert "function contiChanged(mine)" in conti and "mine.ctx.contiChanged()" in conti
    # merge, structure edits (insert/duplicate/move/delete/undo), editor reloads (cut save, restore, conflict
    # reload), footage insert: each tells the workspace
    merged = conti[conti.index("onMerged: (res) => {"):conti.index("onReload: () =>")]
    assert "contiChanged(mine);" in merged
    edit = conti[conti.index("async function editCuts("):conti.index("function showEdited(")]
    assert "contiChanged(mine);\n    return res;" in edit
    load = conti[conti.index("async function loadEditor("):conti.index("function currentCut(")]
    assert "if (keep) contiChanged(mine);" in load
    foot = conti[conti.index("async function reloadAfterFootageInsert("):conti.index("const DOT_TEXT")]
    assert foot.count("contiChanged(mine);") == 1
    for fn in ("async function saveCut(", "async function restoreTo("):
        body = conti[conti.index(fn):]
        assert "await loadEditor(true);" in body[:body.index("\n}\n")]


def test_final_m3_replace_with_unsaved_input_pushes_an_entry():
    app = _js("app.js")
    nav = app[app.index("function navigate("):app.index("function pageNote(")]
    assert "if (replace && (force || !pageNote())) location.replace(hash); else location.hash = hash;" in nav


def test_final_m4_leave_question_sits_above_overlays_below_toasts():
    css = _js("app.css")
    rule = re.search(r"^\.leave-ask \{([^}]*)\}", css, re.M).group(1)
    z = int(re.search(r"z-index: (\d+)", rule).group(1))
    assert z == 48 and 47 < z < 70 and z > 45 and z > 40


def test_final_m5_in_place_route_skips_the_sidebar_read_and_shares_status():
    app = _js("app.js")
    route = app[app.index("async function route()"):app.index("// ---- leaving with unsaved input")]
    assert "routeChrome(hash, !inPlace);" in route and "if (inPlace) refreshSidebar();" in route
    chrome = app[app.index("function routeChrome("):app.index("async function route()")]
    assert "if (sidebar) refreshSidebar();" in chrome
    assert "statusSeen: adoptStatus" in app and "function adoptStatus(category, answer)" in app
    video = _js("pages/video.js")
    assert video.count("shareStatus(mine, ") == 2


def test_final_m6_lost_focus_goes_to_main_after_a_route():
    app = _js("app.js")
    assert "function keepFocus()" in app
    route = app[app.index("async function route()"):app.index("// ---- leaving with unsaved input")]
    assert "if (token === state.navToken) keepFocus();" in route
    assert 'tabindex="-1"' in (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert ".main:focus { outline: none; }" in _js("app.css")


def test_final_m7_conti_editor_names_scenes():
    conti = _js("pages/conti.js")
    for text in ['"여기에 장면 넣기"', '"섹션에 장면이 하나는 있어야 해요"', '"aria-label": "장면 목록"',
                 "`콘티를 저장했습니다 · 장면 ${res.cuts}개`"]:
        assert text in conti, text
    for gone in ['"여기에 컷 넣기"', '"섹션에 컷이 하나는 있어야 해요"', '"aria-label": "컷 목록"', "· 컷 ${res.cuts}개",
                 '"맨 앞 컷입니다"', '"다른 컷 고르기"', "`컷 ${cuts.length}개 · "]:
        assert gone not in conti, gone


def test_final_m9_progress_action_on_its_own_tab_focuses_the_panel_button():
    video = _js("pages/video.js")
    act = video[video.index("function progressActions("):video.index("// Draft-level")]
    assert "act.kind === mine.tab" in act and "focusTabAction(mine)" in act
    assert "[data-tab-primary]" in video
    assert '"data-tab-primary": ""' in _js("pages/conti.js") and '"data-tab-primary": ""' in _js("pages/diagnose.js")


def test_final_draft_preview_edits_ask_before_leaving():
    draft = _js("pages/draft.js")
    note = draft[draft.index("export function unsavedNote()"):draft.index("export function unmount()")]
    assert "S.preview && S.edit && S.dirty" in note and "저장하지 않은 기획안 입력" in note
    save = draft[draft.index("async function save()"):draft.index("function askDiscard()")]
    assert save.index("mine.dirty = false;") < save.index("mine.ctx.navigate(")


def test_final_motif_page_announces_its_own_job():
    motif = _js("pages/motif.js")
    assert "function say(text)" in motif and '"aria-live": "polite"' in motif
    assert 'say("Claude가 모티브 카드를 만드는 중이에요")' in motif and "카드 ${n}장을 뽑았어요" in motif

"""관리 화면(서버판 #/admin, T16 + 0004 사용자 관리): 정적 계약 + admin-utils.js 단위(node 없으면 그 부분만 skip).

- 화면은 vanilla ES 모듈, `h()`만(innerHTML·브라우저 confirm 없음), 확인은 화면 안 확인 상자, 대화 상자는 기본 <dialog>.
- 탭 네 개(사용자·사용량·감사 기록·서버 정보), 사용자 목록은 서버 쪽 검색·필터·정렬·쪽.
- 사용자 삭제는 그 사람의 이메일을 그대로 입력해야 버튼이 켜진다.
- 메뉴(머리글 링크·도움말 메뉴)는 관리자에게만 보이고, 로컬판은 `/api/me`를 묻지도 않는다.
- 비밀번호는 보내기만 하고 다시 보여 주지 않는다.
"""
import json
import re
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR
from tests.test_ui_server import client, no_browser  # noqa: F401  (fixture 재사용)

NODE = shutil.which("node")
PAGE = STATIC_DIR / "pages" / "admin.js"
UTILS = STATIC_DIR / "admin-utils.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _js(path):
    return path.read_text(encoding="utf-8")


def run_js(module, body):
    code = (f"import({json.dumps(module.as_uri())}).then(async (m) => {{ const out = await (async () => {{ {body} }})(); "
            "console.log(JSON.stringify(out)); }).catch((e) => { console.error(e); process.exit(1); });")
    out = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, encoding="utf-8",
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ---- 정적 계약 -----------------------------------------------------------------------------

def test_admin_page_and_utils_are_served(client):
    for rel, token in (("pages/admin.js", "/api/admin/users"), ("admin-utils.js", "loadIsAdmin")):
        status, headers, raw = client.call("GET", f"/static/{rel}")
        assert status == 200 and "javascript" in headers["Content-Type"]
        assert token in raw.decode("utf-8")


def test_admin_page_contract_and_safety():
    js = _js(PAGE)
    for token in ["export async function mount", "export function unmount", "export function unsavedNote",
                  "isCurrent()", "ctx.api(", "/api/admin/users${usersQuery(", "/api/admin/usage?days=",
                  "/api/admin/audit${query}", "/api/admin/overview", "/auth/methods",
                  "/api/admin/users/${encodeURIComponent(d.id)}/update",
                  "/api/admin/users/${encodeURIComponent(d.id)}/delete", "confirm_email: d.typed",
                  "/api/admin/users/${encodeURIComponent(d.id)}`", "logout-all", "clear_password: true",
                  "deleteMatches(d.typed, u.email)", "showModal()", "\"dialog\"", "\"cancel\"",
                  "inline-confirm", "role: \"alert\"", "role: \"tabpanel\"", "aria-controls", "aria-invalid",
                  "aria-describedby", "aria-busy", "autocomplete: \"new-password\"", "불러오는 중…", "다시 시도",
                  "아직 사용자가 없습니다", "조건에 맞는 사용자가 없습니다", "AI를 쓴 기록이 없습니다",
                  "아직 관리 기록이 없습니다", "조건에 맞는 기록이 없습니다", "사용자 추가", "변경 저장",
                  "비밀번호 정하기", "모든 기기에서 로그아웃", "사용자 영구 삭제", "필터 지우기", "취소",
                  "data-label", "내 계정은 비활성으로 바꾸거나 관리자에서 뺄 수 없습니다", "내 계정은 여기서 지울 수 없습니다",
                  "쪽 이동", "서버판에서 꺼진 기능", "로그인 방법"]:
        assert token in js, token
    for bad in ["innerHTML", "outerHTML", "insertAdjacentHTML", "confirm(", "alert(", "prompt(", "localStorage",
                "sessionStorage", "console.log"]:
        assert bad not in js.replace("inline-confirm", "").replace("confirmBox(", ""), bad
    assert not re.search(r"https?://", js)
    imports = [line for line in js.splitlines() if line.startswith("import ")]
    assert imports[:2] == ['import { h, fill } from "/static/dom.js";', 'import { button, tabs } from "/static/ui.js";']


def test_admin_page_never_puts_a_password_back_on_screen():
    js = _js(PAGE)
    # the typed password goes into the request body only; the boxes are emptied after saving; no value= for passwords
    assert 'd.password.value = ""' in js and 'c.password.value = ""' in js
    assert re.search(r'type: "password"[^}]*value:', js) is None
    assert "password_hash" not in js and "scrypt" not in js


def test_delete_button_waits_for_the_typed_email():
    js = _js(PAGE)
    block = js.split("function dangerZone(u)", 1)[1].split("async function removeUser", 1)[0]
    assert "disabled: d.busy || !deleteMatches(d.typed, u.email) || null" in block
    assert "go.disabled = d.busy || !deleteMatches(d.typed, u.email);" in block
    remove = js.split("async function removeUser(u)", 1)[1].split("\n}\n", 1)[0]
    assert "!deleteMatches(d.typed, u.email)) return;" in remove


def test_admin_utils_has_no_dom_and_no_imports():
    js = _js(UTILS)
    assert [line for line in js.splitlines() if line.startswith("import ")] == []
    for bad in ["document.", "innerHTML", "window."]:
        assert bad not in js, bad


def test_app_routes_admin_and_shows_the_entry_only_for_admins():
    app = _js(STATIC_DIR / "app.js")
    assert 'case "admin": return { page: "admin", params: {} };' in app
    assert '{ route: "#/admin", name: "관리" }' in app
    assert 'import { loadIsAdmin } from "/static/admin-utils.js";' in app
    assert "showAdminEntry(await loadIsAdmin(api, features.edition));" in app
    # both entries start hidden; only showAdminEntry(true) reveals them
    assert re.search(r'id: "admin-link", href: "#/admin", hidden: true', app)
    assert re.search(r'id: "admin-pop", href: "#/admin", hidden: true', app)
    # the help chatbot's screen list (SCREENS) does not grow
    block = app.split("const SCREENS = [", 1)[1].split("];", 1)[0]
    assert "#/admin" not in block
    css = _js(STATIC_DIR / "app.css")
    assert ".pop-item[hidden], .topbar-admin[hidden] { display: none; }" in css
    assert "@media (max-width: 640px)" in css.split("/* Admin screen", 1)[1]
    assert "content: attr(data-label)" in css


# ---- admin-utils.js ------------------------------------------------------------------------

@needs_node
def test_load_is_admin_asks_only_the_server_edition():
    out = run_js(UTILS, """
      const calls = [];
      const api = async (method, path) => { calls.push(path); return { email: "a", is_admin: true }; };
      const local = await m.loadIsAdmin(api, "local");
      const server = await m.loadIsAdmin(api, "server");
      const no = await m.loadIsAdmin(async () => ({ is_admin: "yes" }), "server");
      const err = await m.loadIsAdmin(async () => { throw { status: 401 }; }, "server");
      return { local, server, no, err, calls };""")
    assert out == {"local": False, "server": True, "no": False, "err": False, "calls": ["/api/me"]}


@needs_node
def test_spend_status_and_login_methods():
    out = run_js(UTILS, """
      const u = { today_usd: 1.2, effective_daily_usd: 1, daily_usd_limit: null, is_admin: true, disabled: false,
                  is_self: true, has_password: true, has_google: true };
      return {
        spend: m.spend(u), under: m.spend({ today_usd: 0.25, effective_daily_usd: 2, daily_usd_limit: 2 }),
        chips: m.statusChips(u).map((c) => c.label), off: m.statusChips({ disabled: true }).map((c) => c.kind),
        methods: m.loginMethods(u), none: m.loginMethods({}), usd: [m.usd(0), m.usd(1.005), m.usd(null)],
      };""")
    assert out["spend"]["text"] == "$1.20 / $1.00" and out["spend"]["over"] is True and out["spend"]["note"] == "기본 한도"
    assert out["under"] == {"text": "$0.25 / $2.00", "note": "개별 한도", "over": False,
                            "label": "오늘 $0.25 사용, 한도 $2.00(개별)"}
    assert out["chips"] == ["사용 중", "관리자", "나"] and out["off"] == ["off"]
    assert out["methods"] == ["비밀번호", "Google"] and out["none"] == ["아직 없음"]
    assert out["usd"][0] == "$0.00" and out["usd"][2] == "—"


@needs_node
def test_create_and_limit_checks_match_the_server_rules():
    out = run_js(UTILS, """
      return {
        ok: m.checkCreate({ email: " a@b.co ", name: " 지수 ", password: "", limit: "", isAdmin: false }),
        full: m.checkCreate({ email: "a@b.co", password: "0123456789", limit: "2.5", isAdmin: true }).body,
        bad: m.checkCreate({ email: "nope", password: "short", limit: "1.234", name: "x".repeat(101) }).errors,
        need: m.checkCreate({ email: "a@b.co", password: "", passwordRequired: true }).errors,
        limits: ["", "0", "3", "3.25", "-1", "1e3", "abc", "1000000"].map((t) => m.parseLimit(t).ok),
        pw: [m.checkPassword("          "), m.checkPassword("0123456789")],
      };""")
    assert out["ok"]["body"] == {"email": "a@b.co", "display_name": "지수", "password": None, "daily_usd_limit": None,
                                 "is_admin": False}
    assert out["full"] == {"email": "a@b.co", "display_name": None, "password": "0123456789", "daily_usd_limit": "2.5",
                           "is_admin": True}
    assert set(out["bad"]) == {"email", "password", "limit", "name"}
    assert set(out["need"]) == {"password"}
    assert out["limits"] == [True, True, True, True, False, False, False, False]
    assert out["pw"][0] is not None and out["pw"][1] is None


@needs_node
def test_audit_lines_and_usage_totals():
    out = run_js(UTILS, """
      return {
        limit: m.auditLine({ action: "set_limit", actor_email: "r@x", target_email: "b@x", detail: { from: 2, to: null } }),
        cli: m.auditLine({ action: "set_admin", actor_email: null, target_email: "b@x", detail: { is_admin: true, via: "cli" } }),
        invite: m.auditLine({ action: "add_user", actor_email: "r@x", target_email: "n@x", detail: { is_admin: false } }),
        email: m.auditLine({ action: "set_email", actor_email: "r@x", target_email: "c@x", detail: { from: "b@x", to: "c@x" } }),
        gone: m.auditLine({ action: "delete_user", actor_email: null, target_email: "b@x",
                            detail: { docs: 3, objects: 2, trash: 1, via: "cli" } }),
        lost: m.auditLine({ action: "logout_all", actor_email: "r@x", target_email: null, detail: {} }),
        anon: m.usageTotals([{ email: null, calls: 1, failed: 0, cost_usd: 0.25 }]),
        totals: m.usageTotals([{ email: "a", calls: 1, failed: 0, cost_usd: 0.5 }, { email: "b", calls: 2, failed: 1, cost_usd: 1 },
                               { email: "a", calls: 3, failed: 0, cost_usd: 0.75 }]),
        err: [m.adminErrorText({ status: 404, error: "not_found" }), m.adminErrorText({ status: 403, error: "admin_only" }),
              m.adminErrorText({ status: 409, error: "last_admin", message: "마지막" })],
      };""")
    assert out["limit"] == {"who": "r@x", "what": "하루 한도 $2.00 → 기본", "whom": "b@x"}
    assert out["cli"] == {"who": "운영자(명령줄)", "what": "관리자로 지정", "whom": "b@x"}
    assert out["invite"]["what"] == "사용자 추가"
    assert out["email"]["what"] == "이메일 변경 b@x → c@x"
    assert out["gone"] == {"who": "운영자(명령줄)", "what": "사용자 삭제(문서 3개, 파일 2개, 휴지통 1개)", "whom": "b@x"}
    assert out["lost"]["whom"] == "삭제된 사용자"
    assert out["anon"] == [{"email": "삭제된 사용자", "calls": 1, "failed": 0, "cost": 0.25}]
    assert out["totals"] == [{"email": "a", "calls": 4, "failed": 0, "cost": 1.25},
                             {"email": "b", "calls": 2, "failed": 1, "cost": 1}]
    assert out["err"] == ["관리 화면은 서버판에서만 쓸 수 있습니다.", "관리자만 볼 수 있는 화면입니다.", "마지막"]


@needs_node
def test_feature_info_reports_the_edition():
    out = run_js(STATIC_DIR / "features.js", """
      const srv = await m.loadFeatureInfo(async () => ({ edition: "server", disabled: { library: "x" } }));
      const old = await m.loadFeatureInfo(async () => ({ disabled: {} }));
      const down = await m.loadFeatureInfo(async () => { throw { status: 0 }; });
      const plain = await m.loadFeatures(async () => ({ edition: "server", disabled: { footage: "y" } }));
      return { srv, old, down, plain };""")
    assert out == {"srv": {"edition": "server", "disabled": {"library": "x"}},
                   "old": {"edition": "local", "disabled": {}}, "down": {"edition": "local", "disabled": {}},
                   "plain": {"footage": "y"}}


@needs_node
def test_edit_check_sends_only_changes_and_warns_before_risky_ones():
    out = run_js(UTILS, """
      const u = { email: "b@x.co", display_name: "밥", daily_usd_limit: 2, is_admin: false, disabled: false,
                  has_google: true, is_self: false };
      const same = { email: "B@x.co ", name: " 밥 ", limit: "2.00", isAdmin: false, enabled: true };
      return {
        none: m.checkEdit(u, same),
        name: m.checkEdit(u, { ...same, name: "" }),
        limit: m.checkEdit(u, { ...same, limit: "" }),
        email: m.checkEdit(u, { ...same, email: "New@x.co" }),
        off: m.checkEdit(u, { ...same, enabled: false, isAdmin: true }),
        bad: m.checkEdit(u, { ...same, email: "nope", limit: "-1" }),
        self: m.checkEdit({ ...u, is_self: true, has_google: false }, { ...same, email: "me@x.co" }).warnings.length,
      };""")
    assert out["none"]["body"] is None and out["none"]["changed"] == [] and out["none"]["warnings"] == []
    assert out["name"]["body"] == {"display_name": None} and out["name"]["warnings"] == []
    assert out["limit"]["body"] == {"daily_usd_limit": None}
    assert out["email"]["body"] == {"email": "new@x.co"} and "Google" in out["email"]["warnings"][0]
    assert out["off"]["body"] == {"is_admin": True, "disabled": True} and len(out["off"]["warnings"]) == 2
    assert set(out["bad"]["errors"]) == {"email", "limit"} and out["bad"]["body"] is None
    assert out["self"] == 2


@needs_node
def test_query_strings_pages_and_delete_match():
    out = run_js(UTILS, """
      return {
        plain: m.usersQuery(m.DEFAULT_USERS_QUERY),
        full: m.usersQuery({ q: " kim & co ", status: "disabled", role: "admin", login: "none", sort: "last_login:desc",
                             page: 3 }),
        rev: m.usersQuery({ ...m.DEFAULT_USERS_QUERY, sort: "created:asc" }),
        filters: [m.hasUserFilters(m.DEFAULT_USERS_QUERY), m.hasUserFilters({ ...m.DEFAULT_USERS_QUERY, role: "admin" })],
        audit: m.auditQuery({ actor: "r", action: "delete_user", target: "", from: "2026-11-01", to: "" }, 2),
        noAudit: m.auditQuery(m.DEFAULT_AUDIT_FILTERS),
        range: [m.checkRange("2026-11-03", "2026-11-02"), m.checkRange("2026-11-01", "2026-11-02"), m.checkRange("", "x")],
        pages: [m.pageText({ page: 1, per_page: 50, total: 120 }), m.pageText({ page: 3, per_page: 50, total: 120 }),
                m.pageText({ total: 0 }), m.pageText({ page: 1, per_page: 50, total: 7 }, "건")],
        del: [m.deleteMatches(" B@X.co ", "b@x.co"), m.deleteMatches("b@x.c", "b@x.co"), m.deleteMatches("", ""),
              m.deleteMatches(null, "b@x.co")],
      };""")
    assert out["plain"] == ""
    assert out["full"] == "?q=kim+%26+co&status=disabled&role=admin&login=none&sort=last_login&order=desc&page=3"
    assert out["rev"] == "?sort=created"
    assert out["filters"] == [False, True]
    assert out["audit"] == "?actor=r&action=delete_user&from=2026-11-01&page=2" and out["noAudit"] == ""
    assert out["range"][0] is not None and out["range"][1] is None and out["range"][2] is None
    assert out["pages"] == ["120명 중 1–50", "120명 중 101–120", "0명", "7건 중 1–7"]
    assert out["del"] == [True, False, False, False]


@needs_node
def test_bytes_dates_logins_and_server_lines():
    out = run_js(UTILS, """
      const now = Date.parse("2026-11-03T12:00:00Z");
      return {
        bytes: [m.bytesText(0), m.bytesText(1023), m.bytesText(1536), m.bytesText(5 * 1024 * 1024), m.bytesText(-1)],
        login: [m.loginText(null, now), m.loginText("2026-11-03T11:59:40Z", now), m.loginText("2026-11-03T11:15:00Z", now),
                m.loginText("2026-11-03T02:00:00Z", now), m.loginText("2026-10-30T12:00:00Z", now)],
        lines: m.loginLines({ google: false, password: true, dev: true }).map((l) => [l.label, l.on, !!l.warn]),
        done: m.deleteDoneText({ email: "b@x", deleted: { docs: 1, objects: 2, bytes: 2048 }, storage: { ok: false, left: 3 } }),
        opts: m.actionOptions(["delete_user", "weird"]),
        tabs: m.TABS.map((t) => t.label),
      };""")
    assert out["bytes"] == ["0 B", "1023 B", "1.5 KB", "5.0 MB", "—"]
    assert out["login"] == ["아직 없음", "방금", "45분 전", "10시간 전", "4일 전"]
    assert out["lines"] == [["Google 로그인", False, False], ["이메일 + 비밀번호", True, False], ["개발용 로그인", True, True]]
    assert out["done"]["warn"] is True and "3개" in out["done"]["text"] and "2.0 KB" in out["done"]["text"]
    assert out["opts"] == [{"value": "", "label": "모든 동작"}, {"value": "delete_user", "label": "사용자 삭제"},
                           {"value": "weird", "label": "weird"}]
    assert out["tabs"] == ["사용자", "사용량", "감사 기록", "서버 정보"]

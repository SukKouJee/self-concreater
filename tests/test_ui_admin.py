"""관리 화면(서버판 #/admin, T16): 정적 계약 + admin-utils.js 단위(node 없으면 그 부분만 skip).

- 화면은 vanilla ES 모듈, `h()`만(innerHTML·브라우저 confirm 없음), 확인은 화면 안 확인 상자.
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
                  "isCurrent()", "ctx.api(", "/api/admin/users", "/api/admin/usage?days=", "/api/admin/audit?limit=",
                  "/auth/methods", "act(u, \"password\"", "clear-password", "logout-all", "disable", "enable",
                  "act(u, \"limit\"", "act(u, \"admin\"", "/api/admin/users/${encodeURIComponent(u.id)}/${action}",
                  "inline-confirm", "role: \"alert\"", "aria-expanded", "aria-controls", "aria-invalid",
                  "aria-describedby", "autocomplete: \"new-password\"", "불러오는 중…", "다시 시도",
                  "아직 초대한 사람이 없습니다", "AI를 쓴 기록이 없습니다", "아직 관리 기록이 없습니다", "초대",
                  "비밀번호 저장", "모든 기기에서 로그아웃", "비활성으로 바꾸기", "다시 쓰게 하기", "관리자로 지정",
                  "관리자에서 빼기", "취소", "Escape", "data-label", "내 계정은 비활성으로 바꾸거나 관리자에서 뺄 수 없습니다"]:
        assert token in js, token
    for bad in ["innerHTML", "outerHTML", "insertAdjacentHTML", "confirm(", "alert(", "prompt(", "localStorage",
                "sessionStorage", "console.log"]:
        assert bad not in js.replace("inline-confirm", "").replace("confirmBox(", ""), bad
    assert not re.search(r"https?://", js)
    imports = [line for line in js.splitlines() if line.startswith("import ")]
    assert imports[:2] == ['import { h, fill } from "/static/dom.js";', 'import { button } from "/static/ui.js";']


def test_admin_page_never_puts_a_password_back_on_screen():
    js = _js(PAGE)
    # the typed password goes into the request body only; the box is emptied after saving; no value= for passwords
    assert 'S.inputs.password.value = ""' in js and "f.form.reset()" in js
    assert re.search(r'type: "password"[^}]*value:', js) is None
    assert "password_hash" not in js and "scrypt" not in js


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
def test_invite_and_limit_checks_match_the_server_rules():
    out = run_js(UTILS, """
      return {
        ok: m.checkInvite({ email: " a@b.co ", name: " 지수 ", password: "", limit: "", isAdmin: false }),
        full: m.checkInvite({ email: "a@b.co", password: "0123456789", limit: "2.5", isAdmin: true }).body,
        bad: m.checkInvite({ email: "nope", password: "short", limit: "1.234", name: "x".repeat(101) }).errors,
        limits: ["", "0", "3", "3.25", "-1", "1e3", "abc", "1000000"].map((t) => m.parseLimit(t).ok),
        pw: [m.checkPassword("          "), m.checkPassword("0123456789")],
      };""")
    assert out["ok"]["body"] == {"email": "a@b.co", "display_name": "지수", "password": None, "daily_usd_limit": None,
                                 "is_admin": False}
    assert out["full"] == {"email": "a@b.co", "display_name": None, "password": "0123456789", "daily_usd_limit": "2.5",
                           "is_admin": True}
    assert set(out["bad"]) == {"email", "password", "limit", "name"}
    assert out["limits"] == [True, True, True, True, False, False, False, False]
    assert out["pw"][0] is not None and out["pw"][1] is None


@needs_node
def test_audit_lines_and_usage_totals():
    out = run_js(UTILS, """
      return {
        limit: m.auditLine({ action: "set_limit", actor_email: "r@x", target_email: "b@x", detail: { from: 2, to: null } }),
        cli: m.auditLine({ action: "set_admin", actor_email: null, target_email: "b@x", detail: { is_admin: true, via: "cli" } }),
        invite: m.auditLine({ action: "add_user", actor_email: "r@x", target_email: "n@x", detail: { is_admin: false } }),
        totals: m.usageTotals([{ email: "a", calls: 1, failed: 0, cost_usd: 0.5 }, { email: "b", calls: 2, failed: 1, cost_usd: 1 },
                               { email: "a", calls: 3, failed: 0, cost_usd: 0.75 }]),
        err: [m.adminErrorText({ status: 404, error: "not_found" }), m.adminErrorText({ status: 403, error: "admin_only" }),
              m.adminErrorText({ status: 409, error: "last_admin", message: "마지막" })],
      };""")
    assert out["limit"] == {"who": "r@x", "what": "일 한도 $2.00 → 기본", "whom": "b@x"}
    assert out["cli"] == {"who": "운영자(명령줄)", "what": "관리자로 지정", "whom": "b@x"}
    assert out["invite"]["what"] == "초대"
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

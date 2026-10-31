"""두 사용자 격리(서버 설계 §10-3): 실제 PG 16 + MinIO 위의 ASGI 앱 + dev 로그인. 환경이 없으면 skip.

A가 문서(기획안)·객체(콘티 HTML·리포트)·작업을 만들면 B의 목록·`/files`·작업 조회에 보이지 않는다(RLS·사용자별 Registry).
공유 캐시(`workspace/frames`)만 두 사용자 모두에게 보인다. 세션: 비로그인 401, 비활성·epoch 증가로 세션이 죽는다.
DB 직접 RLS 단언(app.user_id 없이 0행, 남의 행 INSERT 거부)은 test_db_rls.py.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from scripts import ctx, keys, refstore
from scripts.ctx import Ctx
from scripts.ui.jobs import new_item
from tests.test_draft import DRAFT

CAT = "travel"


def _as(stack: Any, email: str) -> Ctx:
    return Ctx(user_id=str(stack.users[email]), backend=stack.backend)


def _seed(stack: Any, email: str) -> Any:
    """로그인 + 작업 폴더 초기화(HTTP) + 카테고리(코어 함수, 그 사용자의 Ctx)."""
    client = stack.login(email)
    assert client.post("/api/init", json={}).status_code == 200
    with ctx.use(_as(stack, email)):
        refstore.init_category(CAT)
    return client


@pytest.fixture
def pair(asgi_stack: Any) -> tuple[Any, Any, Any]:
    return asgi_stack, _seed(asgi_stack, "a@example.com"), _seed(asgi_stack, "b@example.com")


def test_drafts_of_one_user_are_invisible_to_the_other(pair):
    stack, a, b = pair
    saved = a.post("/api/draft/save", json={"category": CAT, "draft": DRAFT})
    assert saved.status_code == 200, saved.text
    key = saved.json()["path"]
    assert [d["path"] for d in a.get("/api/draft/list", params={"category": CAT}).json()["drafts"]] == [key]
    assert b.get("/api/draft/list", params={"category": CAT}).json()["drafts"] == []
    r = b.get("/api/draft", params={"path": key})
    assert r.status_code in (400, 404) and r.status_code != 500
    with ctx.use(_as(stack, "b@example.com")):
        assert not ctx.here().store.exists(key)
    # the doc itself through /files (drafts/*.md is served): A yes, B no
    assert a.get(f"/files/{key}").status_code == 200
    assert b.get(f"/files/{key}").status_code == 404


def test_status_shows_only_own_categories(pair):
    stack, a, b = pair
    with ctx.use(_as(stack, "a@example.com")):
        refstore.init_category("food")
    a_cats = {c["category"] for c in a.get("/api/status").json()["categories"]}
    b_cats = {c["category"] for c in b.get("/api/status").json()["categories"]}
    assert a_cats == {CAT, "food"} and b_cats == {CAT}
    assert a.get("/api/status").json()["work_root"] == "ytc:a@example.com"
    assert b.get("/api/status").json()["work_root"] == "ytc:b@example.com"


def test_objects_and_files_are_isolated_but_shared_frames_are_shared(pair):
    stack, a, b = pair
    html = keys.html_of(keys.conti_md(keys.draft_md("trip")))
    report = keys.report_html(CAT, "2026-10-31")
    frame = keys.frames_dir("vid12345678") + "0001.jpg"
    with ctx.use(_as(stack, "a@example.com")):
        ctx.here().objects.put(html, b"<html>A conti</html>")
        ctx.here().objects.put(report, b"<html>A report</html>")
        ctx.here().shared.put(frame, b"\xff\xd8jpeg")
    assert a.get(f"/files/{html}").content == b"<html>A conti</html>"
    assert a.get(f"/files/{report}").status_code == 200
    for key in (html, report):
        r = b.get(f"/files/{key}")
        assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert a.get(f"/files/{frame}").content == b"\xff\xd8jpeg"
    assert b.get(f"/files/{frame}").content == b"\xff\xd8jpeg"
    # B writing the same key does not touch A's object
    with ctx.use(_as(stack, "b@example.com")):
        ctx.here().objects.put(html, b"<html>B conti</html>")
    assert a.get(f"/files/{html}").content == b"<html>A conti</html>"
    assert b.get(f"/files/{html}").content == b"<html>B conti</html>"


def test_jobs_are_invisible_and_untouchable_across_users(pair):
    stack, a, b = pair
    release = threading.Event()
    item = new_item("q1", title="질문", step="claude")
    a_jobs = stack.registry.for_user(stack.users["a@example.com"])
    job = a_jobs.start("help_chat", lambda j: (j.wait_decision("q1"), release.wait(5)), items=[item])
    try:
        assert a.get(f"/api/jobs/{job.id}").json()["job"]["id"] == job.id
        for method, path, body in [("GET", f"/api/jobs/{job.id}", None),
                                   ("POST", f"/api/jobs/{job.id}/cancel", {}),
                                   ("POST", f"/api/jobs/{job.id}/items/q1/decision", {"action": "skip"})]:
            r = b.request(method, path, json=body)
            assert r.status_code == 400 and r.json()["error"] == "job_not_found", (path, r.text)
        assert b.get("/api/jobs/current", params={"kind": "help_chat"}).json() == {"job": None}
        assert a.get("/api/jobs/current", params={"kind": "help_chat"}).json()["job"]["id"] == job.id
        assert job.is_active()                                  # B's cancel did nothing
    finally:
        release.set()
        a_jobs.shutdown(timeout=5)


def test_no_session_is_401_on_the_real_stack(asgi_stack):
    c = asgi_stack.client()
    assert c.get("/api/status").status_code == 401
    assert c.get("/files/drafts/x.html").status_code == 401
    assert c.get("/", follow_redirects=False).headers["location"] == "/auth/login"


@pytest.mark.parametrize("sql", ["UPDATE users SET disabled = true WHERE id = %s",
                                 "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s"])
def test_disable_or_epoch_bump_kills_live_sessions(asgi_stack, sql):
    a = asgi_stack.login("a@example.com")
    other = asgi_stack.login("a@example.com")            # a second browser of the same user
    assert a.get("/api/ping").json()["user"] == "a@example.com"
    asgi_stack.owner_sql(sql, asgi_stack.users["a@example.com"])
    for c in (a, other):
        r = c.get("/api/status")
        assert r.status_code == 401 and r.json()["error"] == "login_required"


def test_disabled_user_cannot_log_in(asgi_stack):
    uid = asgi_stack.login("a@example.com") and asgi_stack.users["a@example.com"]
    asgi_stack.owner_sql("UPDATE users SET disabled = true WHERE id = %s", uid)
    r = asgi_stack.client().post("/auth/dev", json={"email": "a@example.com"})
    assert r.status_code == 403 and r.json()["error"] == "login_denied"


def test_unknown_email_cannot_log_in(asgi_stack):
    r = asgi_stack.client().post("/auth/dev", json={"email": f"{uuid.uuid4().hex}@example.com"})
    assert r.status_code == 403


def test_cross_origin_post_is_refused_on_the_real_stack(asgi_stack):
    a = asgi_stack.login("a@example.com")
    r = a.post("/api/init", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = a.post("/api/init", content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 403 and r.json()["error"] == "bad_content_type"
    with ctx.use(_as(asgi_stack, "a@example.com")):
        assert not ctx.here().store.exists(keys.PROFILE)     # nothing was written


def test_healthz_on_the_real_stack(asgi_stack):
    assert asgi_stack.client().get("/healthz").json() == {"ok": True}


def test_public_tx_sees_users_but_no_user_rows(asgi_stack):
    a = asgi_stack.login("a@example.com")
    assert a.post("/api/init", json={}).status_code == 200
    with asgi_stack.backend.db.public_tx() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0

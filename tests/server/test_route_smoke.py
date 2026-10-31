"""라우트 전수 스모크(서버 설계 §8·§10-4): `ROUTES`의 모든 라우트를 서버 백엔드(실제 PG 16 + MinIO) 위에서
로그인한 사용자로 부른다. 500이 하나도 없어야 한다(4xx는 정상: 빈·최소 본문의 검증 오류, 없는 작업, 비활성 기능 403).

- 사용자 A의 계정은 동등성 시나리오(`test_equivalence.scenario`: 카테고리·레퍼런스·유형화·모티브·기획안·콘티·미션·리포트·휴지통)
  + 살아 있는 기획안·콘티 하나로 채운다. 그래서 대부분의 라우트가 검증을 지나 저장소까지 간다.
- 세 번 부른다: ① 빈 쿼리·본문 ② 흔한 매개변수(category·path=기획안·key·day·week …)를 채운 것 ③ ②에서 path=콘티.
  GET을 먼저, 그다음 POST.
- LLM·YouTube는 가짜(`llm._engine`, `fetch.fetch`)라 시작된 작업은 가짜 오류로 끝난다. 작업이 서버 백엔드의
  숨은 로컬 가정(NotImplementedError·작업 폴더 없음)으로 실패하지 않았는지도 본다.
- 결과 표는 `-s`로 보면 찍힌다(보고서용).
"""
from __future__ import annotations

import json
import time
import types
from collections import Counter
from typing import Any

import pytest

from scripts import conti, ctx, fetch, keys, llm
from scripts.common import ScriptError
from scripts.ctx import Ctx
from scripts.ui import server as ui
from tests.server import test_equivalence as eq

DISABLED_PREFIXES = tuple(p for prefixes in ui.FEATURE_PREFIXES.values() for p in prefixes)
SERVER_OVERRIDES = {("GET", "/"), ("GET", "/files/(?P<rel>.+)"), ("GET", "/api/ping"), ("POST", "/api/shutdown"),
                    ("GET", "/healthz"), ("GET", "/static/(?P<rel>.+)")}
GROUPS = {"job_id": "nojob00", "item_id": "x1", "video_id": "refA1", "preview_id": "nopreview", "fid": "nofid",
          "rel": "app.js", "page": "draft"}
LOCAL_ASSUMPTIONS = ("NotImplementedError", "작업 폴더(root)", "RuntimeError")


def _fake_engine() -> types.SimpleNamespace:
    def ask(*args: Any, **kwargs: Any) -> dict:
        raise ScriptError("llm_failed", "테스트: 가짜 LLM은 답하지 않습니다")
    return types.SimpleNamespace(check=lambda: None, ask=ask, DEFAULT_MODEL="fake")


def _path_for(rt: ui.Route) -> str:
    pattern = rt.pattern.pattern
    for name, value in GROUPS.items():
        pattern = pattern.replace(f"(?P<{name}>[^/]+)", value).replace(f"(?P<{name}>[^/?]+)", value) \
            .replace(f"(?P<{name}>.+)", value).replace(f"(?P<{name}>[a-z_-]+)", value)
    assert "(?P<" not in pattern, pattern
    return pattern


@pytest.fixture
def seeded(asgi_stack: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, dict[str, str]]:
    monkeypatch.setattr(llm, "_engine", lambda: (_fake_engine(), {"engine": "fake"}))

    def no_youtube(*args: Any, **kwargs: Any) -> dict:
        raise ScriptError("fetch_failed", "테스트: YouTube에 가지 않습니다")
    monkeypatch.setattr(fetch, "fetch", no_youtube)

    client = asgi_stack.login("a@example.com")
    c = Ctx(user_id=str(asgi_stack.users["a@example.com"]), backend=asgi_stack.backend)
    eq.scenario(c, monkeypatch)
    saved = client.post("/api/draft/save", json={"category": eq.CAT, "draft": eq.DRAFT})
    assert saved.status_code == 200, saved.text
    draft_key = saved.json()["path"]
    with ctx.use(c):
        made = conti.save(draft_key, eq._handoff(c, "conti.json", eq.CONTI), now=eq.T0)
    params = {"category": eq.CAT, "path": draft_key, "key": made["path"], "conti": made["path"], "draft": draft_key,
              "day": eq.DAY, "date": eq.DAY, "week": "2026-W44", "kind": "collect", "id": "refA1", "n": "1",
              "a": "1", "b": "2", "version": "1"}
    return asgi_stack, client, params


def _error(r: Any) -> str:
    try:
        body = r.json()
    except ValueError:
        return "-"
    return str(body.get("error") or "ok") if isinstance(body, dict) else "ok"


def _call(client: Any, rt: ui.Route, params: dict[str, str] | None) -> Any:
    path = _path_for(rt)
    if rt.method == "GET":
        return client.get(path, params=params or None)
    return client.request(rt.method, path, json=params or {})


def test_every_route_answers_without_500(seeded, capsys):
    stack, client, params = seeded
    routes = sorted(ui.ROUTES, key=lambda r: r.method != "GET")       # GETs first: POSTs change the account
    table: list[tuple[str, str, list[str]]] = []
    failures: list[str] = []
    passes = (None, params, {**params, "path": params["conti"]})
    for rt in routes:
        statuses = []
        for p in passes:
            r = _call(client, rt, p)
            statuses.append(f"{r.status_code} {_error(r)}")
            if r.status_code >= 500:
                failures.append(f"{rt.method} {rt.pattern.pattern} {p is not None}: {r.status_code} {r.text[:300]}")
            path = _path_for(rt)
            if path.startswith(DISABLED_PREFIXES) and (rt.method, rt.pattern.pattern) not in SERVER_OVERRIDES:
                assert r.status_code == 403 and r.json()["error"] == "feature_disabled", (path, r.text)
        table.append((rt.method, rt.pattern.pattern, statuses))
    for page in ("home", "refs", "library", "typify", "report", "motif", "draft", "conti", "diagnose", "mission",
                 "guide", "video"):
        r = client.get(f"/api/help/{page}", params={"category": eq.CAT})
        if r.status_code >= 500:
            failures.append(f"GET /api/help/{page}: {r.status_code} {r.text[:300]}")

    jobs = _settle(stack)
    with capsys.disabled():
        print("\n--- route smoke (empty | filled path=draft | filled path=conti) ---")
        for method, pattern, statuses in table:
            print(f"{method:4s} {pattern:60s} " + " | ".join(statuses))
        print("status counts:", dict(Counter(s.split()[0] for row in table for s in row[2])))
        print("jobs:", json.dumps(jobs, ensure_ascii=False))
    assert failures == []
    bad_jobs = [j for j in jobs if any(m in json.dumps(j, ensure_ascii=False) for m in LOCAL_ASSUMPTIONS)]
    assert bad_jobs == []


def _settle(stack: Any) -> list[dict[str, Any]]:
    """시작된 작업들이 끝나기를 잠깐 기다리고 (kind, status, 항목 메시지)를 돌려준다."""
    manager = stack.registry.for_user(stack.users["a@example.com"])
    deadline = time.monotonic() + 20
    jobs = list(manager._jobs.values())
    while time.monotonic() < deadline and any(j.is_active() and not _awaiting(j) for j in jobs):
        time.sleep(0.1)
    out = []
    for j in jobs:
        snap = j.snapshot()
        out.append({"kind": snap["kind"], "status": snap["status"],
                    "messages": sorted({str(i.get("message")) for i in snap["items"] if i.get("message")})})
    return out


def _awaiting(job: Any) -> bool:
    return job.snapshot()["status"] == "awaiting"


def test_help_pages_and_drafts_sources_work_with_library_off(seeded):
    _, client, params = seeded
    r = client.get("/api/draft/sources", params={"category": eq.CAT})
    assert r.status_code == 200 and "library_templates" not in r.json()
    r = client.post("/api/draft/run", json={"category": eq.CAT, "source": "library:someone-x"})
    assert r.status_code == 400 and r.json()["error"] == "library_item_missing"
    assert "서버판" in r.json()["message"]
    r = client.post("/api/motif/run", json={"category": eq.CAT, "library_ids": ["someone-x"]})
    assert r.status_code == 400 and r.json()["error"] in {"library_item_missing", "job_running"}


def test_mission_submit_with_a_pc_path_is_a_clean_400(seeded):
    _, client, _ = seeded
    r = client.post("/api/mission/submit", json={"day": eq.DAY, "file": "C:/Users/me/clip.mp4", "note": "x"})
    assert 400 <= r.status_code < 500, r.text

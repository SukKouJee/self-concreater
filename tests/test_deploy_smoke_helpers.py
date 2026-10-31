"""deploy/smoke.py의 브라우저 점검 도우미 단위 테스트(Docker·Chrome 없이)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "deploy" / "smoke.py"
spec = importlib.util.spec_from_file_location("ytc_deploy_smoke", SMOKE)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

CSP_TEXT = "Refused to load the image 'x' because it violates the following Content Security Policy directive: img-src 'self'"


def _log(text: str) -> dict[str, Any]:
    return {"method": "Log.entryAdded", "params": {"entry": {"text": text}}}


class FakePage:
    """wait_event는 진짜 클라이언트처럼 이벤트를 목록에서 꺼내 돌려준다."""

    def __init__(self, events: list[dict[str, Any]], pending: list[dict[str, Any]]) -> None:
        self.events = events
        self.pending = pending

    def wait_event(self, name: str, timeout: float = 20.0) -> dict[str, Any]:
        if not self.pending:
            raise smoke.SmokeFailure("timeout")
        return self.pending.pop(0)


def test_collect_csp_violations_keeps_the_event_that_wait_event_returned() -> None:
    page = FakePage(events=[], pending=[_log(CSP_TEXT)])
    assert smoke.collect_csp_violations(page) == [CSP_TEXT]


def test_collect_csp_violations_also_scans_events_already_received() -> None:
    page = FakePage(events=[_log(CSP_TEXT), _log("other message")], pending=[])
    assert smoke.collect_csp_violations(page) == [CSP_TEXT]


def test_collect_csp_violations_ignores_other_logs_and_counts_exactly_one() -> None:
    page = FakePage(events=[_log("deprecated API")], pending=[_log(CSP_TEXT)])
    assert len(smoke.collect_csp_violations(page)) == 1


def test_collect_csp_violations_is_empty_without_logs() -> None:
    assert smoke.collect_csp_violations(FakePage(events=[], pending=[])) == []


def test_browser_launch_failure_is_a_skip_not_a_failure(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-chrome")
    with pytest.raises(smoke.BrowserUnavailable):
        smoke.browser_check(missing, "http://127.0.0.1:1", ("ytc_session", "x"), "/files/x.html")

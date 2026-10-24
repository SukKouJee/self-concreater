import pytest


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    """개발자 환경의 엔진·키 설정이 테스트에 새지 않게 한다."""
    monkeypatch.delenv("YTC_LLM_ENGINE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

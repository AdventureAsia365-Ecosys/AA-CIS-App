"""S224: S1_WRITER_TEMPERATURE sets the S1 writer temperature; unset keeps the request unchanged."""
import pytest

from services.content_generation import graph


@pytest.mark.parametrize("raw,expected", [("", {}), ("abc", {}), ("1.5", {}), ("0.4", {"temperature": 0.4}),
                                          ("0", {"temperature": 0.0})])
def test_writer_sampling(monkeypatch, raw, expected):
    monkeypatch.setenv("S1_WRITER_TEMPERATURE", raw)
    assert graph._writer_sampling() == expected


def test_unset_env_sends_no_temperature(monkeypatch):
    from shared.llm_client.models import LLMRequest
    monkeypatch.delenv("S1_WRITER_TEMPERATURE", raising=False)
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="s1_generate", max_tokens=10, **graph._writer_sampling())
    assert "temperature" not in req.model_fields_set

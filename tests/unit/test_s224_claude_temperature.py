"""S224: a temperature set on an LLMRequest reaches Claude on the satellite path; unset sends none."""
import io
import json
from unittest.mock import MagicMock, patch

from shared.llm_client import bedrock_satellite as bs


def _fake_rt():
    rt = MagicMock()
    payload = {"content": [{"text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"}
    rt.invoke_model.return_value = {"body": io.BytesIO(json.dumps(payload).encode())}
    return rt


def _body_sent(**kw):
    rt = _fake_rt()
    session = MagicMock()
    session.client.return_value = rt
    with patch.object(bs, "_get_satellite_session", return_value=session):
        bs.invoke_claude("hi", model="haiku", account="acc3", **kw)
    return json.loads(rt.invoke_model.call_args.kwargs["body"])


def test_temperature_is_sent_when_given():
    assert _body_sent(temperature=0.4)["temperature"] == 0.4


def test_no_temperature_by_default():
    assert "temperature" not in _body_sent()


def test_client_forwards_only_an_explicit_temperature():
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    result = MagicMock(text="ok", usage={"input_tokens": 1, "output_tokens": 1}, stop_reason="end_turn")
    with patch("shared.llm_client.bedrock_satellite.invoke_claude", return_value=result) as inv:
        c = LLMClient.__new__(LLMClient)
        for req, expected in ((LLMRequest(system_prompt="s", user_prompt="u", max_tokens=5, temperature=0.4), 0.4),
                              (LLMRequest(system_prompt="s", user_prompt="u", max_tokens=5), None)):
            try:
                c._call_bedrock_satellite(req, "haiku", "acc3")
            except Exception:
                pass
            assert inv.call_args.kwargs["temperature"] == expected

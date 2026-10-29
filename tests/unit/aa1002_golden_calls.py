"""AA-1002 (gateway slice S2) — capture what today's gateway sends to Bedrock, for the pre-S2 golden snapshot.

``capture()`` runs three calls through ``LLMClient.generate`` with every network seam faked (no AWS, no DB, no key):
a Converse call on a catalog key (``sonnet-5``), the same with a JSON schema (forced tool), and a legacy-chain call
(``haiku``, InvokeModel). It returns, as plain JSON, the provider request kwargs, the satellite client calls and what
the caller got back.

The snapshot ``tests/unit/snapshots/aa1002_pre_s2_calls.json`` was written by running THIS file unchanged against an
``origin/main`` checkout from before S2 (``dd8eea05``, no ``vendor_route``), with the unit-test conftest's env
(``BEDROCK_ACCT_PRIMARY=acc3-test-account ENVIRONMENT=test``)::

    python -I tests/unit/aa1002_golden_calls.py <checkout root> > tests/unit/snapshots/aa1002_pre_s2_calls.json

so ``test_aa1002_no_behaviour_change.py`` compares S2's calls with pre-S2 code, not only with S2's own registry-off
path. The module imports nothing S2 added at module level (it must run on the old checkout too).
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

SCHEMA = {"name": "verdict", "schema": {"type": "object", "properties": {"score": {"type": "integer"}},
                                        "required": ["score"]}}


def _converse_response(structured: bool) -> dict:
    if structured:
        content = [{"toolUse": {"toolUseId": "t1", "name": SCHEMA["name"], "input": {"score": 3}}}]
        stop = "tool_use"
    else:
        content = [{"text": "hello"}]
        stop = "end_turn"
    return {"output": {"message": {"role": "assistant", "content": content}},
            "usage": {"inputTokens": 1000, "outputTokens": 500}, "stopReason": stop}


def _invoke_response():
    body = MagicMock()
    body.read.return_value = (b'{"content": [{"type": "text", "text": "legacy"}], "stop_reason": "end_turn", '
                              b'"usage": {"input_tokens": 10, "output_tokens": 5}}')
    return {"body": body}


def _plain(value):
    """JSON-able copy (call objects become [args, kwargs])."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if type(value).__name__ == "_Call":
            args, kwargs = value
            return [_plain(list(args)), _plain(dict(kwargs))]
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def capture(extra_patches=()) -> dict:
    """The three calls under ``extra_patches`` (context managers entered first, e.g. a registry state)."""
    from shared.llm_client.catalog import CatalogModel
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    sonnet5 = CatalogModel(model_key="sonnet-5", label="Sonnet 5", vendor="anthropic", provider="bedrock",
                           api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5"},
                           callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=2.0,
                           price_out_per_mtok=10.0, enabled=True)
    out: dict = {}
    for name, tier, schema in (("converse", "sonnet-5", None), ("converse_structured", "sonnet-5", SCHEMA),
                               ("legacy_haiku", "haiku", None)):
        rt = MagicMock()
        rt.converse.return_value = _converse_response(schema is not None)
        rt.invoke_model.return_value = _invoke_response()
        with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
            client = LLMClient()
        with ExitStack() as stack:
            for p in extra_patches:
                stack.enter_context(p)
            stack.enter_context(patch("shared.llm_client.client.get_model_sync", return_value=sonnet5))
            stack.enter_context(patch("shared.llm_client.client.get_stage_config_sync", return_value=None))
            stack.enter_context(patch("shared.llm_client.pricing._catalog_rates",
                                      return_value={"in": 0.002, "out": 0.01, "cache_read": 0, "cache_write": 0}))
            gsc = stack.enter_context(patch("shared.llm_client.bedrock_satellite.get_satellite_client",
                                            return_value=rt))
            session = stack.enter_context(patch("shared.llm_client.bedrock_satellite._get_satellite_session"))
            stack.enter_context(patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None))
            session.return_value.client.return_value = rt
            r = client.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier=tier, json_schema=schema))
        out[name] = {
            "converse_kwargs": _plain(rt.converse.call_args.kwargs) if rt.converse.called else None,
            "invoke_kwargs": _plain(rt.invoke_model.call_args.kwargs) if rt.invoke_model.called else None,
            "satellite_calls": _plain(list(gsc.call_args_list)),
            "session_calls": _plain(list(session.call_args_list)),
            "response": {"content": r.content, "model_used": r.model_used, "provider": r.provider,
                         "satellite_account": r.satellite_account, "fallback_used": r.fallback_used,
                         "cost_usd": r.cost_usd, "stop_reason": r.stop_reason, "input_tokens": r.input_tokens,
                         "output_tokens": r.output_tokens},
        }
    return out


if __name__ == "__main__":
    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))   # stdout carries only the JSON
    sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
    from shared.llm_client import catalog, connections, deployments

    catalog.invalidate()
    connections.invalidate()
    deployments.invalidate()
    empty = patch.object(connections, "run_sync", lambda fn: {} if fn is connections._fetch_all else [])
    print(json.dumps(capture([empty]), indent=1, sort_keys=True))

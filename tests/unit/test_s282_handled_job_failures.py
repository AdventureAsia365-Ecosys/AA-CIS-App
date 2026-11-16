"""S282 Sentry triage (AA-CIS-API-1C, -1D, -T) — handled outcomes are warnings, not Sentry error events.

* a typed output failure (OutputFailed: a T9 piece the writer stored as failed) and a business-rule refusal
  (registry.Refused: the one pilot tenant already there) end the job as before (failed at once, error
  ``<code>: <detail>``, reason = code) but the runner logs ``job_failed`` at warning with no traceback;
* any other exception still reaches Sentry with its traceback (tests/unit/test_aa871_request_id_json_logs.py);
* the legacy LLM chain's "GPT-4.1 retired" skip is a warning: the raise right after it is the reported failure.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from shared.jobs import queue, registry
from shared.jobs import worker as worker_mod
from shared.jobs.worker import failure_outcome
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from tests.unit.test_aa871_request_id_json_logs import json_lines, sentry_events  # noqa: F401  (pytest fixtures)


@pytest.mark.parametrize("exc, error, reason", [
    (registry.Refused("pilot_taken", "a pilot tenant already exists"), "pilot_taken: a pilot tenant already exists",
     "pilot_taken"),
    (registry.Refused("slug_taken"), "slug_taken", "slug_taken"),
    (registry.OutputFailed("t9_piece_failed", "SeoEnvelopeError: no_body"),
     "t9_piece_failed: SeoEnvelopeError: no_body", "t9_piece_failed"),
])
def test_a_handled_failure_is_written_as_its_code_and_not_retried(exc, error, reason):
    assert isinstance(exc, registry.NonRetryable)
    assert failure_outcome(exc) == (error, False, reason)


def _run(handler, kind):
    w = worker_mod.Worker(MagicMock(), worker_id="w-s282")
    job = queue.Job("33333333-3333-4333-8333-333333333333", kind, {}, "running", 1, 3, {}, 0.0)
    with patch.dict(registry._KINDS, {kind: registry.JobKind(kind, handler)}), \
            patch.object(queue, "fail", AsyncMock(return_value="failed")) as fail, \
            patch.object(worker_mod, "run_terminal_hook", AsyncMock()):
        asyncio.run(w._execute(job))
    return fail


@pytest.mark.parametrize("exc", [
    registry.Refused("pilot_taken", "a pilot tenant already exists"),
    registry.OutputFailed("t9_piece_failed", "SeoEnvelopeError: no_body"),
], ids=["refused", "output_failed"])
def test_a_handled_failure_is_a_warning_without_a_sentry_event(exc, json_lines, sentry_events):  # noqa: F811
    async def handler(ctx):
        raise exc

    fail = _run(handler, "s282_handled")
    assert fail.await_args.kwargs["retryable"] is False
    assert fail.await_args.args[3] == str(exc)
    line = next(x for x in json_lines() if x["event"] == "job_failed")
    assert line["level"] == "warning" and "exception" not in line and line["reason"] == exc.code
    assert not [e for e in sentry_events if (e.get("logentry") or {}).get("message") == "job_failed"]


def test_a_plain_non_retryable_is_still_an_error_with_traceback(json_lines, sentry_events):  # noqa: F811
    async def handler(ctx):
        raise registry.NonRetryable("payload needs tour_id")

    _run(handler, "s282_plain")
    line = next(x for x in json_lines() if x["event"] == "job_failed")
    assert line["level"] == "error" and "NonRetryable: payload needs tour_id" in line["exception"]
    assert [e for e in sentry_events if (e.get("logentry") or {}).get("message") == "job_failed"]


def test_gpt41_retired_skip_is_a_warning_and_still_raises():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.openai_platform_allowed", return_value=False), capture_logs() as logs:
        with pytest.raises(RuntimeError, match="GPT-4.1 retired"):
            c._legacy_last_resort(req, "haiku", [RuntimeError("bedrock down")])
    assert [e["log_level"] for e in logs if e["event"] == "t3_skipped_gpt41_retired"] == ["warning"]

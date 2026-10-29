"""AA-900 — the LLM availability signal (GET /admin/llm-availability): built from real outcomes, no probe call."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import llm_availability as av

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ERROR_002 = "llm_access_denied: All models in the route failed: Error 002: Access to Bedrock models is not allowed"


def _calls(provider="bedrock-satellite", account="acc3", embed=False, ok_at=None, calls=5):
    return {"provider": provider, "account": account, "embed": embed, "calls": calls, "ok_calls": calls,
            "last_call_at": ok_at, "last_ok_at": ok_at}


def _job(error, at, kind="t2_rewrite", status="failed", job_id="j1"):
    return {"id": job_id, "kind": kind, "status": status, "error": error, "updated_at": at}


def test_blocked_when_the_newest_outcome_is_an_account_level_refusal():
    out = av.build([_calls(ok_at=NOW - timedelta(hours=5))],
                   [_job(ERROR_002, NOW - timedelta(minutes=10), kind="s1_rewrite")], hours=24, now=NOW)
    assert out["status"] == "blocked"
    assert out["cause"]["error_class"] == "access_denied" and out["cause"]["kind"] == "s1_rewrite"
    assert out["failures"][0]["jobs"] == 1


def test_embeddings_on_another_account_do_not_clear_a_block():
    out = av.build([_calls(ok_at=NOW - timedelta(hours=5)),
                    _calls(provider="bedrock", account="acc2", embed=True, ok_at=NOW - timedelta(minutes=1))],
                   [_job(ERROR_002, NOW - timedelta(minutes=10))], hours=24, now=NOW)
    assert out["status"] == "blocked"
    assert out["last_ok_at"] == (NOW - timedelta(hours=5)).isoformat()
    assert {p["embed"] for p in out["providers"]} == {True, False}


def test_ok_when_an_answered_call_is_newer_than_the_last_refusal():
    out = av.build([_calls(ok_at=NOW - timedelta(minutes=1))], [_job(ERROR_002, NOW - timedelta(hours=2))],
                   hours=24, now=NOW)
    assert out["status"] == "ok" and out["cause"] is None


def test_degraded_on_a_recent_throttle():
    out = av.build([_calls(ok_at=NOW - timedelta(hours=1))],
                   [_job("RuntimeError: ThrottlingException: Too many requests", NOW - timedelta(minutes=5),
                         status="queued")], hours=24, now=NOW)
    assert out["status"] == "degraded" and out["cause"]["error_class"] == "throttled"


def test_quota_exhausted_blocks():
    out = av.build([], [_job("llm_access_denied: RateLimitError: insufficient_quota", NOW)], hours=24, now=NOW)
    assert out["status"] == "blocked" and out["cause"]["error_class"] == "quota_exhausted"


def test_unknown_without_calls_or_llm_failures_and_other_errors_are_ignored():
    out = av.build([], [_job("NonRetryable: payload needs tour_id", NOW),
                        _job("RuntimeError: run_tour failed after retries (see logs)", NOW)], hours=24, now=NOW)
    assert out["status"] == "unknown" and out["failures"] == []


def test_failures_are_grouped_with_the_newest_example():
    rows = [_job(ERROR_002, NOW - timedelta(minutes=30), kind="a3_atomize", job_id="old"),
            _job(ERROR_002, NOW - timedelta(minutes=3), kind="t9_write", job_id="new")]
    out = av.build([], rows, hours=24, now=NOW)
    (entry,) = out["failures"]
    assert entry["jobs"] == 2 and entry["last_job_id"] == "new" and entry["last_kind"] == "t9_write"
    assert len(entry["last_error"]) <= av.ERROR_SAMPLE_CHARS


def test_sql_reads_only_and_skips_jev():
    assert "typesafe" in av.CALLS_SQL and "billed_unusable" in av.CALLS_SQL
    for sql in (av.CALLS_SQL, av.JOBS_SQL):
        assert sql.strip().upper().startswith("SELECT")


@pytest.mark.asyncio
async def test_read_passes_the_window_and_row_limit():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[_calls(ok_at=NOW)], []])
    out = await av.read(conn, hours=6, now=NOW)
    assert out["status"] == "ok" and out["window_hours"] == 6
    since = NOW - timedelta(hours=6)
    assert conn.fetch.await_args_list[0].args[1:] == (since,)
    assert conn.fetch.await_args_list[1].args[1:] == (since, av.JOB_ROW_LIMIT)

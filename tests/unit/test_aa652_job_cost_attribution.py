"""AA-652 follow-up — LLM spend is attributed to the durable job that caused it.

The job worker binds the job id (call_log.bind_job) around each handler; every record_call*()
under it writes shared.llm_call_log.job_id (migration 177). The Jobs page sums those rows on read
(queue.LLM_COST_SQL). No live DB — asyncpg mocked; the real-Postgres path is covered by
tests/integration/test_aa650_job_queue.py::test_job_cost_includes_the_llm_calls_it_logged.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.llm_client import call_log


def _kwargs():
    return dict(stage="t2_generate", role="writer", model="sonnet-4-6", tokens_in=1, tokens_out=1,
                cost_usd=0.01, quality_signal={"ok": True})


@pytest.mark.asyncio
async def test_job_id_is_null_outside_a_job():
    conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(**_kwargs())
    assert conn.execute.call_args.args[-1] is None


@pytest.mark.asyncio
async def test_bound_job_id_reaches_tasks_and_threads():
    conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        with call_log.bind_job("11111111-1111-1111-1111-111111111111"):
            task = asyncio.create_task(call_log.record_call(**_kwargs()))
        await task  # created inside the binding, awaited after it ended: still tagged
        assert conn.execute.call_args.args[-1] == "11111111-1111-1111-1111-111111111111"

        with call_log.bind_job("22222222-2222-2222-2222-222222222222"):
            seen = await asyncio.to_thread(call_log.current_job_id)
        assert seen == "22222222-2222-2222-2222-222222222222"
    assert call_log.current_job_id() is None


def test_insert_sql_names_job_id_last():
    sql = " ".join(call_log._INSERT_SQL.split())
    assert "provider, job_id)" in sql and "$15::uuid)" in sql

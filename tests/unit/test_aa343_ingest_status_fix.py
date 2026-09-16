"""
Unit tests for AA-343 Part C: pipeline_runs no longer sticks at 'ingesting' forever on ingest
failure, and ingest-level landed/dropped diagnostics are written to the new ingest_details JSONB
column instead of tours_passed/tours_failed (which already carry different, live semantics
downstream — see migration 091 and services/export/handler.py).

Design confirmed with Nghiep mid-implementation:
- Ingest SUCCESS leaves pipeline_runs.status UNCHANGED ('ingesting') — that status already means
  "ingested, not yet through S1/export" for the rest of the codebase (services/export/handler.py's
  ingesting->completed transition depends on it staying 'ingesting' until export).
- Ingest FAILURE (any exception mid-flow) sets a NEW terminal status 'ingest_failed', with
  completed_at and a non-empty error_message — this is the actual bug (P1.4): ingest death left no
  trace and no distinguishable terminal state from "still running".
- tours_passed / tours_failed are never written by the ingest step at all.
"""
import os
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.ingestion import handler
from shared.repository.raw_tour_repository import RawTourRepository


def _make_conn(fetchrow_return=None, fetchval_return="staging-id-1"):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.execute = AsyncMock(return_value=None)
    conn.close = AsyncMock(return_value=None)
    return conn


def _pipeline_runs_calls(conn, keyword: str):
    """Return conn.execute call args whose SQL text contains `keyword` (case-insensitive)."""
    return [
        call for call in conn.execute.call_args_list
        if "pipeline_runs" in str(call.args[0]) and keyword.lower() in str(call.args[0]).lower()
    ]


async def _run_process_file(records, tour_repo_insert_batch_result, source_insert_side_effect=None):
    """Shared harness mirroring test_aa311_no_step_functions_call.py's mocking style.
    Calls process_file() INSIDE the patch context (all mocks revert on `with` exit, so the
    call must happen before that, not after) and returns (conn_main, result_or_exception)."""
    conn_check = _make_conn(fetchrow_return=None)   # no dedup file-hash match
    conn_main = _make_conn(fetchrow_return=None)     # no existing-tour dedup match -> all new_records

    mock_s3 = MagicMock()
    mock_s3.download_fileobj = MagicMock(return_value=None)
    mock_s3.head_object = MagicMock(return_value={"ContentLength": 1024})

    mock_parser_instance = MagicMock()
    mock_parser_instance.parse.return_value = records

    with patch.object(handler, "_s3", return_value=mock_s3), \
         patch.object(handler, "get_database_url", return_value="postgresql://fake"), \
         patch.object(handler.asyncpg, "connect", AsyncMock(side_effect=[conn_check, conn_main])), \
         patch.object(handler, "ExcelParser", return_value=mock_parser_instance), \
         patch.object(handler, "RawSourceRepository") as mock_source_repo_cls, \
         patch.object(handler, "RawTourRepository") as mock_tour_repo_cls:

        mock_source_repo = MagicMock()
        if source_insert_side_effect is not None:
            mock_source_repo.insert = AsyncMock(side_effect=source_insert_side_effect)
        else:
            mock_source_repo.insert = AsyncMock(return_value="source-id-1")
        mock_source_repo.update_status = AsyncMock(return_value=None)
        mock_source_repo_cls.return_value = mock_source_repo

        mock_tour_repo = MagicMock()
        mock_tour_repo.insert_batch = AsyncMock(return_value=tour_repo_insert_batch_result)
        mock_tour_repo_cls.return_value = mock_tour_repo

        try:
            result = await handler.process_file("aa-cis-bronze", "raw-inbox/Horizon/file.xlsx")
            return conn_main, result, None
        except Exception as exc:  # noqa: BLE001 — T2 needs the raised exception itself
            return conn_main, None, exc


# ── T1: ingest success — status untouched, ingest_details written, tours_passed/failed untouched ──

@pytest.mark.asyncio
async def test_t1_success_leaves_status_ingesting_and_writes_ingest_details():
    records = [{"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"}]
    conn_main, result, exc = await _run_process_file(
        records, tour_repo_insert_batch_result=(["tour-id-1"], []))

    assert exc is None
    assert result["status"] == "done"
    assert result["rows_dropped"] == 0

    # No UPDATE ever sets pipeline_runs.status on the success path — only the initial
    # INSERT ('ingesting') and the ingest_details UPDATE happen.
    status_updates = _pipeline_runs_calls(conn_main, "SET status")
    assert status_updates == []

    tours_passed_writes = _pipeline_runs_calls(conn_main, "tours_passed")
    tours_failed_writes = _pipeline_runs_calls(conn_main, "tours_failed")
    assert tours_passed_writes == []
    assert tours_failed_writes == []

    details_calls = _pipeline_runs_calls(conn_main, "ingest_details")
    assert len(details_calls) == 1
    written = json.loads(details_calls[0].args[2])
    assert written == {"rows_parsed": 1, "rows_landed": 1, "rows_dropped": 0, "drops": []}


# ── T2: ingest fails mid-flow — status='ingest_failed', error_message non-empty, completed_at set ──

@pytest.mark.asyncio
async def test_t2_mid_flow_failure_marks_ingest_failed_not_stuck_ingesting():
    records = [{"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam"}]
    conn_main, result, exc = await _run_process_file(
        records, tour_repo_insert_batch_result=([], []),
        source_insert_side_effect=RuntimeError("db connection reset"),
    )

    assert result is None
    assert isinstance(exc, RuntimeError)

    fail_updates = _pipeline_runs_calls(conn_main, "ingest_failed")
    assert len(fail_updates) == 1
    sql, batch_id_arg, error_message_arg = fail_updates[0].args
    assert "ingest_failed" in sql
    assert "completed_at" in sql
    assert error_message_arg  # non-empty
    assert "db connection reset" in error_message_arg
    assert batch_id_arg  # a real batch_id was passed, not stuck unresolved


# ── T3: rows dropped at insert -> ingest_details.rows_dropped correct, drops[] has reason+sample_ids ──

@pytest.mark.asyncio
async def test_t3_dropped_rows_recorded_in_ingest_details_not_tours_failed():
    records = [
        {"src_name": "Good Tour", "provider": "Horizon Voyages", "country": "Vietnam",
         "tour_id_external": "tour-001", "src_itineraries": "Day 1 — Arrival"},
        {"src_name": "Bad Tour", "provider": "Horizon Voyages", "country": "Vietnam",
         "tour_id_external": "tour-002", "src_itineraries": "Day 1 — Arrival"},
    ]
    insert_batch_result = (
        ["tour-id-1"],
        [{"identifier": "tour-002", "reason": "value too long for type character varying(500)"}],
    )
    conn_main, result, exc = await _run_process_file(
        records, tour_repo_insert_batch_result=insert_batch_result)

    assert exc is None
    assert result["rows_dropped"] == 1

    details_calls = _pipeline_runs_calls(conn_main, "ingest_details")
    written = json.loads(details_calls[0].args[2])
    assert written["rows_parsed"] == 2
    assert written["rows_landed"] == 1
    assert written["rows_dropped"] == 1
    assert written["drops"] == [{
        "reason": "value too long for type character varying(500)",
        "count": 1,
        "sample_ids": ["tour-002"],
    }]

    # tours_failed must never be touched by the ingest step (already-live S1-quality-failure
    # semantics downstream — see migration 091).
    assert _pipeline_runs_calls(conn_main, "tours_failed") == []


# ── RawTourRepository.insert_batch: one bad record no longer aborts the rest (P1.3 root cause) ──

@pytest.mark.asyncio
async def test_insert_batch_isolates_failures_does_not_abort_remaining_records():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    repo = RawTourRepository(conn, tenant_id="00000000-0000-0000-0000-000000000001")

    async def fake_insert(data):
        if data["src_name"] == "Bad Tour":
            raise RuntimeError("value too long for type character varying(500)")
        return f"tour-id-for-{data['src_name']}"

    records = [
        {"src_name": "Tour A", "tour_id_external": "ext-a"},
        {"src_name": "Bad Tour", "tour_id_external": "ext-bad"},
        {"src_name": "Tour C", "tour_id_external": "ext-c"},
    ]

    with patch.object(repo, "insert", AsyncMock(side_effect=fake_insert)):
        ids, failures = await repo.insert_batch(records)

    # Tour A and Tour C both landed even though Tour B (in the middle) failed — previously a
    # single failing insert raised out of insert_batch and silently dropped every record after it.
    assert ids == ["tour-id-for-Tour A", "tour-id-for-Tour C"]
    assert len(failures) == 1
    assert failures[0]["identifier"] == "ext-bad"
    assert "value too long" in failures[0]["reason"]

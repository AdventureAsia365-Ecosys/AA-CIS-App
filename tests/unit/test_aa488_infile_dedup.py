"""
Unit tests for AA-488 Gap 1: in-file dedup in process_file().

Two rows in the SAME uploaded file with an identical (src_name, provider) key, neither already
present in raw_tours, used to both land in new_records and both get inserted — process_file()'s
per-row DB-existence query runs before either row is written, so the second row's query never
sees the first row as already-existing (it isn't, yet). Fix: seen_keys tracks (src_name,
provider) already assigned to new_records THIS run; a repeat key is dropped (first occurrence is
kept and inserted normally) with reason='duplicate_in_file' recorded in ingest_details.drops via
the existing _summarize_drops() mechanism — no schema/enum change, no upload_staging row.
"""
import os
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.ingestion import handler


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


async def _run_process_file(records, tour_repo_insert_batch_result):
    """Mirrors test_aa343_ingest_status_fix.py's harness. conn_main.fetchrow always returns
    None -> no row in `records` matches an EXISTING raw_tours row, isolating the new in-file
    dedup path from the pre-existing DB-dedup (upload_staging) path."""
    conn_check = _make_conn(fetchrow_return=None)   # no file-hash dedup match
    conn_main = _make_conn(fetchrow_return=None)     # no existing-tour match for any row

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
        mock_source_repo.insert = AsyncMock(return_value="source-id-1")
        mock_source_repo.update_status = AsyncMock(return_value=None)
        mock_source_repo_cls.return_value = mock_source_repo

        mock_tour_repo = MagicMock()
        mock_tour_repo.insert_batch = AsyncMock(return_value=tour_repo_insert_batch_result)
        mock_tour_repo_cls.return_value = mock_tour_repo

        result = await handler.process_file("aa-cis-bronze", "raw-inbox/Horizon/file.xlsx")
        return conn_main, mock_tour_repo, result


# ── Gap 1 fix: identical (src_name, provider) rows within one file ───────────────────────────

@pytest.mark.asyncio
async def test_two_identical_rows_in_file_only_first_inserted_second_dropped():
    records = [
        {"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
        {"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
    ]
    conn_main, mock_tour_repo, result = await _run_process_file(
        records, tour_repo_insert_batch_result=(["tour-id-1"], []))

    # Only the FIRST occurrence reached insert_batch — new_records had exactly 1 row, not 2.
    inserted_records = mock_tour_repo.insert_batch.call_args.args[0]
    assert len(inserted_records) == 1
    assert inserted_records[0]["src_name"] == "Ha Long Bay Cruise"

    assert result["status"] == "done"
    assert result["rows_dropped"] == 1

    details_calls = _pipeline_runs_calls(conn_main, "ingest_details")
    written = json.loads(details_calls[0].args[2])
    assert written["rows_parsed"] == 2
    assert written["rows_landed"] == 1
    assert written["rows_dropped"] == 1
    assert written["drops"] == [{
        "reason": "duplicate_in_file",
        "count": 1,
        "sample_ids": ["Ha Long Bay Cruise"],
    }]

    # The dropped row must NOT be routed to upload_staging (that path is reserved for matches
    # against an EXISTING raw_tours row, which this is not).
    staging_inserts = [
        call for call in conn_main.execute.call_args_list
        if "upload_staging" in str(call.args[0])
    ]
    assert staging_inserts == []


@pytest.mark.asyncio
async def test_case_and_whitespace_variant_still_deduped_in_file():
    """normalize_group_key() lower()s + strip()s both sides — a case/whitespace variant of the
    same tour within one file must still be caught as an in-file duplicate."""
    records = [
        {"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
        {"src_name": "  ha long bay cruise  ", "provider": "HORIZON VOYAGES", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
    ]
    conn_main, mock_tour_repo, result = await _run_process_file(
        records, tour_repo_insert_batch_result=(["tour-id-1"], []))

    inserted_records = mock_tour_repo.insert_batch.call_args.args[0]
    assert len(inserted_records) == 1
    assert result["rows_dropped"] == 1


@pytest.mark.asyncio
async def test_distinct_rows_in_file_both_still_insert_unaffected():
    """Regression guard: distinct (src_name, provider) rows in the same file are unaffected by
    the new in-file dedup — both still reach new_records/insert_batch as before."""
    records = [
        {"src_name": "Ha Long Bay Cruise", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
        {"src_name": "Sapa Trekking", "provider": "Horizon Voyages", "country": "Vietnam", "src_itineraries": "Day 1 — Arrival"},
    ]
    conn_main, mock_tour_repo, result = await _run_process_file(
        records, tour_repo_insert_batch_result=(["tour-id-1", "tour-id-2"], []))

    inserted_records = mock_tour_repo.insert_batch.call_args.args[0]
    assert len(inserted_records) == 2
    assert result["rows_dropped"] == 0

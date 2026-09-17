"""AA-490 (follow-up AA-488 Gap 1) — the dry_run preview branch of GET /admin/ingest-s3
(admin_pipeline.py::ingest_s3()) now drops in-file duplicates (two rows in the SAME uploaded
file sharing normalize_group_key(src_name, provider), neither yet in the DB) the same way the
real Commit path (services/ingestion/handler.py::process_file()) already did since AA-488 PR-1
-- previously the preview showed both rows as "ready" and the drop was only discovered after
clicking Commit.

Covers:
  1. test_dry_run_flags_second_in_file_duplicate_as_blocked — 2 rows, identical
     src_name+provider, neither in DB -> row 1 ready, row 2 blocked reason=duplicate_in_file
  2. test_dry_run_in_file_dedup_is_case_and_whitespace_insensitive — matches
     normalize_group_key's own normalization (lower + strip)
  3. test_dry_run_different_provider_not_flagged_as_duplicate — same src_name, different
     provider -> both ready (not a dedup key match)
  4. test_dry_run_db_duplicate_still_takes_correct_branch — a row matching an existing DB
     tour is still reason=duplicate_tour, not swallowed by the in-file check
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline


def _make_request_and_pool(existing_hash_row=None, duplicate_names_rows=None, sources_rows=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing_hash_row)
    conn.fetch = AsyncMock(side_effect=[
        duplicate_names_rows or [],  # dup_rows query
        sources_rows or [],          # upload_history sources query
    ])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    request = MagicMock()
    request.app.state.pool = pool
    return request, conn


def _req(**kw):
    defaults = dict(s3_key="test.xlsx", dry_run=True, max_tours=500)
    defaults.update(kw)
    return admin_pipeline.IngestS3Request(**defaults)


@pytest.mark.asyncio
class TestAA490DryRunInFileDedup:
    async def _run(self, monkeypatch, records, existing_hash_row=None, duplicate_names_rows=None):
        monkeypatch.setattr(admin_pipeline, "verify_admin_secret", lambda *a, **k: None)
        request, conn = _make_request_and_pool(
            existing_hash_row=existing_hash_row, duplicate_names_rows=duplicate_names_rows,
        )

        mock_s3 = MagicMock()
        mock_parser_instance = MagicMock()
        mock_parser_instance.parse.return_value = records

        with patch.object(admin_pipeline, "_boto3") as mock_boto3, \
             patch("services.ingestion.excel_parser.ExcelParser", return_value=mock_parser_instance), \
             patch("tempfile.NamedTemporaryFile") as mock_tmp:
            mock_boto3.client.return_value = mock_s3
            mock_tmp.return_value.__enter__.return_value.name = "/tmp/aa490-test-fake.xlsx"
            with patch("builtins.open", MagicMock(return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=b"fake"))),
                __exit__=MagicMock(return_value=False),
            ))):
                result = await admin_pipeline.ingest_s3(_req(), request, x_admin_secret="x")
        return result

    async def test_dry_run_flags_second_in_file_duplicate_as_blocked(self, monkeypatch):
        records = [
            {"src_name": "Halong Bay Cruise", "provider": "Horizon Voyages",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
            {"src_name": "Halong Bay Cruise", "provider": "Horizon Voyages",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
        ]
        result = await self._run(monkeypatch, records)

        assert result["ready_count"] == 1
        assert result["blocked_count"] == 1
        assert result["blocked_tours"][0]["reason"] == "duplicate_in_file"
        assert "same name" in result["blocked_tours"][0]["message"].lower()

    async def test_dry_run_in_file_dedup_is_case_and_whitespace_insensitive(self, monkeypatch):
        records = [
            {"src_name": " Halong Bay Cruise ", "provider": "Horizon Voyages",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
            {"src_name": "halong bay cruise", "provider": "HORIZON VOYAGES",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
        ]
        result = await self._run(monkeypatch, records)

        assert result["ready_count"] == 1
        assert result["blocked_tours"][0]["reason"] == "duplicate_in_file"

    async def test_dry_run_different_provider_not_flagged_as_duplicate(self, monkeypatch):
        records = [
            {"src_name": "Halong Bay Cruise", "provider": "Horizon Voyages",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
            {"src_name": "Halong Bay Cruise", "provider": "Indochina Junks",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
        ]
        result = await self._run(monkeypatch, records)

        assert result["ready_count"] == 2
        assert result["blocked_count"] == 0

    async def test_dry_run_db_duplicate_still_takes_correct_branch(self, monkeypatch):
        records = [
            {"src_name": "Halong Bay Cruise", "provider": "Horizon Voyages",
             "country": "Vietnam", "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrival"},
        ]
        dup_rows = [{"n": "halong bay cruise"}]
        result = await self._run(monkeypatch, records, duplicate_names_rows=dup_rows)

        assert result["ready_count"] == 0
        assert result["blocked_count"] == 1
        assert result["blocked_tours"][0]["reason"] == "duplicate_tour"

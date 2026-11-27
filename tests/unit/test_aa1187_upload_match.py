"""AA-1187 — the Upload preview and Commit share one match (services/ingestion/tour_match.py). Unit side: the key
normalization, the preview lines, and Commit staging a Provider change for Duplicate Review instead of inserting a
new tour. The SQL rule itself runs on Postgres in tests/integration/test_aa1187_upload_match_pg.py. Invented names."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ingestion import handler, tour_match


@pytest.fixture(autouse=True)
def _no_jev_or_near_duplicates():
    from services.ingestion import jev_gates, near_duplicate

    async def none(rows, *a, **k):
        return [jev_gates.RowVerdict() for _ in rows]

    async def no_dup(self, row):
        return near_duplicate.NearDup()
    with patch.object(jev_gates, "assess_rows_standalone", new=none), \
            patch.object(near_duplicate.Checker, "check", new=no_dup):
        yield


def test_match_key_normalizes_and_treats_an_empty_country_as_unknown():
    assert tour_match.match_key({"src_name": " Zorvane Ridge Walk ", "provider": " QUILLON ", "country": "Laos"}) \
        == ("zorvane ridge walk", "quillon", "laos")
    assert tour_match.match_key({"src_name": "A", "provider": None, "country": "  "}) == ("a", "", None)


@pytest.mark.asyncio
async def test_match_existing_skips_a_row_without_a_name_and_passes_the_key():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    assert await tour_match.match_existing(conn, "t-1", {"src_name": "  "}) is None
    conn.fetchrow.assert_not_called()
    await tour_match.match_existing(conn, "t-1", {"src_name": "Zorvane", "provider": "Q", "country": "Laos"})
    sql, *args = conn.fetchrow.call_args.args
    assert sql == tour_match.MATCH_SQL and args == ["t-1", "zorvane", "q", "laos"]


def test_preview_messages():
    same = {"same_provider": True, "live": True, "key_deleted_at": None, "provider": "Quillon Treks"}
    assert tour_match.preview_message(same, {}) == "This tour already exists in the system."
    other = {**same, "same_provider": False}
    line = tour_match.preview_message(other, {"provider": None})
    assert "another provider (Quillon Treks; this row: none)" in line and "Create a new tour" in line
    gone = {**same, "live": False, "key_deleted_at": datetime(2026, 10, 20, tzinfo=timezone.utc)}
    assert tour_match.preview_message(gone, {}).startswith("This tour was deleted on 2026-10-20")


def _conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="staging-id-1")
    conn.execute = AsyncMock(return_value=None)
    conn.close = AsyncMock(return_value=None)
    return conn


@pytest.mark.asyncio
async def test_commit_stages_a_provider_change_for_duplicate_review_and_inserts_nothing():
    """The S295 bug: Commit matched Name + Provider only, so another Provider made a second tour."""
    records = [{"src_name": "Zorvane Ridge Walk", "provider": "Quillon Trek Co", "country": "Laos",
                "duration": "3 days", "price_raw": "500", "src_itineraries": "Day 1 — Arrive"}]
    existing = {"tour_id": "aa118700-0000-4000-8000-000000000001", "source_group_id": None,
                "provider": "Quillon Treks", "same_provider": False, "live": True, "key_deleted_at": None}
    conn_check, conn_main = _conn(), _conn()
    s3 = MagicMock()
    s3.head_object = MagicMock(return_value={"ContentLength": 1024})
    parser = MagicMock()
    parser.parse.return_value = records
    with patch.object(handler, "_s3", return_value=s3), \
            patch.object(handler, "get_database_url", return_value="postgresql://fake"), \
            patch.object(handler.asyncpg, "connect", AsyncMock(side_effect=[conn_check, conn_main])), \
            patch.object(handler, "ExcelParser", return_value=parser), \
            patch.object(handler, "match_existing", AsyncMock(return_value=existing)) as matcher, \
            patch.object(handler, "RawSourceRepository") as source_cls, \
            patch.object(handler, "RawTourRepository") as tour_cls, \
            patch("services.tour_lifecycle.insert_new_sources", AsyncMock(return_value=([], []))) as insert:
        source_cls.return_value.insert = AsyncMock(return_value="source-id-1")
        source_cls.return_value.update_status = AsyncMock(return_value=None)
        tour_cls.return_value.insert_batch = AsyncMock(return_value=[])
        result = await handler.process_file("aa-cis-bronze", "raw-inbox/Invented/file.xlsx")
    assert result["tours_staged"] == 1 and result["tours_written"] == 0
    assert matcher.await_args.args[2]["provider"] == "Quillon Trek Co"
    assert insert.await_args.args[1] == []  # no new tour
    staged = [c for c in conn_main.fetchval.await_args_list if "upload_staging" in c.args[0]]
    assert len(staged) == 1 and staged[0].args[4] == existing["tour_id"]

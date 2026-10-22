"""AA-482 — Landing page engine (ADR-2026-030 D5: AA hosts the white-label tour page).

Covers:
  1. test_get_trip_page_returns_real_fields_and_marks_alive — a real, active, non-deleted
     tour returns the expected field shape and stamps url_alive=true on its DB row
  2. test_get_trip_page_404_for_missing_tour_and_marks_dead — a tour that doesn't qualify
     (not found/inactive/deleted) 404s AND flips url_alive=false, not silently
  3. test_publish_trip_page_computes_real_url_not_placeholder — the publish endpoint writes
     a real, this-app's-own-domain URL, never a placeholder like the pre-existing 87 rows
  4. test_publish_trip_page_422_when_tour_not_eligible — publishing a non-qualifying tour
     is rejected, not silently written with fabricated data
  5. test_recheck_flips_dead_rows_and_counts_correctly — batch recheck correctly separates
     still-alive from now-dead rows
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_trip_page


def _make_pool(fetch_page_row=None, fetch_rows=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetch_page_row)
    conn.fetch = AsyncMock(return_value=fetch_rows or [])
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    request = MagicMock()
    request.app.state.pool = pool
    return request, conn


_TOUR_ROW = {
    "tour_id": "770a38dd-4e7b-4b55-9ded-0e9ced757301",
    "country": "Bhutan", "duration": "12 days",
    "aa_name": "Bhutan: West to Central", "aa_subtitle": "Dzongs & Valleys",
    "aa_summary": "A private journey through Bhutan.",
    "aa_itineraries": "Day 1 — Arrival...",
    # AA-482 live-verify (04/09/2026): real asyncpg rows return jsonb columns as a raw JSON
    # STRING in this pool's config, not an auto-decoded Python list -- confirmed against the
    # real deployed endpoint (a real tour's highlights came back double-JSON-encoded before
    # the _jsonb() fix). Mocking it as a real list here would NOT have caught that bug -- mock
    # the real shape.
    "aa_highlights": '["Taktshang Monastery", "Dochu La Pass", "Punakha Dzong"]',
    "seo_title": "Bhutan Journey", "seo_meta": "A private 12-day journey through Bhutan.",
}


@pytest.mark.asyncio
class TestAA482TripPage:
    async def test_get_trip_page_returns_real_fields_and_marks_alive(self, monkeypatch):
        request, conn = _make_pool(fetch_page_row=_TOUR_ROW)

        result = await v1_trip_page.get_trip_page(_TOUR_ROW["tour_id"], request)

        assert result["name"] == "Bhutan: West to Central"
        # real array, decoded from the raw jsonb string -- not a string, not still-encoded
        assert result["highlights"] == ["Taktshang Monastery", "Dochu La Pass", "Punakha Dzong"]
        assert isinstance(result["highlights"], list)
        assert result["seo_meta"] == _TOUR_ROW["seo_meta"]
        assert result["summary"] == _TOUR_ROW["aa_summary"]
        # url_alive=true update was issued
        update_call = conn.execute.await_args
        assert "url_alive = true" in update_call.args[0]

    async def test_get_trip_page_404_for_missing_tour_and_marks_dead(self):
        from fastapi import HTTPException
        request, conn = _make_pool(fetch_page_row=None)

        with pytest.raises(HTTPException) as exc_info:
            await v1_trip_page.get_trip_page("00000000-0000-0000-0000-000000000099", request)

        assert exc_info.value.status_code == 404
        update_call = conn.execute.await_args
        assert "url_alive = false" in update_call.args[0]

    async def test_get_trip_page_404_for_a_non_uuid_id_without_any_query(self):
        """Sentry AA-CIS-API-E: GET /v1/trip/abc was a 500 (asyncpg DataError)."""
        from fastapi import HTTPException
        request, conn = _make_pool(fetch_page_row=None)

        with pytest.raises(HTTPException) as exc_info:
            await v1_trip_page.get_trip_page("abc", request)

        assert exc_info.value.status_code == 404
        conn.fetchrow.assert_not_awaited()
        conn.execute.assert_not_awaited()

    async def test_publish_trip_page_computes_real_url_not_placeholder(self, monkeypatch):
        monkeypatch.setattr(v1_trip_page, "verify_admin_secret", lambda *a, **k: None)
        request, conn = _make_pool(fetch_page_row=_TOUR_ROW)

        result = await v1_trip_page.publish_trip_page(
            _TOUR_ROW["tour_id"], request, x_admin_secret="x",
        )

        assert result["status"] == "published"
        assert result["url"] == f"https://aa-cis.lumiguides.it.com/trip/{_TOUR_ROW['tour_id']}"
        assert result["url"] != "https://aa-cis.lumiguides.it.com/"  # not the old placeholder

    async def test_publish_trip_page_422_when_tour_not_eligible(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(v1_trip_page, "verify_admin_secret", lambda *a, **k: None)
        request, conn = _make_pool(fetch_page_row=None)

        with pytest.raises(HTTPException) as exc_info:
            await v1_trip_page.publish_trip_page(
                "00000000-0000-0000-0000-000000000099", request, x_admin_secret="x",
            )

        assert exc_info.value.status_code == 422
        conn.execute.assert_not_called()  # never wrote a fabricated row

    async def test_recheck_flips_dead_rows_and_counts_correctly(self, monkeypatch):
        monkeypatch.setattr(v1_trip_page, "verify_admin_secret", lambda *a, **k: None)
        existing_rows = [
            {"tenant_id": "00000000-0000-0000-0000-000000000001", "tour_id": "a"},
            {"tenant_id": "00000000-0000-0000-0000-000000000001", "tour_id": "b"},
        ]
        request, conn = _make_pool(fetch_rows=existing_rows)
        # first tour_id ("a") still qualifies, second ("b") no longer does
        conn.fetchrow = AsyncMock(side_effect=[_TOUR_ROW, None])

        result = await v1_trip_page.recheck_trip_pages(request, x_admin_secret="x")

        assert result["checked"] == 2
        assert result["flipped_dead"] == 1

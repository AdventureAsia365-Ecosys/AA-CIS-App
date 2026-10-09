"""AA-747 item 3 — S1 does not wait for the whole SEO prefetch.

Covers: prefetch commits each batch of 5 in the ORDER GIVEN; the rewrite SEO step waits for its
own tour's prefetched row then proceeds; falls back to a per-tour fetch after the cap; never buys
twice for the same tour. No live Bedrock / DataForSEO / DB — all mocked.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.seo_intelligence import s1_prefetch as P
from services.seo_intelligence.dataforseo_client import DataForSEOClient


def _row(tid, name, country, acts=None):
    return {"tour_id": tid, "src_name": name, "country": country, "activities": acts}


class _Conn:
    """Minimal asyncpg-connection stub: records the ORDER seo_context rows are written."""
    def __init__(self, fresh=(), demand=()):
        self.fresh, self.demand, self.inserted = set(fresh), list(demand), []

    async def fetch(self, sql, *args):
        if "seo_context" in sql:
            return [{"tour_id": t} for t in args[0] if t in self.fresh]
        if "search_demand" in sql:
            return self.demand
        return []

    async def fetchrow(self, sql, *args):
        # _persist writes via SeoContextRepository(conn).insert → conn.fetchrow (RETURNING id)
        self.inserted.append(args)
        return {"id": "x"}


# ── batch commit order ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prefetch_commits_in_order_given_in_batches_of_five():
    """Seven tours across two countries, given in a specific order → prefetch persists their
    seo_context rows IN THAT ORDER (batches of 5 do not reorder within the given order)."""
    uuids = [f"00000000-0000-0000-0000-00000000000{c}" for c in "1234567"]
    names = ["Everest Trek", "Annapurna Circuit", "Langtang Valley", "Mustang Trail",
             "Manaslu Circuit", "Paro Taktsang", "Bumthang Owl"]
    countries = ["Nepal", "Nepal", "Nepal", "Nepal", "Nepal", "Bhutan", "Bhutan"]
    rows = [_row(u, n, c) for u, n, c in zip(uuids, names, countries)]

    conn = _Conn()
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[])
    client._serp_advanced = AsyncMock(return_value={})

    await P.prefetch(conn, rows, tenant_id="t", location_code=2840, language_code="en", jev=False,
                     client=client)

    # the first element of each insert args tuple is tour_id (see _persist payload order)
    written_order = [a[0] for a in conn.inserted]
    assert written_order == uuids   # exact given order preserved across the chunk boundaries


# ── rewrite waits for its own row then proceeds ──────────────────────────────

@pytest.mark.asyncio
async def test_wait_for_prefetched_seo_returns_row_when_it_appears(monkeypatch):
    """load_fresh is empty for the first 2 polls, then the prefetch commits the row → the waiter
    returns it (the rewrite then reuses it, never buying)."""
    calls = {"n": 0}
    fresh_row = {"keywords": {"top_keywords": ["everest base camp"]}, "keyword_ideas": [],
                 "people_also_ask": [], "related_keywords": [], "destination": "everest"}

    async def fake_load_fresh(conn, tour_id):
        calls["n"] += 1
        return fresh_row if calls["n"] >= 3 else None

    async def fake_covers(conn, tour_id):
        return True

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    conn = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)), \
         patch.object(P, "load_fresh", fake_load_fresh), \
         patch.object(P, "prefetch_job_covers_tour", fake_covers), \
         patch("asyncio.sleep", fake_sleep):
        out = await P.wait_for_prefetched_seo("tid", poll_seconds=10, cap_seconds=300)

    assert out == fresh_row
    assert calls["n"] == 3          # polled until the row appeared
    assert sleeps == [10, 10]       # slept between the first two empty polls only


@pytest.mark.asyncio
async def test_wait_falls_back_after_cap(monkeypatch):
    """The row never appears but a prefetch job keeps covering the tour → the waiter returns None
    once the cap is reached, so the caller falls back to its own per-tour fetch."""
    async def never_fresh(conn, tour_id):
        return None

    async def always_covers(conn, tour_id):
        return True

    # monotonic jumps past the cap on the 2nd read so the loop exits via the deadline branch
    times = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])

    async def fake_sleep(s):
        pass

    conn = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)), \
         patch.object(P, "load_fresh", never_fresh), \
         patch.object(P, "prefetch_job_covers_tour", always_covers), \
         patch("asyncio.sleep", fake_sleep), \
         patch("time.monotonic", lambda: next(times)):
        out = await P.wait_for_prefetched_seo("tid", poll_seconds=10, cap_seconds=300)

    assert out is None   # fall back to per-tour fetch


@pytest.mark.asyncio
async def test_wait_stops_early_when_no_prefetch_covers_tour():
    """No in-flight prefetch job for this tour and no row → return None immediately (don't wait)."""
    async def never_fresh(conn, tour_id):
        return None

    async def not_covered(conn, tour_id):
        return False

    conn = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)), \
         patch.object(P, "load_fresh", never_fresh), \
         patch.object(P, "prefetch_job_covers_tour", not_covered):
        out = await P.wait_for_prefetched_seo("tid")

    assert out is None


# ── no double buy: once the prefetched row exists, the rewrite reuses it ──────

@pytest.mark.asyncio
async def test_process_seo_reuses_prefetched_row_never_buys():
    """When wait_for_prefetched_seo returns a row, process_seo returns status='reused' and never
    constructs a DataForSEOClient (no second buy for a tour the prefetch already covered)."""
    from services.seo_intelligence import handler as H

    fresh_row = {"keywords": {"top_keywords": ["everest"]}, "keyword_ideas": [],
                 "people_also_ask": [], "related_keywords": [], "destination": "everest"}

    reuse_conn = AsyncMock()
    reuse_conn.close = AsyncMock()

    async def fake_load_fresh(conn, tour_id):
        return None  # no row yet at first check

    async def fake_covers(conn, tour_id):
        return True  # a prefetch job covers it

    async def fake_wait(tour_id, **kw):
        return fresh_row  # the row lands while waiting

    with patch("asyncpg.connect", AsyncMock(return_value=reuse_conn)), \
         patch("services.seo_intelligence.s1_prefetch.load_fresh", fake_load_fresh), \
         patch("services.seo_intelligence.s1_prefetch.prefetch_job_covers_tour", fake_covers), \
         patch("services.seo_intelligence.s1_prefetch.wait_for_prefetched_seo", fake_wait), \
         patch.object(H, "DataForSEOClient",
                      side_effect=AssertionError("must not buy — the prefetched row is reused")):
        result = await H.process_seo(
            tour_id="00000000-0000-0000-0000-00000000000a", destination="everest", seed="everest",
            tenant_id=None, seo_mode="dataforseo", extra_seeds=[], place_terms=["everest"],
            activity_words=["trekking"], country="Nepal",
        )

    assert result["status"] == "reused"
    assert result["data"] == fresh_row


@pytest.mark.asyncio
async def test_prefetch_skips_a_tour_bought_by_a_rewrite_meanwhile():
    """A rewrite that hit its wait cap buys tour #6 while the prefetch is on batch 1 — batch 2
    re-checks freshness and does not buy or write tour #6 again (no double buy)."""
    uuids = [f"00000000-0000-0000-0000-00000000000{c}" for c in "1234567"]
    rows = [_row(u, f"Trek {i}", "Nepal") for i, u in enumerate(uuids)]

    class _Racing(_Conn):
        async def fetch(self, sql, *args):
            if "seo_context" in sql and len(self.inserted) >= 5:
                self.fresh.add(uuids[5])  # bought by the rewrite after batch 1 was committed
            return await super().fetch(sql, *args)

    conn = _Racing()
    client = DataForSEOClient(login="x", password="y")
    client.fetch_keyword_ideas_multi = AsyncMock(return_value=[])
    client._serp_advanced = AsyncMock(return_value={})

    summary = await P.prefetch(conn, rows, tenant_id="t", location_code=2840, language_code="en",
                               jev=False, client=client)

    written = [a[0] for a in conn.inserted]
    assert uuids[5] not in written
    assert written == uuids[:5] + [uuids[6]]
    assert summary["reused"] == 1

"""AA-448 — tenant_pool.py: T7's tenant-scoped trip+atom fetch (replaces fetch_trips()/
fetch_atoms_by_trip() for T7's own code path only — those two functions are UNTOUCHED and still
used by admin_atoms.py's preview-slotgrid + admin_produce.py's /run trigger, confirmed by grep
before this task started, see docs/implementation-notes/AA-448-t7-content-planning.md Decision
1).

DB-backed — tested with a mocked asyncpg pool, same pool.acquire() convention as
test_aa301_quarter.py/test_aa299_atom_insert.py.
"""
import datetime
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_planning.tenant_pool import (fetch_tenant_atoms_by_trip, fetch_tenant_trips,
                                                fetch_used_atom_ids)

TENANT = uuid.uuid4()


def _mock_pool(rows):
    conn = AsyncMock()
    conn.fetch.return_value = rows
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


class TestFetchTenantTrips:
    @pytest.mark.asyncio
    async def test_query_scoped_by_tenant_id_param(self):
        """Confirms the query is called with tenant_id (not the platform-wide, unfiltered
        fetch_trips() shape) — the exact behavior this module exists to guarantee."""
        trip_id = uuid.uuid4()
        pool, conn = _mock_pool([{
            "id": trip_id, "name": "Sapa Trek", "destination": "Vietnam",
            "period": "Mar-May", "duration_raw": "4 days", "itinerary_source": "day 1...",
            "lifecycle_stage": "active", "trip_url": None, "url_alive": None,
        }])

        trips = await fetch_tenant_trips(TENANT, pool)

        conn.fetch.assert_awaited_once()
        call_args = conn.fetch.await_args.args
        assert call_args[1] == TENANT  # query, tenant_id
        assert len(trips) == 1
        assert trips[0].id == trip_id
        assert trips[0].name == "Sapa Trek"

    @pytest.mark.asyncio
    async def test_no_tours_for_tenant_returns_empty_list(self):
        pool, _ = _mock_pool([])
        trips = await fetch_tenant_trips(TENANT, pool)
        assert trips == []

    @pytest.mark.asyncio
    async def test_null_lifecycle_stage_defaults_active_same_as_row_to_trip(self):
        """Reuses runway.py's own _row_to_trip() unchanged — confirms that reuse actually
        applies its `or "active"` fallback, not a divergent copy."""
        trip_id = uuid.uuid4()
        pool, _ = _mock_pool([{
            "id": trip_id, "name": "T", "destination": "D", "period": None,
            "duration_raw": None, "itinerary_source": None, "lifecycle_stage": None,
            "trip_url": None, "url_alive": None,
        }])
        trips = await fetch_tenant_trips(TENANT, pool)
        assert trips[0].lifecycle_stage == "active"


class TestFetchTenantAtomsByTrip:
    @pytest.mark.asyncio
    async def test_query_scoped_by_owner_scope_str_tenant_id(self):
        """The one thing this module exists to fix vs. the old fetch_atoms_by_trip(): the query
        param passed must be the OWNER_SCOPE value (str(tenant_id)), not raw_tours.tenant_id."""
        trip_id = uuid.uuid4()
        pool, conn = _mock_pool([{
            "atom_id": "atom_1", "tour_id": trip_id, "text": "text", "activity_type": "trek",
            "distinctiveness": "HIGH", "deleted": False, "weight": 1.0,
            "cooldown_until": "{}", "usage_log": "[]",
        }])

        by_trip = await fetch_tenant_atoms_by_trip(TENANT, pool)

        conn.fetch.assert_awaited_once()
        call_args = conn.fetch.await_args.args
        assert call_args[1] == str(TENANT)  # query, owner_scope (text column -> str)
        assert trip_id in by_trip
        assert by_trip[trip_id][0].distinctiveness == "HIGH"

    @pytest.mark.asyncio
    async def test_jsonb_string_shape_parsed_via_reused_row_to_atom(self):
        """Reuses quarter.py's own _row_to_atom()/_parse_jsonb() unchanged (asyncpg-has-no-
        jsonb-codec workaround) — confirms cooldown_until/usage_log arriving as raw JSON
        strings still parse correctly through this new query path."""
        trip_id = uuid.uuid4()
        pool, _ = _mock_pool([{
            "atom_id": "atom_1", "tour_id": trip_id, "text": "text", "activity_type": None,
            "distinctiveness": "MED", "deleted": False, "weight": 1.5,
            "cooldown_until": '{"blog": "2026-09-01"}', "usage_log": "[]",
        }])
        by_trip = await fetch_tenant_atoms_by_trip(TENANT, pool)
        atom = by_trip[trip_id][0]
        assert atom.cooldown_until == {"blog": "2026-09-01"}
        assert atom.usage_log == []

    @pytest.mark.asyncio
    async def test_no_atoms_for_tenant_returns_empty_dict(self):
        pool, _ = _mock_pool([])
        by_trip = await fetch_tenant_atoms_by_trip(TENANT, pool)
        assert by_trip == {}


class TestFetchUsedAtomIds:
    """AA-494 Decision 6 — the atom-availability rule ("An atom is 'used' (locked) for calendar
    month X if there exists at least one content_piece ... with status at or beyond approved ...
    whose created_at falls within calendar month X"). content_piece.status's real value set
    (migration 118) is processing/approved/held/failed — no state exists "beyond" approved, so
    the rule collapses to exactly status='approved'; these tests pin the generated SQL/params
    that encode that, matching this module's own existing convention (test_aa448_tenant_pool.py
    above: DB-backed functions are tested against a mocked pool, pinning SQL text/params, not a
    live-DB filtering integration test — see also test_aa249_seo_context_tour_unique.py)."""

    @pytest.mark.asyncio
    async def test_query_scoped_by_tenant_and_month_bounds(self):
        pool, conn = _mock_pool([])
        await fetch_used_atom_ids(TENANT, 2026, 9, pool)

        conn.fetch.assert_awaited_once()
        call_args = conn.fetch.await_args.args
        assert call_args[1] == TENANT
        assert call_args[2] == datetime.date(2026, 9, 1)
        assert call_args[3] == datetime.date(2026, 10, 1)

    @pytest.mark.asyncio
    async def test_december_month_bounds_roll_over_to_next_year(self):
        """A piece written in a different month must not lock the current month — this is
        enforced by the created_at range itself, so the December year-rollover edge (a common
        off-by-one spot) is worth its own check."""
        pool, conn = _mock_pool([])
        await fetch_used_atom_ids(TENANT, 2026, 12, pool)

        call_args = conn.fetch.await_args.args
        assert call_args[2] == datetime.date(2026, 12, 1)
        assert call_args[3] == datetime.date(2027, 1, 1)

    @pytest.mark.asyncio
    async def test_query_filters_status_approved_only(self):
        """held/failed/processing pieces must not lock an atom — encoded as a hard filter in the
        query itself (cp.status = 'approved'), not app-level post-filtering."""
        pool, conn = _mock_pool([])
        await fetch_used_atom_ids(TENANT, 2026, 9, pool)

        sql = conn.fetch.await_args.args[0]
        assert "cp.status = 'approved'" in sql

    @pytest.mark.asyncio
    async def test_query_filters_by_content_piece_created_at_not_slot_month(self):
        """The rule explicitly uses the piece's real write date (content_piece.created_at), never
        a slot's pre-assigned plan month — pinning the column name guards against a future
        change accidentally joining against acp_v2_slots.month instead."""
        pool, conn = _mock_pool([])
        await fetch_used_atom_ids(TENANT, 2026, 9, pool)

        sql = conn.fetch.await_args.args[0]
        assert "cp.created_at >= $2 AND cp.created_at < $3" in sql
        assert "acp_v2_slots" not in sql

    @pytest.mark.asyncio
    async def test_returns_atom_id_keyed_dict_with_used_at_and_channel(self):
        used_at = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)
        pool, _ = _mock_pool([{
            "atom_id": "atom_1", "used_at": used_at, "channel": "blog", "request_id": "req-1",
        }])

        used = await fetch_used_atom_ids(TENANT, 2026, 9, pool)

        assert set(used.keys()) == {"atom_1"}
        assert used["atom_1"].used_at == used_at.isoformat()
        assert used["atom_1"].channel == "blog"
        # AA-497 — request_id lets the T7 slot-view reopen the request behind an already-written
        # atom ("Change angle").
        assert used["atom_1"].request_id == "req-1"

    @pytest.mark.asyncio
    async def test_request_id_picks_latest_not_earliest_piece(self):
        """AA-497 — unlike used_at/channel (earliest piece), request_id must reflect the LATEST
        approved piece for this atom (ARRAY_AGG ... ORDER BY cp.created_at DESC), since a reopen
        + re-write can now produce a second, later piece for the same atom under a different (or
        the same) request — pinning the SQL's ORDER BY direction, not just the Python return
        shape, since a mocked single-row result can't distinguish "picked which row" on its own."""
        pool, conn = _mock_pool([{
            "atom_id": "atom_1", "used_at": datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc),
            "channel": "blog", "request_id": "req-latest",
        }])
        await fetch_used_atom_ids(TENANT, 2026, 9, pool)

        sql = conn.fetch.await_args.args[0]
        assert "ARRAY_AGG(cp.angle_gate_request_id::text ORDER BY cp.created_at DESC)" in sql

    @pytest.mark.asyncio
    async def test_no_used_atoms_returns_empty_dict(self):
        pool, _ = _mock_pool([])
        used = await fetch_used_atom_ids(TENANT, 2026, 9, pool)
        assert used == {}

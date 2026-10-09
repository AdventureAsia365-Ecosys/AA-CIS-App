"""AA-749 (option b) — platform atoms are never scored for distinctiveness.

A platform atom (owner_scope='platform') carries a stored distinctiveness that is a
migration default, not a measurement. Only a tenant atom (owner_scope = a tenant id, written
through the T5 score_distinctiveness path) has a real HIGH/MED/LOW. This suite locks in:

  Part 1 (read side, api/routers/admin_atoms.py)
    - GET /admin/atoms surfaces distinctiveness as NULL for a platform atom.
    - the `distinctiveness` filter: NOT_SCORED -> platform rows; HIGH/MED/LOW -> tenant-scored
      rows only.
    - GET /admin/atoms/summary breakdown gains a NOT_SCORED bucket; HIGH/MED/LOW count only
      scored (non-platform) rows.

  Part 3 (quarter scoring, services/acp_planning/quarter.py)
    - unscored (platform) atoms are excluded from the distinctiveness average; a trip with no
      scored atom scores 0 for that term.
    - ranking order on an all-platform fixture (every atom MED today) is unchanged vs. the old
      "MED is a constant" behavior — the term is still constant across trips, so order holds.

Mocks the asyncpg pool — no live DB, no LLM. Mirrors test_aa300_admin_atoms.py's harness.
"""
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock

from api.routers import admin_atoms
from services.acp_planning.models import AtomRecord, RunwayMap, Trip
from services.acp_planning.quarter import compute_quarter_plan

TENANT = "11111111-1111-1111-1111-111111111111"
MASTER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ── harness (same shape as test_aa300_admin_atoms.py) ──────────────────────

def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


async def _call_list(conn, **over):
    kwargs = dict(
        tour_id=None, tour_ids=None, atom_ids=None, distinctiveness=None, unreviewed_only=False,
        thin_only=False, include_deleted=False, owner_scope_class=None, lifecycle_stage=None,
        limit=50, offset=0, owner_scope=None,
    )
    kwargs.update(over)
    pool = _make_pool(conn)
    return await admin_atoms.list_atoms(_make_request(pool), **kwargs)


# ── Part 1a — list surfaces NULL distinctiveness for a platform atom ───────

class TestListNullForPlatform:
    @pytest.mark.asyncio
    async def test_select_casts_platform_distinctiveness_to_null(self):
        """The SELECT must return NULL for a platform atom's distinctiveness, not the stored
        default MED. Asserting on the SQL (the pool is mocked, so there is no real CASE
        evaluation) — the column expression is what makes a platform row read as 'not scored'."""
        conn = AsyncMock()
        conn.fetchval.return_value = 0
        conn.fetch.return_value = []
        await _call_list(conn)
        select_query = conn.fetch.call_args[0][0]
        assert "ta.owner_scope = 'platform' THEN NULL" in select_query
        assert "END AS distinctiveness" in select_query


# ── Part 1b — distinctiveness filter: NOT_SCORED vs HIGH/MED/LOW ──────────

class TestDistinctivenessFilter:
    @pytest.mark.asyncio
    async def test_not_scored_filters_to_platform_rows(self):
        conn = AsyncMock()
        conn.fetchval.return_value = 0
        conn.fetch.return_value = []
        await _call_list(conn, distinctiveness="NOT_SCORED")
        query, *params = conn.fetch.call_args[0]
        assert "ta.owner_scope = 'platform'" in query
        # NOT_SCORED is a scope filter, not a value match — it must NOT bind a 'NOT_SCORED' value.
        assert "NOT_SCORED" not in params

    @pytest.mark.asyncio
    async def test_high_matches_only_tenant_scored_rows(self):
        conn = AsyncMock()
        conn.fetchval.return_value = 0
        conn.fetch.return_value = []
        await _call_list(conn, distinctiveness="HIGH")
        query, *params = conn.fetch.call_args[0]
        assert "ta.distinctiveness =" in query
        assert "ta.owner_scope != 'platform'" in query
        assert "HIGH" in params

    @pytest.mark.asyncio
    async def test_invalid_distinctiveness_value_rejected_by_pattern(self):
        """The Query pattern now allows exactly HIGH|MED|LOW|NOT_SCORED. A stray value would be
        rejected by FastAPI before the handler runs — assert the pattern string itself so the
        contract is pinned without needing a live request."""
        import inspect
        sig = inspect.signature(admin_atoms.list_atoms)
        default = sig.parameters["distinctiveness"].default
        assert default.metadata  # FieldInfo carries the pattern constraint
        # The regex is stored on the FieldInfo; confirm NOT_SCORED is accepted and a junk value is not.
        import re
        pattern = None
        for m in getattr(default, "metadata", []):
            pat = getattr(m, "pattern", None)
            if pat:
                pattern = pat
        assert pattern is not None
        assert re.fullmatch(pattern, "NOT_SCORED")
        assert re.fullmatch(pattern, "HIGH")
        assert re.fullmatch(pattern, "BOGUS") is None


# ── Part 1c — summary breakdown gains a NOT_SCORED bucket ──────────────────

class TestSummaryBreakdown:
    @pytest.mark.asyncio
    async def test_breakdown_has_not_scored_and_scored_only_counts(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [
            # grouped by `bucket`: platform -> NOT_SCORED, else the distinctiveness value.
            [{"bucket": "NOT_SCORED", "c": 500},
             {"bucket": "HIGH", "c": 7},
             {"bucket": "MED", "c": 3},
             {"bucket": "LOW", "c": 20}],
            [],  # by_tour
        ]
        conn.fetchrow.return_value = {"total": 530, "reviewed": 10}
        pool = _make_pool(conn)
        result = await admin_atoms.atoms_summary(_make_request(pool), owner_scope=None)

        assert result["distinctiveness_breakdown"] == {
            "HIGH": 7, "MED": 3, "LOW": 20, "NOT_SCORED": 500}

    @pytest.mark.asyncio
    async def test_breakdown_query_groups_platform_into_not_scored(self):
        conn = AsyncMock()
        conn.fetch.side_effect = [[], []]
        conn.fetchrow.return_value = {"total": 0, "reviewed": 0}
        pool = _make_pool(conn)
        await admin_atoms.atoms_summary(_make_request(pool), owner_scope=None)
        breakdown_sql = conn.fetch.call_args_list[0][0][0]
        assert "owner_scope = 'platform' THEN 'NOT_SCORED'" in breakdown_sql


# ── Part 3 — quarter scoring excludes unscored atoms, order unchanged ──────

def _trip(**over):
    base = dict(id=uuid.uuid4(), name="Trip", destination="Dest", period="Mar-May",
                lifecycle_stage="active")
    base.update(over)
    return Trip(**base)


def _atom(trip_id, distinctiveness="MED", owner_scope="platform"):
    return AtomRecord(atom_id=f"atom_{uuid.uuid4().hex[:8]}", trip_id=trip_id,
                      text="atom text", distinctiveness=distinctiveness, owner_scope=owner_scope)


def _dist_of(plan, trip_id):
    return next(ts.distinctiveness_score for ts in plan.trip_scores if ts.trip_id == trip_id)


class TestQuarterUnscoredExcluded:
    def test_all_platform_atoms_score_zero_distinctiveness(self):
        """Every platform atom is unscored -> the distinctiveness term is 0 for the trip (no
        signal), NOT the old flat 0.5 that a pool of MED-defaulted platform atoms produced."""
        t = _trip()
        runway = RunwayMap(tenant_id=MASTER, year=2026, cells=[])
        plan = compute_quarter_plan(
            MASTER, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway,
            atoms_by_trip={t.id: [_atom(t.id, "MED", "platform") for _ in range(5)]})
        assert _dist_of(plan, t.id) == 0.0

    def test_tenant_scored_atoms_still_averaged(self):
        """A tenant atom (owner_scope != 'platform') keeps its real HIGH/MED/LOW contribution."""
        t = _trip()
        runway = RunwayMap(tenant_id=MASTER, year=2026, cells=[])
        plan = compute_quarter_plan(
            MASTER, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway,
            atoms_by_trip={t.id: [_atom(t.id, "HIGH", TENANT) for _ in range(4)]})
        # SIGNAL_SCORE_MAP["HIGH"] == 1.0
        assert _dist_of(plan, t.id) == 1.0

    def test_platform_atoms_excluded_from_mixed_average(self):
        """In a mix, only the tenant-scored atoms count toward the average; the platform atoms
        are excluded entirely (not counted as MED)."""
        t = _trip()
        runway = RunwayMap(tenant_id=MASTER, year=2026, cells=[])
        atoms = [_atom(t.id, "HIGH", TENANT), _atom(t.id, "LOW", "platform"),
                 _atom(t.id, "LOW", "platform")]
        plan = compute_quarter_plan(
            MASTER, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={t.id: atoms})
        # Only the single HIGH tenant atom counts -> 1.0, not (1.0+0.1+0.1)/3.
        assert _dist_of(plan, t.id) == 1.0


class TestRankingOrderUnchangedAllPlatform:
    def test_order_unchanged_on_all_platform_fixture(self):
        """Today's data is 100% platform atoms, all MED. Under the old code the distinctiveness
        term was a flat 0.5 constant across every trip; under option (b) it is a flat 0.0
        constant. A constant term cannot change the RELATIVE ranking of trips either way — so the
        order the plan selects must be identical to the order driven by the other terms alone.

        Build three trips whose non-distinctiveness signals (richness, via atom COUNT) strictly
        order them, give every one only platform atoms, and assert the ranked order matches that
        richness order exactly."""
        runway = RunwayMap(tenant_id=MASTER, year=2026, cells=[])
        few = _trip(name="Few", destination="A")
        mid = _trip(name="Mid", destination="B")
        many = _trip(name="Many", destination="C")
        atoms_by_trip = {
            few.id: [_atom(few.id, "MED", "platform") for _ in range(2)],
            mid.id: [_atom(mid.id, "MED", "platform") for _ in range(5)],
            many.id: [_atom(many.id, "MED", "platform") for _ in range(9)],
        }
        plan = compute_quarter_plan(
            MASTER, 2026, 1, [few, mid, many], markets=["US"], capacity_posts_per_week=10,
            specials=[], runway=runway, atoms_by_trip=atoms_by_trip)

        # trip_scores is sorted by descending total score. With distinctiveness a constant, the
        # only differing term is richness (atom count) -> many > mid > few.
        ranked_ids = [ts.trip_id for ts in plan.trip_scores]
        assert ranked_ids == [many.id, mid.id, few.id]
        # And every distinctiveness_score is the same constant (0.0) — the term truly carries no
        # ordering information on this fixture.
        assert {ts.distinctiveness_score for ts in plan.trip_scores} == {0.0}

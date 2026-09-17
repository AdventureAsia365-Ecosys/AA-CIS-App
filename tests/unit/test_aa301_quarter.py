"""AA-301 — N5 Quarter Plan: B4 (fuzzy special matching) and B5 (thin-trip
share cap) fixes, Gate B (approval required).

Pure-Python logic — no DB, no LLM. TestFetchAtomsByTripDbWrapper is the one
exception: it exercises the async DB-wiring layer (fetch_atoms_by_trip/
_row_to_atom) with a mocked asyncpg pool, per the pool.acquire() convention
in test_aa299_atom_insert.py.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.acp_planning.constants import THIN_TRIP_MAX_SHARE
from services.acp_planning.models import (AtomRecord, RunwayCell, RunwayMap, Trip,
                                          compute_trips_hash)
from services.acp_planning.quarter import (_cap_thin_trip_shares, _fuzzy_match,
                                           _parse_jsonb, _row_to_atom, _score_reason,
                                           approve_quarter_plan, compute_quarter_plan,
                                           fetch_atoms_by_trip)
from services.acp_shared.atom_constants import THIN_TRIP_ATOM_MIN

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _trip(**over):
    base = dict(id=uuid.uuid4(), name="Test Trip", destination="Testland", period="Mar-May",
                lifecycle_stage="active")
    base.update(over)
    return Trip(**base)


def _atom(trip_id, distinctiveness="HIGH", atom_id=None):
    return AtomRecord(atom_id=atom_id or f"atom_{uuid.uuid4().hex[:8]}", trip_id=trip_id,
                       text="atom text", distinctiveness=distinctiveness)


class TestFuzzyMatchB4:
    """Issue's own worked examples."""

    def test_sapa_trekking_matches_sapa_valley_trek(self):
        assert _fuzzy_match("sapa trekking", "Sapa Valley Trek", "Sapa") is True

    def test_ha_giang_loop_matches_full_name(self):
        assert _fuzzy_match("ha giang loop", "Ha Giang Loop by Motorbike", "Ha Giang") is True

    def test_unrelated_trip_not_matched(self):
        assert _fuzzy_match("sapa trekking", "Hanoi City Tour", "Vietnam") is False

    def test_substring_bug_no_longer_reproduces(self):
        """The original bug: 'sapa trekking' in 'sapa valley trek'.lower() ->
        False. Confirm the OLD substring check would have failed, to lock in
        that we replaced it, not just happened to also pass."""
        old_buggy_result = "sapa trekking" in "sapa valley trek".lower()
        assert old_buggy_result is False
        assert _fuzzy_match("sapa trekking", "Sapa Valley Trek", "Sapa") is True


class TestThinTripCapB5:
    def test_thin_trip_share_capped(self):
        """Issue's own numbers: Ha Giang 4 atoms (thin, < THIN_TRIP_ATOM_MIN=5)
        got 0.54 share (54%) in the buggy version — must be capped."""
        shares = {"Ha Giang": 0.54, "Mongolia": 0.31, "Sapa": 0.15}
        atom_counts = {"Ha Giang": 4, "Mongolia": 12, "Sapa": 10}
        capped, notes = _cap_thin_trip_shares(shares, atom_counts)
        assert capped["Ha Giang"] <= THIN_TRIP_MAX_SHARE
        assert any("Ha Giang" in n and "thin" in n for n in notes)

    def test_non_thin_trip_not_capped(self):
        shares = {"Mongolia": 0.9}
        atom_counts = {"Mongolia": 12}
        capped, notes = _cap_thin_trip_shares(shares, atom_counts)
        assert capped["Mongolia"] == 0.9
        assert notes == []

    def test_freed_share_redistributed_sums_to_one(self):
        shares = {"Ha Giang": 0.54, "Mongolia": 0.31, "Sapa": 0.15}
        atom_counts = {"Ha Giang": 4, "Mongolia": 12, "Sapa": 10}
        capped, _ = _cap_thin_trip_shares(shares, atom_counts)
        assert abs(sum(capped.values()) - 1.0) < 1e-9

    def test_exactly_at_threshold_not_thin(self):
        shares = {"X": 0.9}
        atom_counts = {"X": THIN_TRIP_ATOM_MIN}  # == 5, not < 5
        capped, notes = _cap_thin_trip_shares(shares, atom_counts)
        assert capped["X"] == 0.9
        assert notes == []


class TestComputeQuarterPlan:
    def test_forced_special_included_via_fuzzy_match(self):
        sapa = _trip(name="Sapa Valley Trek", destination="Sapa")
        other = _trip(name="Random Beach Tour", destination="Elsewhere")
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        atoms_by_trip = {sapa.id: [_atom(sapa.id) for _ in range(6)],
                         other.id: [_atom(other.id) for _ in range(6)]}
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [sapa, other], markets=["US"], capacity_posts_per_week=1,
            specials=["sapa trekking"], runway=runway, atoms_by_trip=atoms_by_trip)
        assert sapa.id in plan.forced_specials

    def test_thin_trip_share_capped_end_to_end(self):
        thin = _trip(name="Ha Giang Loop", destination="Ha Giang")
        rich = _trip(name="Mongolia Gobi", destination="Mongolia")
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        atoms_by_trip = {thin.id: [_atom(thin.id) for _ in range(4)],
                         rich.id: [_atom(rich.id) for _ in range(12)]}
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [thin, rich], markets=["US"], capacity_posts_per_week=4,
            specials=[], runway=runway, atoms_by_trip=atoms_by_trip)
        assert plan.destination_shares.get("Ha Giang", 0) <= THIN_TRIP_MAX_SHARE
        assert plan.thin_trip_notes

    def test_retired_trip_excluded(self):
        retired = _trip(lifecycle_stage="retired")
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [retired], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={})
        assert retired.id not in plan.trip_ids

    def test_gate_b_defaults_unapproved(self):
        t = _trip()
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={t.id: [_atom(t.id)]})
        assert plan.approved is False
        assert plan.approved_by is None

    def test_approve_quarter_plan_sets_gate(self):
        t = _trip()
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={t.id: [_atom(t.id)]})
        approve_quarter_plan(plan, approved_by="ms.thu")
        assert plan.approved is True
        assert plan.approved_by == "ms.thu"

    def test_trips_hash_stamped(self):
        t = _trip()
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={})
        assert plan.trips_hash == compute_trips_hash([t])


class TestScoreReasonTieBreak:
    """AA-323 round 6, Phần C — round 5's live audit found 562/763 real trips
    (78% of the trips showing this label) mislabeled "High runway fit" while
    their actual runway_fit was 0.0, because plain `max(dict, key=...)`
    resolves a tie to whichever key is listed FIRST, not the one that's
    actually largest. Locks in the fix: an honest label for the all-zero
    case, "Balanced" for a genuine nonzero tie, and the correct single
    dominant factor otherwise."""

    # AA-448 round 1 — _score_reason() gained a 4th positional arg (dfs_score). Round 6 — a 5th
    # (engagement_score). Weights now come from QUARTER_SCORE_WEIGHTS (constants.py:
    # runway_fit=0.30, richness=0.20, distinctiveness=0.20, dfs_relevance=0.15,
    # engagement_adjustment=0.15 — was 0.4/0.3/0.3 pre-AA-448, then 0.32/0.24/0.24/0.20 after
    # round 1, now this). Every call below carries both new args; 0.0 in the original 3-factor
    # tests keeps them isolating exactly the same comparison they always did (a 0.0 term
    # contributes nothing). 2 new tests at the bottom cover dfs_relevance's and
    # engagement_adjustment's own dominance cases — the two genuinely new behaviors AA-448 adds.

    def test_forced_short_circuits_before_any_contribution_check(self):
        assert _score_reason(0.9, 0.9, 0.9, 0.9, 0.9, forced=True) == "Manually added"

    def test_all_zero_no_longer_defaults_to_first_key(self):
        """The exact real-world case round 5 found: runway_fit=richness=dist=0.0
        (no BOFU/MOFU window yet, no curated atoms) must NOT read as
        'High runway fit' — the literal opposite of what 0.0 means."""
        reason = _score_reason(0.0, 0.0, 0.0, 0.0, 0.0, forced=False)
        assert reason != "High runway fit (BOFU/MOFU window this quarter)"
        assert reason == "No runway or atom signal yet this quarter"

    def test_genuine_runway_fit_dominance_still_labeled_correctly(self):
        # runway_fit*0.30=0.30, richness*0.20=0.10, dist*0.20=0.04, dfs*0.15=0.0,
        # engagement*0.15=0.0 -> runway_fit wins
        reason = _score_reason(1.0, 0.5, 0.2, 0.0, 0.0, forced=False)
        assert reason == "High runway fit (BOFU/MOFU window this quarter)"

    def test_genuine_richness_dominance_labeled_correctly(self):
        # richness*0.20=0.20 beats runway_fit*0.30=0.06, dist*0.20=0.02, dfs*0.15=0.0,
        # engagement*0.15=0.0
        reason = _score_reason(0.2, 1.0, 0.1, 0.0, 0.0, forced=False)
        assert reason == "Rich atom pool"

    def test_genuine_distinctiveness_dominance_labeled_correctly(self):
        # dist*0.20=0.20 beats runway_fit*0.30=0.06, richness*0.20=0.02, dfs*0.15=0.0,
        # engagement*0.15=0.0
        reason = _score_reason(0.2, 0.1, 1.0, 0.0, 0.0, forced=False)
        assert reason == "High-distinctiveness atoms"

    def test_genuine_dfs_relevance_dominance_labeled_correctly(self):
        # AA-448 round 1 axis. dfs*0.15=0.15 beats runway_fit*0.30=0.06, richness*0.20=0.02,
        # dist*0.20=0.02, engagement*0.15=0.0.
        reason = _score_reason(0.2, 0.1, 0.1, 1.0, 0.0, forced=False)
        assert reason == "Strong search demand (DFS)"

    def test_genuine_engagement_adjustment_dominance_labeled_correctly(self):
        # AA-448 round 6 axis. engagement*0.15=0.15 beats runway_fit*0.30=0.06,
        # richness*0.20=0.02, dist*0.20=0.02, dfs*0.15=0.0.
        reason = _score_reason(0.2, 0.1, 0.1, 0.0, 1.0, forced=False)
        assert reason == "Strong real engagement (feedback)"

    def test_nonzero_tie_reads_as_balanced_not_first_key(self):
        # richness*0.20 == dist*0.20 == 0.20 (same multiplier applied to the same
        # value 1.0, so this is an EXACT float tie, not just close) -> both
        # beat runway_fit*0.30=0.0, dfs*0.15=0.0, engagement*0.15=0.0 -> a real tie between two
        # nonzero factors.
        reason = _score_reason(0.0, 1.0, 1.0, 0.0, 0.0, forced=False)
        assert reason == "Balanced score (multiple factors tied)"


class TestNoLlmCost:
    def test_no_bedrock_or_anthropic_imports(self):
        import services.acp_planning.quarter as mod
        src = open(mod.__file__).read()
        for banned in ("boto3", "bedrock", "anthropic", "invoke_model", "invoke_claude"):
            assert banned not in src.lower(), f"N5 must be $0 LLM — found '{banned}' in quarter.py"


class TestParseJsonb:
    """Real bug found live in production (AA-300): asyncpg has no jsonb
    codec registered on this app's connections (same gap AA-314 already
    found for src_highlights elsewhere), so tour_atoms.cooldown_until/
    usage_log (both JSONB) arrive as raw JSON strings — e.g. '{}', '[]' —
    not parsed dict/list. _row_to_atom() previously passed these straight
    into AtomRecord, which pydantic correctly rejected
    (pydantic_core.ValidationError: 'Input should be a valid dictionary
    [type=dict_type, input_value=\\'{}\\', input_type=str]'), crashing
    GET /admin/atoms/preview-slotgrid with a real 500 in production. Every
    test in this suite up to this point mocked DB rows with real Python
    {}/[] instead of the actual string shape asyncpg produces, so nothing
    caught it — these tests exist specifically to close that mock-fidelity
    gap, not just to re-test the fix in isolation."""

    def test_string_dict_is_parsed(self):
        assert _parse_jsonb('{"a": 1}', {}) == {"a": 1}

    def test_string_list_is_parsed(self):
        assert _parse_jsonb('["a", "b"]', []) == ["a", "b"]

    def test_empty_string_uses_default(self):
        assert _parse_jsonb("", {"fallback": True}) == {"fallback": True}

    def test_already_parsed_dict_passed_through(self):
        """Defensive — if a jsonb codec is ever registered later, asyncpg
        would hand back a real dict/list directly; must not double-parse."""
        assert _parse_jsonb({"a": 1}, {}) == {"a": 1}

    def test_none_uses_default(self):
        assert _parse_jsonb(None, []) == []

    def test_row_to_atom_with_real_asyncpg_string_shape(self):
        """The exact live crash, reproduced directly: a raw asyncpg Record
        with cooldown_until/usage_log as JSON strings, not dict/list."""
        trip_id = uuid.uuid4()
        row = {
            "atom_id": "atom_x", "tour_id": trip_id, "text": "some atom text",
            "activity_type": "trek",
            "distinctiveness": "HIGH", "deleted": False, "weight": 1.5,
            "cooldown_until": '{"blog": "2026-08-01"}', "usage_log": '["a", "b"]',
        }
        atom = _row_to_atom(row)
        assert atom.cooldown_until == {"blog": "2026-08-01"}
        assert atom.usage_log == ["a", "b"]
        assert atom.activity_type == "trek"

    def test_row_to_atom_with_empty_jsonb_strings(self):
        """The exact production traceback's inputs verbatim: '{}' and '[]'."""
        trip_id = uuid.uuid4()
        row = {
            "atom_id": "atom_y", "tour_id": trip_id, "text": "atom text",
            "activity_type": None,
            "distinctiveness": "LOW", "deleted": False, "weight": 1.0,
            "cooldown_until": "{}", "usage_log": "[]",
        }
        atom = _row_to_atom(row)  # must not raise pydantic_core.ValidationError
        assert atom.cooldown_until == {}
        assert atom.usage_log == []


class TestFetchAtomsByTripDbWrapper:
    @pytest.mark.asyncio
    async def test_query_returns_real_asyncpg_shaped_atoms(self):
        """End-to-end through fetch_atoms_by_trip() with a mocked pool
        whose rows use the real asyncpg string shape for JSONB columns —
        the exact path that crashed in production."""
        trip_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetch.return_value = [{
            "atom_id": "atom_z", "tour_id": trip_id, "text": "text",
            "activity_type": "food",
            "distinctiveness": "MED", "deleted": False, "weight": 1.0,
            "cooldown_until": "{}", "usage_log": "[]",
        }]
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        by_trip = await fetch_atoms_by_trip(TENANT, pool)

        assert trip_id in by_trip
        atom = by_trip[trip_id][0]
        assert atom.cooldown_until == {}
        assert atom.usage_log == []

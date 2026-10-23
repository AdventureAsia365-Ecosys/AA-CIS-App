"""AA-301 — N4 Runway Map: B9 (family detection + per-trip offset) and B11
(MOFU band) fixes, duration parser, recompute trigger, trip_url no-op.

Pure Python — no DB, no LLM. compute_runway_map() is unit-tested directly
with in-memory Trip fixtures.

AA-516 (03/09/2026): TestFetchTripsDbWrapper (the DB-wrapper tests for `fetch_trips()`/
`runway_map()`) was removed along with those functions — deleted as dead N4-N6 admin-triggered
code (Slate/AA-511 replaced it, T7 reads trips via tenant_pool.py instead). Every other class
here still covers compute_runway_map() and its pure helpers, all untouched and still live.
"""
import uuid
from collections import Counter

import pytest

from services.acp_planning.models import RunwayMap, Trip, compute_trips_hash, needs_recompute
from services.acp_planning.runway import (
    _family_detected, _offset_for, _offsets_for_destination, _stage_for_dist,
    compute_runway_map, dead_trip_url_alarms, parse_duration_days, parse_period,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _trip(**over):
    base = dict(id=uuid.uuid4(), name="Test Trip", destination="Testland", period="Mar-May",
                duration_raw="8 days", itinerary_source="Day 1: trekking.", lifecycle_stage="active",
                trip_url=None, url_alive=None)
    base.update(over)
    return Trip(**base)


class TestParsePeriod:
    def test_two_seasons(self):
        assert parse_period("Mar-May,Sep-Nov") == [3, 4, 5, 9, 10, 11]

    def test_single_range(self):
        assert parse_period("May-Oct") == [5, 6, 7, 8, 9, 10]

    def test_empty(self):
        assert parse_period(None) == []
        assert parse_period("") == []


class TestDurationParser:
    """Built from a live survey of 749 non-null v_trip_registry.duration_raw
    rows (aa_internal tenant, 2026-07-22), not guessed from 1-2 examples."""

    @pytest.mark.parametrize("raw,expected", [
        ("11 days", 11), ("9 days", 9), ("5 DAYS", 5), ("15 DAYS", 15),
        ("1 day", 1), ("1 DAY", 1),
        ("13 days 12 nights", 13), ("18 days 17 nights", 18),
        ("4 DAYS \n3 NIGHTS", 4), ("7 DAYS\n6 NIGHTS", 7),
        ("12days 11nights", 12),
        ("HALF DAY", 0), ("half day", 0), ("Half Day", 0),
        ("12 hours", 0), ("7 HOURS", 0), ("3.5 hours", 0), ("4-5 hours", 0), ("14 - 15 Hours", 0),
        ("DAY TRIP", 0), ("same day", 0),
        ("OVERNIGHT TRIP", 1),
        ("Total time: 6 hours", 0),
        ("30 MIN.", 0),
        ("7-8 hours (Day Hike)", 0),
        ("12 days`", 12),
    ])
    def test_known_shapes(self, raw, expected):
        assert parse_duration_days(raw) == expected

    def test_none_and_empty_are_unparseable(self):
        assert parse_duration_days(None) is None
        assert parse_duration_days("") is None
        assert parse_duration_days("   ") is None

    def test_garbage_is_unparseable_not_fabricated(self):
        assert parse_duration_days("call for details") is None


class TestFamilyDetectionB9:
    def test_family_in_itinerary_source_detected(self):
        # issue's own Mongolia example
        assert _family_detected("...ger camps, nomad families.") is True

    def test_no_family_word(self):
        assert _family_detected("Day 1: trekking through mountains.") is False

    def test_none_is_false(self):
        assert _family_detected(None) is False

    def test_offset_uses_itinerary_source_not_summary(self):
        """B9 — the fix must key off itinerary_source; a trip whose
        itinerary_source mentions family but is short should still get the
        family_extended (6,12) offset for long-haul markets, matching the
        original bug's own worked example (Mongolia Gobi 8-day tour wrongly
        got 6-12mo lead time from a substring match on 'families')."""
        t = _trip(duration_raw="8 days", itinerary_source="...ger camps, nomad families.")
        assert _offset_for("US", t) == (6, 12)

    def test_long_trip_without_family_word_still_extended(self):
        t = _trip(duration_raw="18 days", itinerary_source="Day 1: trekking.")
        assert _offset_for("US", t) == (6, 12)

    def test_short_trip_no_family_word_gets_long_haul(self):
        t = _trip(duration_raw="8 days", itinerary_source="Day 1: trekking.")
        assert _offset_for("US", t) == (3, 6)

    def test_short_haul_market_ignores_family(self):
        t = _trip(duration_raw="8 days", itinerary_source="families welcome")
        assert _offset_for("VN", t) == (0.5, 2)


class TestOffsetPerTripB9:
    def test_representative_trip_bug_fixed(self):
        """B9 — destination-level offset must NOT come from a single
        'representative' trip. A destination with one short trip and one
        long/family trip must produce a WIDER aggregate window (min lo, max
        hi across all trips), not just whichever trip happened to be
        trips[0]."""
        short_trip = _trip(duration_raw="5 days", itinerary_source="city tour")
        family_trip = _trip(duration_raw="8 days", itinerary_source="families welcome, ger camps")
        lo, hi = _offsets_for_destination("US", [short_trip, family_trip])
        assert (lo, hi) == (3, 12)  # min(lo of long_haul=3, lo of family_extended=3), max(hi=6, hi=12)

        # order must not matter (this is the literal trips[0] bug being tested)
        lo2, hi2 = _offsets_for_destination("US", [family_trip, short_trip])
        assert (lo2, hi2) == (lo, hi)


class TestMofuBandB11:
    def test_sapa_two_seasons_has_mofu(self):
        """B11 — issue's own worked example: Sapa (Mar-May, Sep-Nov, season
        starts 6 months apart), long_haul lo=3/hi=6. Previously 0 MOFU
        cells all year for this exact geometry."""
        starts = [3, 9]
        lo, hi = 3, 6
        stages = []
        for month in range(1, 13):
            dist = min(((s - month) % 12) for s in starts)
            stages.append(_stage_for_dist(dist, lo, hi))
        counts = Counter(stages)
        assert counts["MOFU"] > 0, f"B11 regression: 0 MOFU cells, counts={counts}"

    def test_off_never_happens_is_no_longer_guaranteed_dead(self):
        """Issue flags OFF as observed dead code in the buggy version. The
        redesigned band still reaches OFF for distances far beyond the
        extended TOFU zone — verify OFF is reachable, not asserting it must
        appear in the Sapa case specifically (Sapa's max dist is bounded)."""
        assert _stage_for_dist(dist=50, lo=0.5, hi=2) == "OFF"

    def test_bofu_still_covers_the_inner_booking_window(self):
        assert _stage_for_dist(dist=3, lo=3, hi=6) == "BOFU"

    def test_dist_below_lo_is_tofu_fallback(self):
        assert _stage_for_dist(dist=1, lo=3, hi=6) == "TOFU"


class TestComputeRunwayMap:
    def test_two_season_destination_produces_mofu_end_to_end(self):
        sapa = _trip(name="Sapa Valley Trek", destination="Sapa", period="Mar-May,Sep-Nov",
                     duration_raw="4 days", itinerary_source="trekking through terraced rice fields")
        rm = compute_runway_map(TENANT, 2026, [sapa], markets=["US"])
        stages = [c.stage for c in rm.cells if c.destination == "Sapa" and c.market == "US"]
        assert "MOFU" in stages

    def test_missing_period_excluded_and_logged(self):
        t = _trip(period=None)
        rm = compute_runway_map(TENANT, 2026, [t], markets=["US"])
        assert rm.cells == []
        assert any("no PERIOD" in u for u in rm.unknowns)

    def test_retired_trip_excluded_and_logged(self):
        # AA-986 L3 (ADR 0015 Q9): a tour AA no longer serves (or has paused) is skipped, by its served state
        for state in ("retired", "paused"):
            t = _trip(served_state=state)
            rm = compute_runway_map(TENANT, 2026, [t], markets=["US"])
            assert rm.cells == []
            assert any("no longer served" in u for u in rm.unknowns)

    def test_sales_retired_stage_alone_is_not_a_deletion(self):
        # Q9: lifecycle_stage is the sales phase (nothing writes it); a served tour stays in the runway
        t = _trip(lifecycle_stage="retired", served_state="served")
        assert compute_runway_map(TENANT, 2026, [t], markets=["US"]).cells

    def test_phasing_out_trip_still_included_in_runway(self):
        """phasing_out affects N6 allocation (current month only), not N4's
        runway map itself — a phasing_out trip still has a real season."""
        t = _trip(lifecycle_stage="phasing_out")
        rm = compute_runway_map(TENANT, 2026, [t], markets=["US"])
        assert len(rm.cells) == 12  # one cell per month for this destination/market

    def test_tenant_id_stamped_on_output(self):
        rm = compute_runway_map(TENANT, 2026, [_trip()], markets=["US"])
        assert rm.tenant_id == TENANT


class TestTripUrlAlarmNoOp:
    def test_none_url_alive_is_not_alarmed(self):
        """tenant_tour_pages is empty in prod today (migration 078) — every
        trip has url_alive=None. Must NOT be treated as a dead-link alarm."""
        t = _trip(trip_url=None, url_alive=None)
        assert dead_trip_url_alarms([t]) == []

    def test_confirmed_dead_link_is_alarmed(self):
        t = _trip(trip_url="https://dead.example.com", url_alive=False)
        assert dead_trip_url_alarms([t]) == [str(t.id)]

    def test_confirmed_alive_link_not_alarmed(self):
        t = _trip(trip_url="https://alive.example.com", url_alive=True)
        assert dead_trip_url_alarms([t]) == []


class TestRecomputeTrigger:
    def test_no_change_no_recompute(self):
        trips = [_trip()]
        h = compute_trips_hash(trips)
        assert needs_recompute(h, trips) is False

    def test_new_trip_triggers_recompute(self):
        trips = [_trip()]
        h = compute_trips_hash(trips)
        trips2 = trips + [_trip()]
        assert needs_recompute(h, trips2) is True

    def test_period_change_triggers_recompute(self):
        t = _trip(period="Mar-May")
        h = compute_trips_hash([t])
        t2 = t.model_copy(update={"period": "Jun-Aug"})
        assert needs_recompute(h, [t2]) is True

    def test_lifecycle_stage_change_triggers_recompute(self):
        t = _trip(lifecycle_stage="active")
        h = compute_trips_hash([t])
        t2 = t.model_copy(update={"lifecycle_stage": "phasing_out"})
        assert needs_recompute(h, [t2]) is True


class TestNoLlmCost:
    def test_no_bedrock_or_anthropic_imports(self):
        import services.acp_planning.runway as mod
        src = open(mod.__file__).read()
        for banned in ("boto3", "bedrock", "anthropic", "invoke_model", "invoke_claude"):
            assert banned not in src.lower(), f"N4 must be $0 LLM — found '{banned}' in runway.py"

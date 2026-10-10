"""AA-754 — the quarter-plan scoring weights after the `distinctiveness` term was removed
end-to-end. Pure Python, no DB, no LLM.

Two things are locked in here:
  1. QUARTER_SCORE_WEIGHTS re-normalizes to exactly 1.0 after dropping the 0.20 distinctiveness
     term (the remaining 4 terms were each divided by the old 0.80 non-distinctiveness total, so
     their relative proportions are unchanged: runway_fit still largest, richness second,
     dfs_relevance == engagement_adjustment smallest).
  2. A small fixture ranking: three trips whose only differences are the surviving signals, to
     confirm the removed distinctiveness term no longer influences the order and the surviving
     terms still rank trips the way their weights say they should.
"""
import math
import uuid

from services.acp_planning.constants import QUARTER_SCORE_WEIGHTS
from services.acp_planning.models import RunwayCell, RunwayMap, Trip
from services.acp_planning.quarter import compute_quarter_plan

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestWeightsRenormalized:
    def test_weights_sum_to_one(self):
        assert math.isclose(sum(QUARTER_SCORE_WEIGHTS.values()), 1.0, abs_tol=1e-9)

    def test_distinctiveness_term_removed(self):
        assert "distinctiveness" not in QUARTER_SCORE_WEIGHTS
        assert set(QUARTER_SCORE_WEIGHTS) == {
            "runway_fit", "richness", "dfs_relevance", "engagement_adjustment"}

    def test_relative_proportions_preserved_from_pre_aa754(self):
        """The pre-AA-754 non-distinctiveness weights were runway_fit=0.30, richness=0.20,
        dfs_relevance=0.15, engagement_adjustment=0.15 (sum 0.80). Dividing each by 0.80 is the
        exact re-normalization applied, so each new weight equals old/0.80."""
        old = {"runway_fit": 0.30, "richness": 0.20, "dfs_relevance": 0.15,
               "engagement_adjustment": 0.15}
        for k, old_w in old.items():
            assert math.isclose(QUARTER_SCORE_WEIGHTS[k], old_w / 0.80, abs_tol=1e-9)

    def test_runway_largest_dfs_and_engagement_smallest_and_equal(self):
        w = QUARTER_SCORE_WEIGHTS
        assert w["runway_fit"] > w["richness"] > w["dfs_relevance"]
        assert math.isclose(w["dfs_relevance"], w["engagement_adjustment"], abs_tol=1e-9)


class TestFixtureRanking:
    """Fixture ranking (explained in result.md):

    Three trips, all `active`, each with a full atom pool (10 atoms so `richness` saturates at
    1.0 for every trip, holding richness constant) and the neutral default feedback weight
    (`engagement_adjustment` == 0.5 for all three, held constant). The only varied signal is
    runway: `runway_fit` is driven by the RunwayMap below.

    - RUNWAY_TRIP has a BOFU window in the US market across the quarter's months -> runway_fit=1.0
    - QUIET_TRIP has no BOFU/MOFU window at all -> runway_fit=0.0
    - MID_TRIP has a window in one of the three months -> runway_fit between the two

    Expected order: RUNWAY_TRIP > MID_TRIP > QUIET_TRIP, driven purely by runway_fit (the
    largest surviving weight). The point of the fixture is that this order is produced with NO
    distinctiveness contribution anywhere — before AA-754 every atom scored a constant MED
    distinctiveness, so that term added the same amount to all three and never changed the order;
    after AA-754 the term is simply gone, and the order is identical.
    """

    def _atoms(self, trip_id, n=10):
        from services.acp_planning.models import AtomRecord
        return [AtomRecord(atom_id=f"a_{uuid.uuid4().hex[:8]}", trip_id=trip_id, text="t")
                for _ in range(n)]

    def test_ranking_driven_by_surviving_signals_only(self):
        runway_trip = Trip(id=uuid.uuid4(), name="Runway Trip", destination="Runwayland",
                           lifecycle_stage="active")
        mid_trip = Trip(id=uuid.uuid4(), name="Mid Trip", destination="Midland",
                        lifecycle_stage="active")
        quiet_trip = Trip(id=uuid.uuid4(), name="Quiet Trip", destination="Quietland",
                          lifecycle_stage="active")

        # Q1 2026 -> months 1, 2, 3. US market.
        cells = [
            # Runwayland: a BOFU window in all three months -> runway_fit = 1.0
            RunwayCell(destination="Runwayland", market="US", month=1, stage="BOFU"),
            RunwayCell(destination="Runwayland", market="US", month=2, stage="BOFU"),
            RunwayCell(destination="Runwayland", market="US", month=3, stage="BOFU"),
            # Midland: a MOFU window in one month only -> runway_fit = 1/3
            RunwayCell(destination="Midland", market="US", month=1, stage="MOFU"),
            # Quietland: nothing -> runway_fit = 0.0 (OFF everywhere)
        ]
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=cells)

        trips = [quiet_trip, mid_trip, runway_trip]  # deliberately out of order
        atoms_by_trip = {t.id: self._atoms(t.id) for t in trips}

        plan = compute_quarter_plan(
            TENANT, 2026, 1, trips, markets=["US"], capacity_posts_per_week=4,
            specials=[], runway=runway, atoms_by_trip=atoms_by_trip)

        # trip_scores is sorted by -score; confirm the exact runway-driven order.
        ordered_names = [ts.name for ts in plan.trip_scores]
        assert ordered_names == ["Runway Trip", "Mid Trip", "Quiet Trip"]

        by_name = {ts.name: ts for ts in plan.trip_scores}
        # richness and engagement are held constant across all three; runway_fit is the mover.
        # TripScore.runway_fit is round(…, 3), so compare against the rounded expected values.
        assert by_name["Runway Trip"].runway_fit == 1.0
        assert by_name["Mid Trip"].runway_fit == round(1 / 3, 3)
        assert by_name["Quiet Trip"].runway_fit == 0.0
        assert by_name["Runway Trip"].richness == by_name["Quiet Trip"].richness == 1.0

    def test_tripscore_has_no_distinctiveness_field(self):
        t = Trip(id=uuid.uuid4(), name="T", destination="D", lifecycle_stage="active")
        runway = RunwayMap(tenant_id=TENANT, year=2026, cells=[])
        plan = compute_quarter_plan(
            TENANT, 2026, 1, [t], markets=["US"], capacity_posts_per_week=1,
            specials=[], runway=runway, atoms_by_trip={t.id: self._atoms(t.id, 3)})
        ts = plan.trip_scores[0]
        assert not hasattr(ts, "distinctiveness_score")

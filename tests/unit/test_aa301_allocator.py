"""AA-301 — N6 Slot Allocator: B7 (in-month cooldown) and B6 (per-slot
keyword_seed) fixes, atom floor (AA-300), reactive-hold, Gate B enforcement,
phasing_out current-month-only.

Pure Python — no DB, no LLM.
"""
import uuid
from datetime import date

import pytest

from services.acp_planning.allocator import compute_slot_grid
from services.acp_planning.models import (AtomRecord, QuarterPlan, QuarterPlanNotApprovedError,
                                          RunwayCell, RunwayMap, Trip)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT = uuid.UUID("a1b2c3d4-0001-4000-8000-000000000001")


def _trip(**over):
    base = dict(id=uuid.uuid4(), name="Ha Giang Loop by Motorbike", destination="Ha Giang",
                period="Mar-May", lifecycle_stage="active", trip_url=None, url_alive=None)
    base.update(over)
    return Trip(**base)


_ACTIVITY_TYPES = ["trek", "bike", "food", "culture", "stay", "transit", "other"]


def _atoms(trip_id, n):
    # AA-379 — activity_type cycles through the real 7-value decompose enum, matching
    # production (100% of live atoms have it set — it's a required field of the decompose
    # LLM contract). keyword_seed is now derived from it, so an all-None fixture would no
    # longer reflect real atom shape.
    return [AtomRecord(atom_id=f"atom_{i}_{uuid.uuid4().hex[:6]}", trip_id=trip_id,
                       text=f"atom text number {i} about limestone cliffs and rice terraces",
                       activity_type=_ACTIVITY_TYPES[i % len(_ACTIVITY_TYPES)])
            for i in range(n)]


def _full_runway(destination="Ha Giang", market="US", stage="BOFU"):
    return RunwayMap(tenant_id=TENANT, year=2026,
                     cells=[RunwayCell(destination=destination, market=market, month=m, stage=stage)
                            for m in range(1, 13)])


def _approved_plan(tenant_id, trip_ids, shares=None):
    return QuarterPlan(tenant_id=tenant_id, year=2026, quarter=1, trip_ids=trip_ids,
                       destination_shares=shares or {}, approved=True, approved_by="ms.thu")


class TestGateB:
    def test_unapproved_plan_raises(self):
        t = _trip()
        plan = QuarterPlan(tenant_id=TENANT, year=2026, quarter=1, trip_ids=[t.id])  # approved=False default
        runway = _full_runway()
        with pytest.raises(QuarterPlanNotApprovedError):
            compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                              {t.id: t}, {t.id: _atoms(t.id, 4)}, "US")

    def test_approved_plan_allocates(self):
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 12)}, "US")
        assert len(grid.slots) > 0

    def test_tenant_mismatch_refused(self):
        t = _trip()
        plan = _approved_plan(OTHER_TENANT, [t.id], {"Ha Giang": 1.0})  # wrong tenant on the plan
        runway = _full_runway()
        with pytest.raises(ValueError):
            compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                              {t.id: t}, {t.id: _atoms(t.id, 4)}, "US")


class TestB7NoAtomRepeatWithinMonth:
    def test_four_blog_slots_no_repeated_atom(self):
        """Issue's own example: Ha Giang has 4 atoms; buggy version reused
        the same 4 atoms for every blog slot in the month."""
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 16)  # enough atoms for several distinct slots
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        blog_slots = [s for s in grid.slots if s.kind == "evergreen"]
        all_atom_ids = [aid for s in blog_slots for aid in s.atom_ids]
        assert len(all_atom_ids) == len(set(all_atom_ids)), "B7 regression: an atom repeated within the month"


class TestB6DistinctKeywordSeed:
    def test_four_blog_slots_four_distinct_keyword_seeds(self):
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 16)
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        blog_slots = [s for s in grid.slots if s.kind == "evergreen"]
        seeds = [s.keyword_seed for s in blog_slots]
        assert len(seeds) >= 2
        assert len(seeds) == len(set(seeds)), "B6 regression: keyword_seed shared across slots"


class TestAtomFloorAA300:
    def test_thin_trip_drops_slots_instead_of_repeating(self):
        """AA-300: atom_sống < 2x số_ô -> reduce capacity + log, never
        silently repeat an atom. Issue's own numbers: Ha Giang has only 4
        atoms."""
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 4)  # thin — matches issue's own Ha Giang example
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        blog_slots = [s for s in grid.slots if s.kind == "evergreen"]
        all_atom_ids = [aid for s in blog_slots for aid in s.atom_ids]
        assert len(all_atom_ids) == len(set(all_atom_ids))
        assert grid.capacity_note is not None
        assert "atom floor" in grid.capacity_note

    def test_capacity_note_deduped(self):
        """A single exhausted destination/channel can be retried many times
        by the round-robin in one call — the log must not repeat the exact
        same line dozens of times."""
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 4)
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        lines = grid.capacity_note.split(" | ")
        assert len(lines) == len(set(lines))


class TestReactiveHoldSlots:
    def test_ten_percent_held_empty_and_logged(self):
        t = _trip()
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 20)
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        held = [s for s in grid.slots if s.kind == "reactive_hold"]
        assert len(held) >= 1
        assert all(s.trip_id is None and s.atom_ids == [] for s in held)
        assert all("Mode-B" in (s.topic_hint or "") for s in held)


class TestPhasingOutCurrentMonthOnly:
    def test_phasing_out_excluded_from_future_month(self):
        t = _trip(lifecycle_stage="phasing_out")
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 12)
        future_year, future_month = 2099, 6  # guaranteed not "today"
        grid = compute_slot_grid(TENANT, future_year, future_month, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US")
        assert not any(s.trip_id == t.id for s in grid.slots)
        assert "phasing_out" in (grid.capacity_note or "")

    def test_phasing_out_included_in_current_month(self):
        today = date.today()
        t = _trip(lifecycle_stage="phasing_out")
        plan = _approved_plan(TENANT, [t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        atoms = _atoms(t.id, 12)
        grid = compute_slot_grid(TENANT, today.year, today.month, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: atoms}, "US", today=today)
        assert any(s.trip_id == t.id for s in grid.slots)


class TestNoLlmCost:
    def test_no_bedrock_or_anthropic_imports(self):
        import services.acp_planning.allocator as mod
        src = open(mod.__file__).read()
        for banned in ("boto3", "bedrock", "anthropic", "invoke_model", "invoke_claude"):
            assert banned not in src.lower(), f"N6 must be $0 LLM — found '{banned}' in allocator.py"

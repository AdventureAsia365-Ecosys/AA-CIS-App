"""AA-449 — T8's channel extension to services/acp_planning (Slot.channel 4 -> 8 values).

STEP0 (docs/claude_audit/AA-449-00-step0-t8-angle-gate-investigation.md §5) found this was a
REAL, not theoretical, bug: acp_shared.tenant_config.channels is unconstrained free text at the
DB layer, but Slot.channel's Pydantic Literal only accepted 4 values — a tenant configuring any
of Bang 2's other 3 channels would make compute_slot_grid() raise a real ValidationError the
moment it tried to construct a Slot. This file is the "test bắt buộc" the build task named
explicitly: (1) all 8 channels must not crash, (2) the original 4 must behave exactly as before
(regression).

Pure Python — no DB, no LLM. Same fixture style as test_aa301_allocator.py.
"""
import uuid
from datetime import date

import pytest

from services.acp_planning.allocator import compute_slot_grid
from services.acp_planning.models import AtomRecord, Channel, QuarterPlan, RunwayCell, RunwayMap, Trip

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")

# The 4 channels Slot.channel supported before AA-449 — must keep behaving identically.
_ORIGINAL_CHANNELS = ["blog", "facebook", "tiktok", "email"]
# The 4 new channels AA-449 adds, per Bang 2 (T8's channel-style table).
_NEW_CHANNELS = ["linkedin", "instagram", "landing_page", "ads"]
_ALL_CHANNELS = _ORIGINAL_CHANNELS + _NEW_CHANNELS

_ACTIVITY_TYPES = ["trek", "bike", "food", "culture", "stay", "transit", "other"]


def _trip(**over):
    base = dict(id=uuid.uuid4(), name="Ha Giang Loop by Motorbike", destination="Ha Giang",
                period="Mar-May", lifecycle_stage="active", trip_url=None, url_alive=None)
    base.update(over)
    return Trip(**base)


def _atoms(trip_id, n):
    return [AtomRecord(atom_id=f"atom_{i}_{uuid.uuid4().hex[:6]}", trip_id=trip_id,
                       text=f"atom text number {i} about limestone cliffs and rice terraces",
                       activity_type=_ACTIVITY_TYPES[i % len(_ACTIVITY_TYPES)])
            for i in range(n)]


def _full_runway(destination="Ha Giang", market="US", stage="BOFU"):
    return RunwayMap(tenant_id=TENANT, year=2026,
                     cells=[RunwayCell(destination=destination, market=market, month=m, stage=stage)
                            for m in range(1, 13)])


def _approved_plan(trip_ids, shares=None):
    return QuarterPlan(tenant_id=TENANT, year=2026, quarter=1, trip_ids=trip_ids,
                       destination_shares=shares or {}, approved=True, approved_by="tenant:test")


class TestAllEightChannelsDoNotCrash:
    """The test STEP0/the build task explicitly required: a tenant configured with every
    channel Bang 2 + backward-compat "blog" names must not raise a Pydantic ValidationError."""

    @pytest.mark.parametrize("channel", _ALL_CHANNELS)
    def test_single_channel_produces_valid_slots(self, channel):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, [channel], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        assert len(grid.slots) > 0, f"channel={channel!r} produced no slots at all"
        for s in grid.slots:
            assert s.channel == channel

    def test_all_eight_channels_together_in_one_config(self):
        """A tenant who has configured every channel at once — the realistic worst case for a
        round-robin channel assignment across 8 values instead of 4."""
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, _ALL_CHANNELS, 8, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 40)}, "US")
        assert len(grid.slots) > 0
        seen_channels = {s.channel for s in grid.slots}
        # Not asserting all 8 appear (round-robin + atom-pool exhaustion can drop some slots,
        # same "dropped, not repeated" behavior test_aa301_allocator.py already covers) — only
        # that every channel that DOES appear is a real, valid Channel value, i.e. nothing
        # crashed constructing any of them.
        assert seen_channels.issubset(set(_ALL_CHANNELS))


class TestOriginalFourChannelsUnchanged:
    """Regression: the 4 channels that worked before AA-449 must keep the exact same n_atoms /
    framework behavior — this feature must not change existing tenants' output."""

    def test_facebook_still_gets_exactly_one_atom_per_slot(self):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["facebook"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) == 1

    def test_tiktok_still_gets_exactly_one_atom_per_slot(self):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["tiktok"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) == 1

    def test_blog_still_gets_up_to_four_atoms_per_slot(self):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) <= 4

    def test_email_still_gets_up_to_four_atoms_per_slot(self):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["email"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) <= 4

    def test_blog_framework_unchanged_by_funnel_stage(self):
        """FRAMEWORK_TABLE's existing (stage, "blog") entries must be untouched by the 4 new
        ("ANY", channel) entries added alongside them."""
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        # BOFU stage everywhere (see _full_runway default) -> framework should be "AIDA"
        runway = _full_runway(stage="BOFU")
        grid = compute_slot_grid(TENANT, 2026, 2, ["blog"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        non_hold = [s for s in grid.slots if s.kind != "reactive_hold"]
        assert non_hold, "expected at least one non-reactive_hold slot"
        assert all(s.framework == "AIDA" for s in non_hold)


class TestNewChannelsGetSensibleFrameworks:
    """The 4 new channels must not silently fall back to the generic 'hub' default — confirms
    the FRAMEWORK_TABLE entries added in constants.py are actually being read."""

    @pytest.mark.parametrize("channel,expected_framework", [
        ("linkedin", "insight_led"),
        ("instagram", "hook_sensory_cta"),
        ("landing_page", "AIDA"),
        ("ads", "hook_benefit_cta"),
    ])
    def test_new_channel_framework(self, channel, expected_framework):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, [channel], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        non_hold = [s for s in grid.slots if s.kind != "reactive_hold"]
        assert non_hold, f"expected at least one non-reactive_hold slot for channel={channel!r}"
        assert all(s.framework == expected_framework for s in non_hold)

    @pytest.mark.parametrize("channel", ["linkedin", "instagram", "ads"])
    def test_single_focus_channels_get_one_atom(self, channel):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, [channel], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) == 1

    def test_landing_page_gets_up_to_four_atoms(self):
        t = _trip()
        plan = _approved_plan([t.id], {"Ha Giang": 1.0})
        runway = _full_runway()
        grid = compute_slot_grid(TENANT, 2026, 2, ["landing_page"], 4, plan, runway,
                                 {t.id: t}, {t.id: _atoms(t.id, 16)}, "US")
        for s in grid.slots:
            if s.kind != "reactive_hold":
                assert len(s.atom_ids) <= 4


def test_channel_literal_has_exactly_eight_values():
    """Pins the exact value set — a future edit that adds/removes a channel here should also
    touch api/routers/admin.py::_VALID_CHANNELS and frontend/app/admin/tenants/page.tsx::
    ALL_CHANNELS (this test doesn't check those files, just documents the expected set)."""
    import typing
    assert set(typing.get_args(Channel)) == set(_ALL_CHANNELS)

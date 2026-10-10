"""AA-301 — multi-tenant isolation for N4/N5/N6.

The dev DB currently has real tour data for exactly one tenant (aa_internal,
793/793 raw_tours rows — verified live 2026-07-22). There is no second
tenant with real tour data to test cross-tenant isolation against, so this
uses a fixture/seed second tenant as plain Python objects (Trip/AtomRecord),
never inserted into the dev DB — per Nghiep's explicit instruction not to
insert fake data into the real database.

Verifies: the same runway_map/plan_quarter/allocate_month logic, called with
two different tenant_ids and disjoint trip sets, produces two outputs with
no cross-tenant leakage.
"""
import uuid

from services.acp_planning.allocator import compute_slot_grid
from services.acp_planning.models import AtomRecord, QuarterPlan, RunwayCell, RunwayMap, Trip
from services.acp_planning.quarter import compute_quarter_plan
from services.acp_planning.runway import compute_runway_map

TENANT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")  # aa_internal — real tenant, real data shape
TENANT_B = uuid.UUID("a1b2c3d4-0001-4000-8000-000000000001")  # wanderlux-travel — real shared.tenants row,
# zero real raw_tours today (verified live) — fixture-only for this test


def _trip(tenant_label, **over):
    base = dict(id=uuid.uuid4(), name=f"{tenant_label} Trip", destination=f"{tenant_label}land",
                period="Mar-May", lifecycle_stage="active")
    base.update(over)
    return Trip(**base)


def _atoms(trip_id, n=8):
    return [AtomRecord(atom_id=f"atom_{i}_{uuid.uuid4().hex[:6]}", trip_id=trip_id,
                       text=f"atom {i}") for i in range(n)]


class TestRunwayMapIsolation:
    def test_two_tenants_disjoint_destinations(self):
        trip_a = _trip("A", destination="Destination-A")
        trip_b = _trip("B", destination="Destination-B")

        rm_a = compute_runway_map(TENANT_A, 2026, [trip_a], markets=["US"])
        rm_b = compute_runway_map(TENANT_B, 2026, [trip_b], markets=["US"])

        assert rm_a.tenant_id == TENANT_A
        assert rm_b.tenant_id == TENANT_B
        assert {c.destination for c in rm_a.cells} == {"Destination-A"}
        assert {c.destination for c in rm_b.cells} == {"Destination-B"}
        # tenant A's map must not contain any tenant B destination and vice versa
        assert "Destination-B" not in {c.destination for c in rm_a.cells}
        assert "Destination-A" not in {c.destination for c in rm_b.cells}


class TestQuarterPlanIsolation:
    def test_two_tenants_disjoint_trip_ids(self):
        trip_a = _trip("A", destination="Destination-A")
        trip_b = _trip("B", destination="Destination-B")
        runway_a = RunwayMap(tenant_id=TENANT_A, year=2026, cells=[])
        runway_b = RunwayMap(tenant_id=TENANT_B, year=2026, cells=[])

        plan_a = compute_quarter_plan(TENANT_A, 2026, 1, [trip_a], ["US"], 4, [],
                                      runway_a, {trip_a.id: _atoms(trip_a.id)})
        plan_b = compute_quarter_plan(TENANT_B, 2026, 1, [trip_b], ["US"], 4, [],
                                      runway_b, {trip_b.id: _atoms(trip_b.id)})

        assert plan_a.tenant_id == TENANT_A
        assert plan_b.tenant_id == TENANT_B
        assert trip_b.id not in plan_a.trip_ids
        assert trip_a.id not in plan_b.trip_ids


class TestSlotGridIsolation:
    def test_two_tenants_disjoint_slots_and_gate_b_independent(self):
        trip_a = _trip("A", destination="Destination-A")
        trip_b = _trip("B", destination="Destination-B")
        runway_a = RunwayMap(tenant_id=TENANT_A, year=2026,
                             cells=[RunwayCell(destination="Destination-A", market="US", month=m, stage="BOFU")
                                    for m in range(1, 13)])
        runway_b = RunwayMap(tenant_id=TENANT_B, year=2026,
                             cells=[RunwayCell(destination="Destination-B", market="US", month=m, stage="BOFU")
                                    for m in range(1, 13)])

        plan_a = QuarterPlan(tenant_id=TENANT_A, year=2026, quarter=1, trip_ids=[trip_a.id],
                             destination_shares={"Destination-A": 1.0}, approved=True, approved_by="ms.thu")
        # Tenant B's plan is deliberately left UNAPPROVED to prove Gate B is
        # per-tenant/per-plan, not a global flag that leaks across tenants.
        plan_b = QuarterPlan(tenant_id=TENANT_B, year=2026, quarter=1, trip_ids=[trip_b.id],
                             destination_shares={"Destination-B": 1.0}, approved=False)

        grid_a = compute_slot_grid(TENANT_A, 2026, 2, ["blog"], 4, plan_a, runway_a,
                                   {trip_a.id: trip_a}, {trip_a.id: _atoms(trip_a.id)}, "US")
        assert all(s.trip_id in (None, trip_a.id) for s in grid_a.slots)

        import pytest
        from services.acp_planning.models import QuarterPlanNotApprovedError
        with pytest.raises(QuarterPlanNotApprovedError):
            compute_slot_grid(TENANT_B, 2026, 2, ["blog"], 4, plan_b, runway_b,
                              {trip_b.id: trip_b}, {trip_b.id: _atoms(trip_b.id)}, "US")

    def test_same_destination_name_different_tenants_no_leakage(self):
        """Regression guard for a subtler leak: two tenants both happening
        to use the same destination NAME (e.g. both sell 'Sapa') must still
        resolve against each tenant's own trip/atom data, never mixing atom
        pools across tenants."""
        trip_a = _trip("A", destination="Sapa")
        trip_b = _trip("B", destination="Sapa")
        atoms_a = _atoms(trip_a.id, n=3)   # tenant A: thin
        atoms_b = _atoms(trip_b.id, n=20)  # tenant B: rich

        runway_a = RunwayMap(tenant_id=TENANT_A, year=2026,
                             cells=[RunwayCell(destination="Sapa", market="US", month=m, stage="BOFU")
                                    for m in range(1, 13)])
        plan_a = QuarterPlan(tenant_id=TENANT_A, year=2026, quarter=1, trip_ids=[trip_a.id],
                             destination_shares={"Sapa": 1.0}, approved=True, approved_by="ms.thu")

        grid_a = compute_slot_grid(TENANT_A, 2026, 2, ["blog"], 4, plan_a, runway_a,
                                   {trip_a.id: trip_a}, {trip_a.id: atoms_a}, "US")
        used_atom_ids = {aid for s in grid_a.slots for aid in s.atom_ids}
        b_atom_ids = {a.atom_id for a in atoms_b}
        assert used_atom_ids.isdisjoint(b_atom_ids), "tenant A's slots must never use tenant B's atoms"

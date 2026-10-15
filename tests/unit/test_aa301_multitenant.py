"""AA-301 — multi-tenant isolation for N4/N5 (the N6 allocator was deleted in AA-840).

The dev DB currently has real tour data for exactly one tenant (aa_internal,
793/793 raw_tours rows — verified live 2026-07-22). There is no second
tenant with real tour data to test cross-tenant isolation against, so this
uses a fixture/seed second tenant as plain Python objects (Trip/AtomRecord),
never inserted into the dev DB — per Nghiep's explicit instruction not to
insert fake data into the real database.

Verifies: the same runway_map/plan_quarter logic, called with
two different tenant_ids and disjoint trip sets, produces two outputs with
no cross-tenant leakage.
"""
import uuid

from services.acp_planning.models import AtomRecord, RunwayMap, Trip
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

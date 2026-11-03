# AA-1027 E5 — KPI registry copy and the TripPlanner funnel report (lane l3-e4e5-tp, S265)

## Decisions
- `services/reports/registry/kpis.yaml` is the byte copy of the root `docs/reports/kpis.yaml` (root PR of the same
  lane): K-LLM-APP and K-UNIT-TRIP read TripPlanner's spend from `shared.llm_call_daily` (app `tripplanner`) instead
  of `tripplanner.llm_spend_daily`, which tp-025 drops. The root report-registry lint checks every KPI source against
  `db-schema-reference.md`, so without this the DB Schema Dump PR after the tp-025 apply would fail the root checks.
- `services/reports/planner.py` stops listing the counter under "Not readable from CIS".

## Changed
- `services/reports/registry/kpis.yaml`, `services/reports/planner.py`.

## Tradeoffs
- KPI versions stay 1: the meaning (TripPlanner LLM spend) is unchanged, only the source table.

## Should know
- Merge with (or right after) the root PR, before the tp-025 apply. `catalog.yaml` already differs between root
  `master` and this repo's `main` on one channel line (another lane's change in flight); not touched here.

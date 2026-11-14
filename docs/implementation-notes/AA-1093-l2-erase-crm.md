# AA-1093 stage 2 (S281 lane l2-erase-crm) — CIS side: schema learn in the tenant erasure, the CRM stage checked

Plan: root `docs/audits/2026-11-13-S279-session/aa1093-plan.md` (Part 2). Stage 1 of the saga ran for real in S280;
stage 2 is the CRM step (AA-Partner `crm_tenant_erase`, its own PR in this lane). This PR closes the gap the S280 run
showed in CIS's own plan: schema `learn` (CIS 420, AA-1105) was not part of the erasure inventory.

## Decisions

- **`learn` joins `CIS_SCHEMAS`** (`services/erasure/inventory.py`), so the catalog guard (a tenant-keyed table
  without an inventory entry blocks the apply and fails CI) watches it from now on, and the eight learn tables with a
  `tenant_id` are `delete` entries: `learn.learner` (partner learners carry the tenant; a staff learner has none and
  stays) and its seven children (each carries the tenant too, so each is its own node, children first). The saga's
  CIS plan hash and the tenant backup follow from the inventory, nothing else changes.
- **`learn.statement` is append-only** (CIS 420 trigger: a DELETE only with `learn.retention_purge = 'on'` in the
  transaction). `inventory.DELETE_SETTINGS` names that setting; `plan.apply_tenant` sets it with
  `set_config(..., true)` (this transaction only) before the deletes when the plan deletes from that table. A plain
  DELETE outside the apply stays refused (integration test).
- **The CRM stage needs no CIS code change**: the saga, the feed and the reports route already carry stage 2
  (`tests/integration/test_aa1093_erasure_saga_db.py::test_drill_stage_one_cis_last_then_stage_two_crm` drives a
  request to `done` through the CRM's reports). The S280 request stayed in round 2 only because no CRM consumer
  existed. The tenant checklist line for the CRM now says the saga's stage 2 does it (no hand step).

## Changed

- The lane brief said "deleting admin_users only nulls learner.admin_user_id": that is a staff learner (no tenant),
  which a tenant erasure never touches; the tenant's own learners are the `kind = partner` rows, now erased.

## Tradeoffs

- No new setting or threshold.

## Should know

- No migration in this PR. Tests: `tests/unit/test_aa1093_learn_erasure.py` (learn entries, the purge flag before the
  deletes), `tests/integration/test_aa1093_learn_erasure_db.py`.

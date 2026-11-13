# AA-1093 lane L2 (S279 l14) — CIS tenant offboarding erasure saga

Plan: root `docs/audits/2026-11-13-S279-session/aa1093-plan.md` (Part 2, lane L2), decisions D1, D2, D5-D8.
Contract: root `docs/contracts/tenant.erasure.v1.openapi.yaml` 1.0 (root PR #747, lane l11), byte copy in
`docs/contracts/`. Migrations CIS 408 (expand: tables), 409 (contract: the feed event type and exact grants), 410
(contract: `catalog.v_tenant_erasure_v1`) ship first in their own PR (MIG-4); this code PR needs 408 and 409.

## Decisions

- **Follow the merged contract, not the first plan.** The lane started from the plan (one combined approval at the
  latest `not_before`); the contract that landed meanwhile defines two stages (stage 1 CIS + Booking + TripPlanner at
  T0 + 30 days, stage 2 the CRM alone at T0 + 90 days), the step status (waiting / active / done / failed), the
  ErasureReport shape (report_id, status ok / blocked / refused / failed, error_code, tables, blockers, census) and the
  409 codes. The tables, the view and the routes use those names. One approval per stage (round), as the contract
  says; the DPA keeps the 30-day promise for the three apps of stage 1.
- **Stages are fixed (`saga.STAGES`)**, not derived from the window lengths: a later stage never starts before the
  earlier one is done. A drill (window 0 everywhere) therefore still rehearses both stages.
- **Internal phase list = the contract's** (no `scheduled`): before the first `not_before` a request sits in `dry_run`
  with no active app (`round_apps` empty).
- **CIS plan hash** = the contract's plan-hash form over the `data_erasure` DRY RUN rows, bound to tenant and round,
  **without `acp_shared.audit_log` and `shared.job`**: the saga itself writes those for the tenant between the DRY RUN
  and the apply (its approval audit row, the tenant backup job), so with them the approval could never match. The
  `data_erasure` apply still binds the full fingerprint of its own fresh DRY RUN.
- **CIS applies through the existing jobs**, not new SQL: `tenant_backup` (cheap, wired before every CIS apply),
  then a fresh `data_erasure` DRY RUN, then the `data_erasure` apply (all its guards stay: `ERASURE_APPLY_ENABLED`,
  the retention window, the recount inside its transaction, single use). The approver is the Cognito admin of the
  combined approval; the snapshot id is typed at the approval (D7: one RDS snapshot).
- **Saga job payload = `{request_id}` only**: a payload with the tenant id would count as a live job of the tenant
  (the erasure plan blocks on those) and the CIS apply would delete the row it runs from.
- **Step rows have no tenant column**, so the CIS erasure plan does not count them; the request table is registered
  as `keep` in `services/erasure/inventory.py` (it is the erasure record).
- **Report views read as JSON** (`to_jsonb(v)`), so an optional or new column of a Booking / TripPlanner view never
  breaks the read; a missing view, schema or grant is `app_not_ready` (a blocker on the request, never a skip).
- **The pilot is never scheduled** at the offboard (the route reports `skipped`); a request whose tenant became the
  pilot fails at the next pass. Dev: approve / apply only when `is_synthetic` (copied at T0).
- **Restore** cancels the open request until any app reported an ok apply (or one is done); afterwards it answers 409.
- **Event** `account.erasure_requested` is emitted from code through `shared.tenant_account_emit()` under a
  savepoint: before 409 the CHECK refuses the type and the offboard still succeeds (no event).

## Changed (vs the plan / the lane brief)

- Admin routes live under `/admin/job-runner/erasure/requests` (not `/admin/tenants/...`): the BFF rule
  `job-runner/erasure` (admin only) already covers them and `/admin/tenants/{tenant_id}` would shadow a literal path.
- Added `POST .../requests/{id}/new-round` (after an expired approval, plan_changed or a failed step) and
  `POST .../requests/{id}/advance` (Run now: queue the job or pull the queued one forward).
- The contract copies in this repo move to `tenant.entitlements.v1` 1.7 and add `tenant.erasure.v1` 1.0 (pins updated).

## Tradeoffs

- Thresholds (tech lead S279: settings, not literals): the settings registry reader is not on main yet (only CIS 407's
  tables). The windows reuse the existing CIS knob `ERASURE_TENANT_RETENTION_DAYS` (stage 1) and the approval
  validity reuses `ERASURE_DRY_RUN_MAX_AGE_HOURS`; the CRM window is the D2 value in `policy.CRM_WINDOW` (no new env
  var on purpose); the apply switch is the env flag `TENANT_ERASURE_SAGA_APPLY` (default off). **Follow-up:** settings
  `cis.erasure.crm_window_days`, `cis.erasure.stage1_window_days`, `cis.erasure.approval_valid_hours`,
  `cis.erasure.saga_apply_enabled` in the registry once its reader is on main, and read them in `policy.py`.
- Polling: a waiting pass reschedules itself (`Reschedule`, 30 s while a CIS job runs, 10 min while another app has
  not reported). After the registry's reschedule cap the job fails and the ops sampler queues a new one.
- The CIS census is the `data_erasure` verification (rows left per table), not a `pii:` comment census.

## Should know

- Apply order on Dev: CIS 408 (expand) and 409 / 410 (contract) before this PR's deploy (readiness raised to 408 / 409).
- A drill never applies in this lane. Applying a drill on Dev also needs `ERASURE_APPLY_ENABLED=true`,
  `TENANT_ERASURE_SAGA_APPLY=on` and `ERASURE_TENANT_RETENTION_DAYS=0` (the `data_erasure` job's own window check).
- Until the Booking (bk-057), TripPlanner (tp-030) and CRM lanes ship, their steps show `app_not_ready` / `pending`:
  expected, and the request never reaches `awaiting_approval`.
- AA-CIS-Infra must add the scope `cis.tenant/erasure` to the M2M resource server and the CRM client
  (`identity_m2m.tf`) before the CRM can call the routes (403 until then).
- `tests/integration/rls_catalogue_rules.py` changed in the migration PR: the Infra copy must follow (Copy Guard).

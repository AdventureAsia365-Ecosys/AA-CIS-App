# AA-839 — erasure DRY RUN job (`data_erasure`) for a traveller and an offboarded tenant

Second slice of AA-839 (the first, the offboard wording, is `AA-839.md`, CIS #918). Separate file so the two
slices never edit the same notes file (lessons: parallel PRs on one notes file go DIRTY).

## Decisions
- **One job kind, `data_erasure`**, ADR 0003a template (`shared.job`), registered in `services/jobs/erasure_job.py`;
  the plan logic lives in `services/erasure/` (inventory + FK planner) so it is unit-testable without a worker.
  Background kind, concurrency 1, `max_attempts=1`: a destructive kind never retries on its own.
- **DRY RUN is the default and the only mode enabled.** The apply mode is in the code and tested on synthetic
  rows, but it runs only when all of these hold: env `ERASURE_APPLY_ENABLED=true` (unset everywhere, so OFF), a
  succeeded DRY RUN of the same subject at most `ERASURE_DRY_RUN_MAX_AGE_HOURS` (24) old whose `fingerprint` the
  caller passes as `approved_fingerprint` (the counts Nghiệp approved, destructive-ops step 1), `approved_by`,
  `snapshot_id` (the manual RDS snapshot, step 2), a recount inside the transaction that gives the same
  fingerprint, no blocker, and (tenant) offboarded at least `ERASURE_TENANT_RETENTION_DAYS` days ago. The API
  also needs a verified Cognito staff session for an apply (the actor goes into the audit row).
- **Tenant inventory = explicit registry + catalog check.** Every CIS base table with a `tenant_id` column must
  be in `services/erasure/inventory.py` with an action (`delete`, `detach`, `merge`, `keep`). The job reads the
  live catalog (`pg_attribute`) and lists any table that has `tenant_id` but no registry entry under
  `unregistered`; that is a blocker for apply, and an integration test fails CI on the replayed migrations.
  So a new tenant table cannot be silently missed.
- **Rows reached only through foreign keys are found from `pg_constraint` at run time** (e.g.
  `quarter_plan_version` via `plan_id`, `content_piece_label` via `piece_id`, atoms/routes of a tenant's own raw
  tours). They are `cascade` rows: counted in the DRY RUN, deleted children-first in the apply. A referencing row
  that belongs to ANOTHER tenant, or sits in a kept/detached table, or in a schema CIS does not own, is a
  `blocker` (counted), never deleted.
- **Actions per tenant table.** Content, config, brand, T2 versions, integrations, plans, publish log, tenant
  notifications, the tenant's terminal `shared.job` rows (`payload->>'tenant_id'`) and finally `shared.tenants`
  itself: `delete`. Cost/decision logs that the platform still needs as totals (`llm_call_log`, `decision_log`,
  `dfs_call_log`): `detach` (tenant_id and the soft keys to deleted rows set to NULL). `llm_call_daily`: `merge`
  into the NULL-tenant row of the same key (its unique key is NULLS NOT DISTINCT, so a plain detach could
  collide). `tenant_api_usage` (+ monthly): `delete`, because a CHECK forbids a tenant-actor row without a
  tenant. `acp_shared.audit_log`: `keep` (append-only hash chain; AA-839 scope 2 keeps the audit row).
- **Traveller = sha256 of the trimmed, lowercased e-mail only** (the runbook's hash). The API refuses anything
  that is not 64 hex characters, so the address never reaches a payload, a log or a job row. CIS holds no
  traveller data by design (DM-6.1 tags: only staff/alert-recipient e-mails in `shared`), so the scan is a proof
  of absence over the places where outside text could land: exact hash on the tagged e-mail columns, and every
  e-mail-shaped token in the free-text/jsonb columns hashed and compared. Matches in staff/alert tables or the
  append-only audit log are `manual` (not a traveller erasure); notifications are `delete`, job payload/result/
  error are `redact`.
- **Cross-app data is a checklist, not a query.** `booking.*`, `tripplanner.*` and `crm.*` are other owners'
  schemas (`cis_app` has no grant on them; DM-6.8 forbids it for `crm`), so every result carries the per-app
  checklist with the runbook section that does each step.
- **S3 and Secrets Manager**: the DRY RUN lists what exists for the tenant (bronze `raw-inbox/<tenant>/`,
  `brand-identity/<tenant>/`, secrets `acp/cms/<tenant>`, `acp/social/<tenant>`) best-effort; an AWS error is
  recorded, not fatal. Deleting them is an operator step in the runbook (the worker role has no delete rights
  there, and granting them is an Infra/IAM decision).

- **Text tenant ids** (`acp_shared.audit_log`, `acp_output_rules`, `acp_deliver.tenant_tour_pages` are
  varchar/text) compare as `tenant_id::text = $1::text`; uuid columns as `= $1::uuid`. Mixing an untyped `$1`
  with both made asyncpg fail with "inconsistent types deduced for parameter $1" (seen in the first test run).
- **Legacy tables absent on Dev** but still created by the CI migration replay (`acp_deliver.packets`, `pieces`,
  `acp_shared.acp_v2_runs`, `acp_v2_slots`, `competitor_index_cache`, `content_metric_snapshot`, `unknown_ledger`,
  `acp_silver_s2.competitor_inputs`) are registered as `delete` ("absent on Dev"): on Dev they are listed under
  `missing_tables`, in CI they are planned like any other. Checked against the live dump of 22/10: the registry
  covers every Dev table with `tenant_id`, none unregistered.

## Changed
- New: `services/erasure/{__init__,inventory,plan}.py`, `services/jobs/erasure_job.py` (kind `data_erasure`,
  registered in `services/jobs/__init__.py`), routes `GET /admin/job-runner/erasure` (`tenant.read`) and
  `POST /admin/job-runner/erasure/run` (`tenant.write`) in `api/routers/admin_job_runner.py`, POLICY entries in
  `api/core/staff_authz.py`, the route-policy snapshot, `AdminTenantAuditAction.ERASED = "tenant.erased"`.
- The job result of a `data_erasure` job is redacted (counts only, no hash/ids/approver) for callers without
  `tenant.read`, through the existing AA-816 M1 `_restricted_results()` hook.
- `tests/unit/test_aa737_worker_throughput.py`: the background-kind set gains `data_erasure`.
- `CONTEXT.md`: job-runner section (data erasure) and tenant lifecycle (erasure is the `data_erasure` job).
- No migration, no Terraform, no frontend change. The Jobs page already lists any kind.

## Tradeoffs
- **Generic FK traversal vs a hand-written delete list.** A hand list is easier to read but rots as migrations add
  FKs; the planner reads `pg_constraint` every run, so a new child table is covered (or blocks) without a code
  change. The cost is nested `IN (SELECT ...)` predicates (up to 4 levels on Dev: raw_tours -> tour_atoms ->
  atom_segment_member); fine for one tenant's rows, and the DRY RUN shows the counts before anything runs.
- **Detach vs delete for cost logs.** Deleting `llm_call_log` / `decision_log` / `dfs_call_log` rows of a tenant
  would change platform cost history; they hold no personal data, so the tenant link (and soft keys to deleted
  rows) is nulled instead. `tenant_api_usage` cannot be detached (CHECK), so it is deleted.
- **S3 / Secrets deletion stays an operator step.** The worker role can list but not delete those objects;
  giving it delete rights is an IAM change for one rare operation. The DRY RUN lists exactly what exists, and the
  runbook has the single-line commands.
- **One fingerprint over all counts** instead of a `max_rows` like retention: an erasure is all-or-nothing for one
  subject, so the approval names the exact per-table counts; any drift refuses the apply.
- **The traveller hash stays in the job payload until the apply** (or until `retention_job` deletes the
  succeeded row after 30 days, for a DRY RUN never applied). The apply strips it from both job rows; callers
  without `tenant.read` never see it.

## Should know
- **Apply is OFF everywhere**: nothing sets `ERASURE_APPLY_ENABLED`. Turning it on for a Dev drill is a Terraform
  env change on the api + worker task definitions (AA-CIS-Infra), and needs Nghiệp's approval
  (destructive-ops.md). `ERASURE_TENANT_RETENTION_DAYS` defaults to 30 (a business value Nghiệp has not set);
  set it to 0 on Dev for the drill.
- A DRY RUN after an apply is the independent verification: every `delete`/`detach`/`merge` count is 0, only the
  kept audit rows remain.
- `shared.job` rows of the tenant are matched by `payload->>'tenant_id'` (the tenant job kinds' convention, as
  offboard uses); `data_erasure` jobs carry `tenant`, never `tenant_id`, and are excluded by kind as well.
- Tests: `tests/unit/test_aa839_erasure.py` (45, pure + routes), `tests/integration/test_aa839_erasure_job.py`
  (7, real Postgres: inventory completeness on the replayed schema + every generated SQL runs, tenant DRY RUN /
  refusals / apply / cross-tenant blocker, traveller DRY RUN + apply).

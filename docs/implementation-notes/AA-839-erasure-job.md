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
- **The traveller hash stays in a succeeded DRY RUN's payload until the apply** (or until `retention_job`
  deletes the succeeded row after 30 days, for a DRY RUN never applied — the open request itself). The apply
  strips it from every `data_erasure` row holding it; a failed/cancelled traveller job strips its own
  (`on_terminal`), so the 90-day failed-job archive never carries it; a `verify: true` DRY RUN strips its own when
  done. Callers without `tenant.read` never see it.

## Should know
- **Apply is OFF everywhere**: nothing sets `ERASURE_APPLY_ENABLED`. Turning it on for a Dev drill is a Terraform
  env change on the api + worker task definitions (AA-CIS-Infra), and needs Nghiệp's approval
  (destructive-ops.md). `ERASURE_TENANT_RETENTION_DAYS` defaults to 30 (a business value Nghiệp has not set);
  set it to 0 on Dev for the drill.
- A DRY RUN after an apply is the independent verification: every `delete`/`detach`/`merge` count is 0, only the
  kept audit rows remain.
- `shared.job` rows of the tenant are matched by `payload->>'tenant_id'` (the tenant job kinds' convention, as
  offboard uses); `data_erasure` jobs carry `tenant`, never `tenant_id`, and are excluded by kind as well.
- Tests: `tests/unit/test_aa839_erasure.py` (61, pure + routes + Sentry), `tests/integration/test_aa839_erasure_job.py`
  (11, real Postgres, handler as cis_app: inventory completeness on the replayed schema + every generated SQL runs, tenant DRY RUN /
  refusals / apply / cross-tenant blocker, traveller DRY RUN + apply).

## Review round (S248, independent review)
Decisions taken on the eight findings (details and evidence in the lane handoff):
- **Tenant keys beyond `tenant_id`.** Registered `acp_contract.tour_atoms` (owner_scope = tenant, OR a tour of
  the tenant — one node, so the platform atoms of a tenant raw tour are not a false cross-tenant blocker),
  `shared.pipeline_jobs` (`tenant` = the id as text, OR the slug), `acp_contract.atomize_day_fingerprint`
  (version id IN the tenant's T2 versions OR its generated_content rows). New `TenantTable.where_any`: predicate
  branches with the tables each reads; a branch whose table is absent is dropped (CI lacks ttv), the tables read
  become soft parents in the delete order (the fingerprint rows go before the versions their predicate needs).
  The catalog guard now counts `tenant_id`, `tenant`, `owner_scope` and any "tenant" column no FK covers.
  Evidence code-side: `pipeline_jobs.tenant` was written from `payload.get("tenant_id")` (removed writer,
  commit 60819bcd / fe7e3530); the slug branch is a safety net. Dev counts are a pre-merge probe (orchestrator).
- **Retry.** `JobKind.retry_refusal` (generic, asked by the retry route, 409); the erasure kind refuses an apply.
  Belt and braces in the job: an apply claims its DRY RUN atomically (`result.used_by_apply`) before writing,
  so neither a retried apply (route bypassed) nor a second apply can reuse the approval.
- **Self FKs.** A `self_reference` check per self FK on a deleted node, whatever the action. Outside-row checks
  (cross_tenant, self_reference) use `NOT coalesce(pred, false)`: a child job without `payload.tenant_id` has a
  NULL predicate and was invisible to a plain `NOT pred` (caught by the integration test).
- **Savepoints.** Each guarded check and each S3-ref statement runs in `conn.transaction()` (a savepoint inside
  the DRY RUN's read-only transaction and inside the apply's write transaction).
- **Hash leaving the store.** Sentry: `before_send` drops every frame's locals for events tagged
  `job_kind=data_erasure` or touching an erasure module (targeted, not a global `include_local_variables=False`:
  the other kinds keep their debugging value). Plus `on_terminal`, strip-all at apply, `verify` DRY RUN. Refusals
  still raise (a failed job is the honest status); the scrub makes that safe.
- **Locked re-check.** `_apply` locks the tenant row `FOR UPDATE` and re-runs `tenant_refusals` before the
  recount.
- **S3 copies.** `raw_sources.s3_path` added; the DRY RUN lists keys outside the tenant prefixes (normalising
  `s3://bucket/`), more than 200 per column or an unreadable column blocks. Checklist + runbook: those keys, and
  the AA-825 retention archives (scan or a recorded keep-until-expiry decision).
- **Tests as cis_app.** The integration module applies CIS 244 in the Dev order (like
  `test_x1_cis_app_privileges`), builds the live `tenant_tour_versions` (FKs: tenant, published tour, self, job;
  RLS + cis_app policy) and `review_queue.tenant_tour_version_id` for its own tests and drops them after; the
  handler runs on a pool logged in AS `cis_app`.

## Fingerprint binds the subject (S248, follow-up review finding)

Finding: the DRY RUN fingerprint hashed only the subject kind and the (table, action, rows) list, so two tenants
(or two travellers) with identical counts shared one fingerprint, and an approval recorded as "fingerprint X" did
not say which subject it approved.

### Decisions
- **Fingerprint v2** (`plan.FINGERPRINT_VERSION = 2`): sha256 of `{"v": 2, "subject", "subject_key", "rows"}`.
  `subject_key` is the tenant id for a tenant, and for a traveller `traveller_key = sha256(salt + ":" + hash)`
  with a random 128-bit salt drawn by each traveller DRY RUN and kept in that DRY RUN's `result.subject_salt`.
  A tenant DRY RUN draws no salt (the tenant id is not secret, so its fingerprint needs none).
- **Why a salt for the traveller, not the bare hash**: after an apply the hash is stripped from every erasure
  job row, but the fingerprint stays in the job rows and in the `dsar.cis_erased` audit row, next to the per-table
  counts. An unsalted `sha256(hash + counts)` would let anyone with a guessed address recompute the hash and test
  it against the kept fingerprint, undoing the strip. `_strip_hash` now removes `result.subject_salt` together with
  the hash, so the kept fingerprint can no longer be matched. A verification DRY RUN (`verify: true`) never stores
  its salt (it is never applied and strips its own hash).
- **The apply reuses its DRY RUN's salt** (read from the DRY RUN job) and recomputes the fingerprint with the
  subject key from its own payload. Before the claim only the stored DRY RUN fingerprint is compared with
  `approved_fingerprint` (`apply_refusals`). The recomputed fingerprint is compared inside `_apply`, after the
  claim: once on the pre-transaction recount and again on the recount inside the write transaction. A count drift
  therefore burns the DRY RUN (fail-closed): it is claimed, nothing is written, and a new DRY RUN and approval are
  needed, which matches "one DRY RUN serves one apply". The existing subject check stays: `apply_refusals` still refuses a DRY RUN whose payload subject / tenant / hash differs from the
  apply's ("the DRY RUN was for another subject").
- **Old DRY RUN jobs** (v1, no `fingerprint_version`) fail the apply check with "the DRY RUN predates the
  subject-bound fingerprint; run a new DRY RUN and approve it". They expire after
  `ERASURE_DRY_RUN_MAX_AGE_HOURS` (24 h) anyway, and no apply has ever run (the flag is off everywhere). A traveller
  DRY RUN without a salt is refused the same way.

### Changed
- `services/erasure/plan.py`: `FINGERPRINT_VERSION`, `traveller_key()`, `fingerprint(subject, subject_key, rows)`
  (raises on an empty key).
- `services/jobs/erasure_job.py`: `subject_key()`, salt drawn per DRY RUN, `fingerprint_version` +
  `subject_salt` in the DRY RUN result, the apply reads the DRY RUN's salt, `_apply` recount uses the key,
  `apply_refusals` refuses v1 / salt-less DRY RUNs, `_strip_hash` drops the salt.
- Tests: unit (same counts / other tenant → other fingerprint; traveller bound to hash and salt; apply with
  another subject's fingerprint refused, incl. the case v1 accepted; v1 DRY RUN refused; salt-less traveller DRY
  RUN refused; strip drops the salt; mutation "no subject key" turns 3 tests red); integration
  `test_same_counts_for_two_tenants_give_two_fingerprints` and the salt check in `test_traveller_dry_run_then_apply`.

### Tradeoffs
- Considered requiring an `approved_tenant_id` field instead: it would change the API and the admin page (#960)
  for the same guarantee. Binding inside the fingerprint keeps the API and the page unchanged (they only copy the
  64-hex value).
- Considered the DRY RUN job id as the binding: it stays in the rows after the strip, so it would not protect the
  traveller case.

### Review round 2 (PR #961)
- Tests: the verification DRY RUN returns no `subject_salt` and its stored row holds none; no row whose hash was
  replaced by a `stripped*` marker keeps a salt (mutation: storing the salt on a verification run turns
  `test_traveller_dry_run_then_apply` red); a v1 DRY RUN is refused before the claim, so its row has no
  `used_by_apply` (`test_a_v1_dry_run_is_refused_and_not_claimed`).
- `_strip_hash` removes `subject_salt` only from an object result (`CASE WHEN jsonb_typeof(result) = 'object'`)
  and rewrites only object payloads: `jsonb - key` raises on a scalar, and a raise there would leave the hash in
  place (`test_strip_hash_leaves_a_non_object_result_alone`, red on the bare `result - 'subject_salt'`).
- The salt is drawn only for a traveller DRY RUN; tenant fingerprints are unchanged
  (`test_same_counts_for_two_tenants_give_two_fingerprints` fails if a tenant DRY RUN draws one).

### Should know
- The admin page (#960) only checks the fingerprint is 64 hex; it opens the apply panel for a v1 DRY RUN and the
  backend refuses it with the message above. Run a new DRY RUN after deploying this change.
- `subject_salt` is visible in the full job view (tenant.read callers) while the hash is also there; it is not in
  the redacted view.

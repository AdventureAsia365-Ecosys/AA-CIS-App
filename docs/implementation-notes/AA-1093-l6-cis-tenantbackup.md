# AA-1093 part 3 (lane l6-cis-tenantbackup): per-tenant CIS backup and restore drill

Gap M27 of the S278 system audit: CIS had only whole-instance PITR, which restores all four apps at once. This slice
copies the CRM model (`crm_pilot_backup` + `app.portability.restore_drill`) for CIS, with no migration. Plan: root
`docs/audits/2026-11-13-S279-session/aa1093-plan.md` (Part 3, "Restore drill", decision D11).

## Decisions

- **Table catalogue = the erasure plan.** `services/tenant_backup/catalogue.py` runs `services/erasure/plan.plan_tenant`
  over the inventory and the live catalog, and exports every node through the erasure predicate of the tenant. A new
  tenant table must get an inventory entry anyway (the `unregistered` guard of `test_aa839_erasure_job` fails CI),
  and then it is backed up with no change here. An unregistered table still reaches the manifest and makes the backup
  `complete: false`.
- **Tenant rows only, never the AA catalogue.** Every table is read with the erasure predicate (`$1` = the tenant):
  its own raw tours, the atoms it owns (`owner_scope`) or that sit on its own tours, its published rows, the FK
  children of those. The AA internal tenant (it owns the master catalogue) is refused by the job and the drill; PITR
  restores it.
- **Left out, each with a reason in the manifest:** the ledgers the erasure detaches or merges (`llm_call_log`,
  `decision_log`, `dfs_call_log`, `geocode_call`, `llm_call_daily`, `ai_usage_hourly`: cost history that stays on the
  platform), `acp_shared.audit_log` (append-only hash chain, never erased, cannot be re-inserted), `shared.job` and
  `shared.pipeline_jobs` (job history; the backup job is itself a row), `shared.llm_cassette` (raw model replies kept
  for replays), `shared.tenant_api_usage` (per-request metering; the monthly rollup is backed up). A cascade table
  reached only through left-out tables is left out too (`child_of_excluded`).
- **Format.** `tables/<schema>.<table>.jsonl.gz`: one `to_jsonb(row)` per line (Postgres writes the JSON, so every
  type comes back through `jsonb_populate_recordset`), gzip with mtime 0. `manifest.json`: format
  `cis-tenant-backup/1`, tenant, backup id, schema version (highest numeric `shared.schema_versions`), per table rows,
  JSON bytes, gzip bytes, sha256, columns, primary key; skipped / excluded / unregistered / missing; restore order
  (parents first = the erasure delete order reversed). `SHA256SUMS` (sha256sum format) over the table files and the
  manifest, written last; its own digest is in the job result, which the drill checks first.
- **Backup id = the job id.** A retry writes the same keys again (the bucket is versioned); the drill finds a backup by
  its job row (`kind = 'tenant_backup'`, `status = 'succeeded'`, `payload.tenant_id`).
- **One snapshot.** All tables are read in one repeatable-read, read-only transaction, so parents and children agree.
- **Size cap** (`TENANT_BACKUP_MAX_BYTES`, default 512 MiB of uncompressed JSON): tables go smallest first (row count),
  a table that would cross the cap is dropped whole (never cut) and listed under `skipped` with its row count. Raw
  tours and atoms of a tenant are its own rows only, so they are exported like the rest unless the cap stops them.
- **Bucket and retention.** Gold bucket `exports/` prefix: its lifecycle rule `exports-expire` deletes objects after
  7 days (D11), SSE-S3 default and asked for on every put. Name: `GOLD_BUCKET`, else `aa-cis-gold-<caller account>`
  (public repo: no account ID in code; same pattern as `shared/aws_client/bronze.py`). The CIS task role already has
  `s3:PutObject` / `GetObject` / `ListBucket` on `*` (AA-CIS-Infra `modules/ecs/main.tf`), so no IAM change.
- **Schedule (D11).** Nightly for the pilot tenant only (`is_pilot`, not offboarded), from 20:00 UTC, idempotency key
  `tenant_backup:pilot:<day>`, queued by the ops metrics sampler. Default OFF: `TENANT_BACKUP_PILOT=on` on the api
  task turns it on (an Infra env change, the tech lead's call).
- **Drill never writes a live table.** Postgres refuses `CREATE TEMP TABLE` in a read-only transaction, so the drill
  transaction is a normal one that is **always rolled back**: its statements are reads of live tables and writes to
  its own `pg_temp` tables (`CREATE TEMP TABLE rd_<n> ON COMMIT DROP AS SELECT * FROM <live> WITH NO DATA`: the live
  column types without constraints). The one audit row (`tenant.restore_drill`, counts only) is written afterwards
  on its own. The integration test fingerprints every live row of the tenant before and after.
- **Drill comparisons.** Per table: manifest rows, restored rows, live rows now (the same predicate), restored primary
  keys found live, restored rows identical to the live row (`to_jsonb` equal). Pass = every digest matches and
  restored = manifest everywhere; live numbers are reported (`live_differs`), not judged, as in the CRM drill.
- **No admin route in this slice.** On demand runs through `python -m services.tenant_backup.enqueue` (ECS exec), like
  the CRM drill; a Tenant 360 button is a follow-up (it would also change the OpenAPI snapshot and RBAC).

## Changed

- New: `services/tenant_backup/` (`catalogue.py`, `export.py`, `store.py`, `restore_drill.py`, `enqueue.py`),
  `services/jobs/tenant_backup_job.py` (kind `tenant_backup`, background, concurrency 1, max_attempts 2).
- `services/jobs/__init__.py` registers the kind; `api/core/ops_metrics_sampler.py` adds `_tenant_backup_part`.
- `shared/llm_client/capacity.py`: `KIND_LINES["tenant_backup"] = None` (the kind makes no LLM call; the
  capacity guard requires a line for every registered kind).
- `services/acp_shared/audit_log.py`: `AdminTenantAuditAction.RESTORE_DRILL = "tenant.restore_drill"` (staff-only, not in
  the tenant activity allowlist).
- Tests: `tests/unit/test_aa1093_tenant_backup.py`, `tests/integration/test_aa1093_tenant_backup.py`; the background
  kind list of `test_aa737_worker_throughput.py`; the staff-only action list of `test_aa761_tenant_account.py`.
- `CONTEXT.md`: job kind list and a paragraph under the erasure.
- `services/erasure/inventory.py`: one `TENANT_CHECKLIST` item, the backup prefix in the gold bucket (an erasure
  inside 7 days of a backup removes it by hand; after that the bucket rule already has).

## Tradeoffs

- **Plain JSONL in the bucket, not an encrypted ZIP like the CRM.** CIS has no per-tenant data keys; the bucket's SSE,
  its private access block and TLS-only policy seal it. CIS tenant data is content and account settings (billing
  contact columns on `shared.tenants`), not traveller records.
- **No migration.** The backup is found through `shared.job` (its result holds the prefix and digests) instead of a
  backup table. Cost: the drill needs the job row, which the job retention keeps 30 days, longer than the 7-day object.
- **Live counts include rows the tenant's insert triggers write** (subscription, entitlements, feed events); on an
  idle tenant they match, on a working one `live_differs` says which tables moved.

## Should know

- **Versioned gold bucket:** the `exports-expire` rule has no `noncurrent_version_expiration`, so after 7 days an
  expired backup becomes a noncurrent version that S3 keeps (the same holds today for the content export files). The
  "kept 7 days" of D11 needs the companion AA-CIS-Infra change (noncurrent versions expire after 1 day) in this wave.
- The schedule stays off until `TENANT_BACKUP_PILOT=on` is set on the api task (AA-CIS-Infra `modules/ecs`).
- The drill runs in the API container (it has the cis_app DSN and reads the bucket); the job runs on the worker.
- The integration test reads the drill's audit row by key (`tenant_id` + `resource_id` = backup id), not by
  `ORDER BY created_at` (the SQL order stability guard refuses a timestamp-only order in DB tests).
- Root runbook section text (for the restore drill runbook, written by the root lane) is in the lane's return notes.

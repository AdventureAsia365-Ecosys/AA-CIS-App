# AA-859 — D5b and wave 3 (S247)

Plan: root `docs/audits/2026-10-16-S240-aa859/aa859_plan.md` (PR-D5 "D5b", "Follow-up waves / Wave 3"). Wave 4 is
out of scope. Two commits on `feat/aa859-d5b-wave3`: D5b (no migration), then wave 3 (migration 262 + routes).

## D5b — the T2 detail reads (commit 1)

Flag: `T2_VERSION_DETAIL_READS_FLAG` = `aa859:t2_version_detail_reads_pool`. No migration.

### Decisions
- **Own flag, not `T2_VERSION_READS_FLAG`.** The lane brief said "same flag as W5a reads, so they switch at
  deploy time". PR-D5a's review decided the opposite and pinned it with
  `test_t2_version_reads_flag_is_used_only_by_the_three_d5a_handlers`: W5a's flag is verified and ON, so a route
  added under it moves with no flip of its own, and reverting D5b would revert W5a too. Under S241 D-12 a new
  key that is missing in Redis means ON, so the D5b routes still switch at deploy time with their own flag —
  the brief's intent holds, and the rollback stays per route group.
- **One log event for the three routes**, separate from W5a's: `aa859_t2_version_detail_read route=full|tour|
  export_docx used_tenant_pool=... found=...` (after the block, also on a 404). A separate event name keeps the
  W5a and D5b Logs Insights checks apart.
- **`/full` staff branch reads no flag.** With `X-Admin-Secret` the handler goes straight to the admin pool
  (plan D2, staff are outside the tenant boundary): no Redis read, no tenant-pool acquire, no log line. Only the
  tenant-JWT branch is on the tenant pool. The two branches are now separate code paths; the staff SQL text is
  unchanged.
- **export-docx audit on MAIN after the block** (`# AA-859 MAIN exception`), best-effort as before.
- SQL lifted to module constants (`_TOUR_VIA_VERSION_SQL` + `_TOUR_VIA_VERSION_TENANT_COND`, `_TOUR_RAW_SQL`,
  `_TOUR_SEO_SQL`, `_EXPORT_VERSION_SQL` + `_EXPORT_VERSION_TENANT_COND`) so tests can forget the tenant
  condition; a unit test pins the flag-off text to the pre-D5b inline SQL (whitespace-normalised). `/{id}` and
  the `/full` tenant branch ran the same SQL text before; they share one constant now.
- The local `safe()` helper of `/full` became the module function `_safe_row()` (same body).

### Changed
- `api/core/aa544_tenant_pool.py`: new constant + `ALL_TENANT_POOL_FLAGS`; header list.
- `api/routers/v1_tours.py`: the three handlers (above).
- `tests/unit/test_s241_tenant_flag_keys.py` (12 keys), `tests/unit/test_aa817_tenant_pool_fail_closed.py`
  (inventory size 12).
- New: `tests/unit/test_aa859_t2_version_detail_reads.py` (fail-closed matrix x 3 routes, 404 path, staff branch,
  audit ordering, flag-off SQL, flag ownership), `tests/unit/test_aa859_reentrancy_d5b.py`,
  `tests/integration/test_aa859_t2_version_detail_reads_pool.py` (tenant pool = admin pool for A, forgotten-WHERE
  for B on `/{id}`, `/full`, export; it imports D5a's stand-in DDL and fakes instead of copying them).
- `CONTEXT.md` route table: D5b row; W5a's "still on MAIN" note removed.

### Tradeoffs
- Two branches in `/full` duplicate the raw/seo reads (one per pool) instead of one shared block with an
  `if is_admin` inside: the staff path must not read the flag at all, and a shared block would.

### Should know
- **Same known divergence as W5a** (239 policies): on the tenant pool a master tour shows `raw` / `seo` only when
  those rows carry the AA master tenant id. Pre-flip probe (read-only, as cis_app through ECS exec), both must be
  0, else do not keep the flag ON:
  `SELECT count(*) FROM gold_aa_internal.published_tours pt JOIN silver_aa_internal.raw_tours rt ON rt.tour_id =
  pt.tour_id WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid AND rt.tenant_id IS DISTINCT FROM
  pt.tenant_id;` and the same with `silver_aa_internal.seo_context sc ON sc.tour_id = pt.tour_id`.
- Ships ON (missing key). To ship it dark, `SET aa859:t2_version_detail_reads_pool false` before the deploy (the
  flag tool's ALLOWED set needs the key: `.tmp-session/aa859_wave3_flag.py` carries it).
- Flip-verify: `.tmp-session/aa859_wave3_flip_verify.py t2_version_detail_reads` (not run by this lane).

## Wave 3 — brand identity + publish (commit 2)

Migration **262** (`api/migrations/262_tenant_brand_seo_rls.sql`; the plan's "242", renumbered: CIS is at 261).
Flags: `BRAND_IDENTITY_FLAG` = `aa859:brand_identity_pool` (needs 262), `PUBLISH_FLAG` = `aa859:publish_pool`
(no new grant).

### Decisions
- **One migration file, not two.** 262 covers both tables (same template, same route); 263 was not needed.
- **Publish needs no migration.** Everything it touches already has RLS and the grants it uses (publish_log
  S/I/U 229, content_piece / angle_gate_request 229, angle_gate_option S 241, tenant_integrations S 237). The
  plan's D7 row "PUBLISH_FLAG — 242" is not true of the code; the flag is independent of 262.
- **262 grants:** `tenant_brand_rules` SELECT + INSERT on the 16 columns the POST writes + UPDATE (is_active);
  `tenant_seo_config` SELECT (the upsert's ON CONFLICT (tenant_id) needs SELECT on tenant_id) + INSERT
  (tenant_id, target_market) + UPDATE (target_market, updated_at). tenant_id is never updatable, no DELETE, no
  TRUNCATE. The file starts from `REVOKE ALL` so a re-run gives the exact set (a guard checks it).
- **X1 parity policy in the same file** (`<table>_cis_app_all` TO cis_app USING (true) WITH CHECK (true)): the api
  main pool, the worker (T2 / T8 / T9 read the brand rows; T2 / T7 read target_market) and ECS-exec scripts are
  cis_app since X1 S18, not the owner; without it they would read 0 rows after the apply, silently. CI standing
  check `test_standing_every_rls_table_has_a_cis_app_parity_policy` requires it.
- **No behavioural aa_app_user guard in the file.** 237 / 241 switched to aa_app_user inside the migration; since
  X1 the file runs as cis_owner (login db_migrator), the role lint allows `SET LOCAL ROLE` only to catalog_owner /
  cis_owner / cis_app, and cis_owner may not join aa_app_user (no role DDL in cis/). Shape guards instead (policy
  set and shape, exact privileges, the invariant), behaviour in CI (`test_aa817_rls_tenant_isolation`,
  `test_aa859_migration_262`, `test_aa859_wave3_pool_routes`) and on Dev by the nightly isolation probe (its tenant
  matrix discovers every RLS table, so the two tables are covered with no probe change).
- **Guard 6 (cis_app parity by behaviour) only the DB Migrate way:** inside a DO block that runs when current_user is
  cis_owner and the login is a member of cis_app; `SET LOCAL ROLE cis_app` and back with `SET LOCAL ROLE cis_owner`
  (both allowlisted). In the CI replay (superuser, no 244 grants) it is skipped with a NOTICE (pinned by a test).
  The file passes the Infra role lint (`run_migration.lint` / `role_lint`, run locally against the file).
- **Guard 1 "other grantees":** refuses PUBLIC and any role that can log in or has members (it would read 0 rows),
  besides the owner, aa_app_user and cis_app. A NOLOGIN role with no members is accepted: CI's legacy `app_user`
  (005) holds grants on these tables and can open no session. Also refuses a dependent view owned by a role RLS
  would filter.
- **Brand identity: staff vs tenant.** A second dependency `_brand_caller_is_tenant` (True when a Bearer was
  presented; `_resolve_brand_tenant_id` already refuses any non-tenant Bearer). Handlers check `is True`, so direct
  calls that pass only `tenant_id` (existing tests, other code) get the Depends default and stay on the staff /
  main-pool path; the request signature of the route is unchanged.
- **Brand POST on the tenant pool:** acquire_scoped_conn's transaction is the save's transaction (no nested one);
  advisory lock `pg_advisory_xact_lock(859, hashtext(<tenant> || ':brand_identity'))` before MAX + 1 (same class as
  W6, different key text); a duplicate version (UNIQUE (tenant_id, brand_name, version), e.g. a concurrent staff
  save on MAIN) is 409 `version_conflict`, never a 500. Flag off: the admin pool inside its own transaction, as
  before (no lock, the old error behaviour).
- **Publish blocks:** (1) piece + connection in one block (no external call between them; the 404 / 422 / 409
  checks raise inside it), (2) `claim()` (its transaction becomes a savepoint, so a concurrent claim's
  UniqueViolation rolls back only the claim and `claim()` still answers `in_progress`), (3) `finish()`. The
  daily-limit override (`shared.tenants`) is read on MAIN **before** block 2 (it used to share the claim's
  connection; same SQL, same order). Audit on MAIN after block 3. `channel_publish` now carries `used_tenant_pool`.

### Changed
- `api/migrations/262_tenant_brand_seo_rls.sql` (new).
- `api/core/aa544_tenant_pool.py`: two constants + inventory; header list.
- `api/routers/admin_pipeline.py`: `_brand_caller_is_tenant`, SQL constants (`_BRAND_*`), `_read_brand_rows`,
  `_save_brand_rows`; GET / POST split into tenant (tenant pool) and staff (main pool) paths.
- `api/routers/v1_publish.py`: `_PUBLISH_PIECE_SQL` / `_PUBLISH_PIECE_TENANT_COND` / `_PUBLISH_INTEGRATION_SQL`,
  `_publishable_channel()` (the old inline checks, unchanged), three scoped blocks.
- `tests/integration/conftest.py`: CI's legacy `app_user` is a LOGIN role (ci.yml creates it) holding 005's grants on
  tenant_brand_rules, which 262's guard 1 refuses; the replay revokes them right before 262 and again after the
  post-replay `GRANT ... TO app_user` (same as tenant_integrations for 237). Dev has no such grant.
- `tests/integration/conftest.py`: `X1_POST_244_RLS` — `x1_244_ci_shape` takes RLS and the cis_app policy off the
  two tables around the X1 tests' 244 apply (244's exact RLS-set guard and the "16 tables" test know only the
  244-time set; on Dev 244 ran before 262), `x1_244_ci_unshape` puts both back.
- `tests/integration/test_aa817_rls_tenant_isolation.py`: the two tables in `TENANT_TABLES` and
  `COLUMN_WRITE_TABLES`, seeded per tenant, 262 re-applied in the fixture, `ALL_RLS_TABLES` 18, the delete test
  parametrised over a new `DELETE_TABLES` (only tenant_integrations has DELETE), new tests: the brand POST
  statements as the tenant role, writes stay in the own tenant, columns outside the grant refused.
- New: `tests/integration/test_aa859_migration_262.py`, `tests/integration/test_aa859_wave3_pool_routes.py`,
  `tests/unit/test_aa859_brand_identity_pool.py`, `tests/unit/test_aa859_publish_pool.py`,
  `tests/unit/test_aa859_reentrancy_wave3.py`.
- `tests/unit/test_aa819_daily_publish_limit.py`: the publish-route fake sets an explicit OFF flag (S241 D-12).
- `tests/unit/test_s241_tenant_flag_keys.py` (14 keys), `tests/unit/test_aa817_tenant_pool_fail_closed.py` (14).
- `CONTEXT.md`: two route rows, MAIN exceptions, 262 rollback note.

### Tradeoffs
- Column grants on the brand INSERT (16 columns) make a later new brand column a 42501 on the tenant path until a
  migration grants it. Accepted: same rule as 241 for tenant_integrations; the unit flag-ownership and the
  integration POST test catch it in CI.
- `REVOKE ALL` + re-grant on every run of 262 (instead of "GRANT if missing") so the guard can demand an exact set.
- Publish takes one extra MAIN acquire (the limit read moved out of the claim connection) — a few ms.

### Should know
- **Order on Dev (corrected after review).** "Apply 262 before the deploy" cannot happen with this branch: the Infra
  Copy Guard (`db/check_migration_copies.py`, flow D8) refuses the Infra copy of 262 until the file is on CIS
  `main`, and merging this PR (it touches `api/**`) auto-runs Deploy Dev. So the wave-3 code always deploys before
  262 can be applied, and `aa859:brand_identity_pool` is ON when its key is missing (S241 D-12): in that window the
  tenant Brand page GET / POST would 500 (42501, no aa_app_user grant). Chosen order (option b, one PR):
  1. Before merging the CIS PR: `aa859_wave3_flag.py off aa859:brand_identity_pool` (writes the explicit
     `"false"`; root `.tmp-session/aa859_wave3_flag.py`, S3-mediated ECS exec), then `get` to read it back.
  2. Merge the CIS PR → Deploy Dev (brand identity tenant branch on the admin pool, as before; publish and D5b ship
     ON, neither needs 262).
  3. Preflight `pre` (below) → `gate_262_ready` true.
  4. Infra PR with the byte-identical copy `db/migrations/cis/262_tenant_brand_seo_rls.sql` → Copy Guard green →
     merge → DB Migrate `check` → `apply`.
  5. Preflight `post` → `post_262_ok` true; nightly isolation probe (or a manual run) covers the two tables.
  6. Flip with `aa859_wave3_flag.py on aa859:brand_identity_pool`, then the flip-verify script.
  Alternative (option a): split out a migration-only CIS PR (262, `test_aa859_migration_262.py`, the conftest
  changes, the `test_aa817_rls_tenant_isolation.py` table changes), then Infra copy + apply, then the routes PR.
  Not taken: it re-cuts a reviewed commit, and option b gives the same safety with one CIS PR.
- Publish does not depend on 262 (its tables already carry RLS and the grants).
- **Read-only preflight before the apply:** root `.tmp-session/aa859_262_preflight.py` (S3-mediated ECS exec, runs as
  cis_app, read-only, `ssl="require"`). Expected: both tables RLS off, owner cis_owner, grantees cis_app only, no
  policy, no dependent view, invariant 0 rows. `job_busy` counts `shared.job` and `booking.job` (DB Migrate waits on
  every job table, DM-11.4); cis_app may not read `booking.*`, in which case the booking count is `null`,
  `booking_job_read_skipped` holds the error and the verdict lists it under `unchecked` (not as busy): DB Migrate's
  own guard is then the authority for booking.job. The preflight counts every queued / running job; DB Migrate
  exempts a few kinds, so the preflight can be stricter, never looser.
- **CI column patches in `test_aa859_wave3_pool_routes.py`** (review fix): the CI replay DB lacks
  `shared.tenant_brand_rules.created_at` (on Dev, added outside any tracked migration; 001 is skipped in CI) and the
  migration-124 columns of `acp_shared.content_piece` (pgvector, absent in CI). The fixture adds them inside its
  rolled-back transaction (same pattern as `test_aa859_piece_edit_pool.py` / `test_aa817_tenant_pool_routes.py`),
  so the file passes alone and in any order, not only after test_aa802 / test_aa815 commit their ALTERs. 262 grants
  SELECT on the table, so aa_app_user reads the added column.
- **Not changed here, on purpose:** `tests/integration/rls_catalogue_rules.py` still allowlists the two tables for
  rule a. After 262 that entry matches nothing (reported as stale, not fatal). Removing it changes a file the Infra
  Copy Guard compares byte for byte, so it is a follow-up once both repos are on 262 (edit here, copy to Infra
  `db/rls_catalogue_rules.py` in the same pair of PRs).
- Flip-verify script (not run): root `.tmp-session/aa859_wave3_flip_verify.py brand_identity publish
  t2_version_detail_reads`, flag tool `.tmp-session/aa859_wave3_flag.py` (the S240 tool with the three new keys).
  The publish check uses 1 of tenant A's 20 daily publishes and the E2E WordPress test site, like W4's.

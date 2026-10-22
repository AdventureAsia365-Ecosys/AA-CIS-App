# AA-859 wave 4 — atoms / subject / planning on the tenant pool

Branch `feat/aa859-wave4`. Plan: root `docs/audits/2026-10-16-S240-aa859/aa859_plan.md` (D4 Q3, "Wave 4", the
flag table, the risk table) and the S247 wave-4 planner's notes (C1–C15). Migrations **265** and **266** (264 is
AA-779). Builds on wave 3 (CIS #927, migration 262).

## Decisions

- **Route hub retired (Q3).** `/v1/routes`, `/v1/hubs`, `/v1/route-picks` had no caller in code and 0 requests in
  30 days of api logs; `/v1/routes` was broken anyway (a `(codes, unmatched)` tuple bound to `text[]`). Deleted the
  router, `create_route_pick` and its snapshot helper. `acp_contract.route_pick` stays (Dead Table Registry
  candidate; dropping it is destructive and needs the DRY RUN procedure). So wave 4 grants nothing on `hub`,
  `atom_ranking` or `route_pick`: the platform read allowlist is 7 relations, not the plan's 9.
- **Six flags, one per group** (`api/core/aa544_tenant_pool.py`): `aa859:angle_gate_read_pool`,
  `aa859:slate_reads_pool`, `aa859:subject_decide_pool`, `aa859:trip_reallocation_pool`, `aa859:marketplace_pool`,
  `aa859:reviews_pool`. ON from the first deploy (S241 D-12).
- **The choose / reopen re-read** runs under `ANGLE_GATE_READ_FLAG` in its own sequential block, not under
  `ANGLE_GATE_DECIDE_FLAG` (verified ON: a read added under it would move with no flip of its own, the D5b lesson).
- **Two migrations.** 265: the five planning / page tables, the platform allowlist, `USAGE` on `acp_contract`. 266:
  `tour_atoms` alone (worker-hot; ACCESS EXCLUSIVE on ~32k rows).
- **tour_atoms keep-alive:** `catalog_owner_read USING (true)` (the catalog views read as catalog_owner) and the X1
  parity policy for cis_app. **No tripplanner policy**: TripPlanner no longer reads the table and CIS 254 (AA-847)
  revokes its 180 column grant; 266's guard 1 refuses to run while tripplanner still holds any privilege.
- **Pick reads atoms on the base table** (`subject_seed._MEMBERS_SQL`, `route_guard._TRIP_MEMBER_SQL`) with the
  203 rule inlined (`JOIN published_tours ON tour_id, master_status = 'active', deleted_at IS NULL`), the 253 / 256
  precedent. Changed globally (same rows for cis_app; checked against the view on real Postgres).
- **Invariant v2** = v1 plus table-level `SELECT` on the 7 allowlist relations (`NO_PRIVILEGE_WITHOUT_RLS_SQL_V2`).
  v1 stays byte for byte (241 / 262 carry it). The allowlist matches on `nspname || '.' || relname`, not on
  `oid::regclass::text` as the planner sketched: the regclass text depends on `search_path`.
- **place_card is a column grant (review S247).** `GRANT SELECT (card_id, status)`, the columns of the one tenant-pool
  statement (`subject_seed._ALIAS_SQL`); `verified_by` (a staff user id), `cost_usd`, `naming_note` and `facts` stay
  unreadable. V2 allows exactly those two column grants (`PLATFORM_READ_COLUMNS` in `_rls_fixture_helpers.py`); the
  other six relations stay table-level.
- **choose / reopen re-read on the decide's connection (review S247).** With both `aa859:angle_gate_decide_pool` and
  `aa859:angle_gate_read_pool` on, the re-read runs on the decide's tenant connection inside its transaction
  (`tenant_flag_enabled`, the cached per-request decision): no second acquire that could queue (max 2, 5 s) and answer
  503 for a saved choice, after which the portal's retry gets 409. Mixed states keep the separate sequential block, so
  each flag still reverts on its own; taking choose / reopen fully off the tenant pool needs **both** keys false
  (CONTEXT.md, 265 STEP 0). 241's STEP 0 is not edited: 241 is applied and its Infra copy is byte-identical.
- **Lock waits are a 409 (review S247).** The tenant pool's `lock_timeout` (5 s): pick / cut → 409
  `subject_busy: …` (`aa859_subject_lock_timeout`), confirm → `PlanBusyError` → 409 `plan_busy: …`
  (`aa859_trip_reallocation_lock_timeout`, audit still written with `applied=false`). The confirm's version conflict
  409 detail is now a string (`version_conflict: …`), the shape of the other routes (it was a dict; the portal ignores
  the detail).
- **Unit tests:** one fail-closed matrix over the nine handlers (`tests/unit/test_aa859_wave4_pools.py`) instead of
  six near-identical files, plus `test_aa859_reentrancy_wave4.py` and `test_aa859_wave4_sql_shape.py`.

## Changed (vs. the S240 plan and today's behaviour)

- **Audit rows after commit, best-effort (flag on):** pick (`aa859_subject_pick_audit_failed`) and trip-reallocation
  confirm (`aa859_trip_reallocation_audit_failed`, for accept and reject, with `applied`). With the flag off both keep
  the pre-wave-4 contract (pick: audit inside the transaction, AA-559; confirm: audit first, then the pool
  functions).
- **Confirm accept = one transaction** (advisory lock per tenant / quarter, save + approve). Before: two transactions,
  a crash in between left an orphan pending version.
- **Two concurrent confirms serialize** on the advisory lock and both succeed (versions N and N + 1), they do not
  give one 409: under READ COMMITTED the second sees the first's version after the lock. The `UniqueViolation` →
  409 `version_conflict` mapping stays as a backstop (unit-tested). Deviation from the plan's "one success, one 409".
- **Pick locks the Subject row** (`FOR UPDATE`), the UPDATE carries `tenant_id` and `state = 'proposed'` guards; cut
  is one conditional UPDATE and a re-SELECT for 404 / 409. A concurrent t7_slate proposal waits and then sees
  `picked`.
- **The tenant trip query (C12)** takes its trip page through a LATERAL: the tenant's own page, else the AA master's,
  one row per tour, on both pools. Before, on the main pool, a tour repeated once per tenant with a page for it (and
  could show another tenant's URL). `fetch_tenant_trips_on` binds the tenant twice ($1 uuid, $2 text).
- **The angle-gate job status** (`shared.job`) is read only for `pending_goal` without a goal (the one state where
  `generation_failed` can be true): fewer main-pool reads from the wizard's 3 s poll. Same response.
- **Alias cards (C10):** inside a transaction the read runs in a savepoint, so the catch-all does not leave the pick's
  tenant transaction aborted.
- **Stale "never move to the RLS pool (AA-578)" notes** rewritten (`v1_marketplace.py`, `slate.py`,
  `tenant_pool.py`, `trip_reallocation.py`); the `v1_route_hub.py` one went with the file.
- **No `tripplanner_read` keep-alive policy on tour_atoms (deviation from the plan).** The plan kept TripPlanner
  reading through `tripplanner_read USING (true)`; instead 266 refuses to run while `tripplanner` holds any privilege
  on the table ("apply CIS 254 first"). Verified: TripPlanner `origin/main` (`0e9fdcf`) and Booking read neither
  `tour_atoms` nor `v_active_tour_atoms`. **CI dependency:** this PR's CI is red until CIS 254 (AA-847, `d3a1a2c7`) is
  on main (the replay grants 180 to tripplanner; 266 raises without 254).
- **265 guards, review S247:** guard 1's grantee check reads column ACLs too (a column-only grantee would silently read
  0 rows); guard 5 refuses any PUBLIC grant (table or column, any privilege) on an `acp_contract` relation without RLS
  (the schema is reachable by `aa_app_user` for the first time; 241 covered only already-reachable schemas and V2 only
  `aa_app_user`'s own grants) and any SECURITY DEFINER function in `acp_contract` the tenant role can execute (none on
  Dev). Both 265 and 266 now take their ACCESS EXCLUSIVE lock before the canonical-id / D9 / grantee checks.
- **Confirm save block:** `as (conn, used)` with its own transaction when `used` is false (defence in depth: the
  cached flag decision makes it match the read block), and an `aa859_trip_reallocation_save` line with
  `used_tenant_pool`.
- **Approved-plan joins** (`_APPROVED_PLAN_SQL`, `fetch_current_version_no`) also require `qpv.plan_id = qp.plan_id`:
  the tenant role may write `quarter_plan.current_version_id`, and FK checks bypass RLS.
- **RLS catalogue allowlist** (`tests/integration/rls_catalogue_rules.py`): removed the two stale rule-a entries
  (`shared.tenant_brand_rules`, `shared.tenant_seo_config`, stale since 262) and the four 265 covers
  (`acp_deliver.tenant_tour_pages`, `acp_shared.quarter_plan`, `year_plan`, `subject`); added rule-t
  `acp_contract.tour_atoms:catalog_owner_read`.

## Tradeoffs

- **Tenant pool head-of-line risk** (max 2 per task): the angle-gate poll (3 s), reviews (5 s) and Slate (15 s) move
  onto it; pick holds its connection for ~10 statements, no external call. Watch `aa859_tenant_pool_timing`.
- **RLS on tour_atoms for cis_app / catalog_owner** adds `USING (true)` policies: plans should not change, but run the
  EXPLAIN before / after (lesson S238). 266's guard counts the catalog views as catalog_owner under the ACCESS
  EXCLUSIVE lock (before and after, the DB Migrate way): a few seconds on Dev; the lock is the price of a consistent
  count.
- **seo_context relevance differs by path:** flag on, `fetch_dfs_relevance_by_tour_on` reads `seo_context` as the
  tenant role (the master's rows plus the tenant's own); flag off it reads every tenant's rows. The trip-reallocation
  suggestion can therefore differ between the two paths; the flag-on result is the intended one.
- **Review nits not taken:** a lasting `CHECK … NOT VALID` + `VALIDATE` keeping `tour_atoms.owner_scope` /
  `tenant_tour_pages.tenant_id` canonical (a non-canonical writer fails closed, hides rows, never leaks; a new
  constraint changes every writer's failure mode and belongs in its own reviewed change); the D9 count in the Infra
  probe / X2 guard (`segment_matching.py:904` reads `v_active_tour_atoms` with no `owner_scope` filter, so a future
  tenant atom on a live master tour would reach `atom_segment_member` and the catalog views): Infra follow-up, with the
  probe items below.
- **The allowlist removal** (four 265 tables) makes the Dev nightly dump report four rule-a findings between the Infra
  copy merge and the 265 apply (reported, not fatal; never "hard": no tenant-pool grant on them before 265).

## Should know

**Flags to set `false` on Dev before the deploy** (all six; the Copy Guard means the code deploys before 265 / 266 can
be applied):

```
aa859:angle_gate_read_pool   aa859:slate_reads_pool   aa859:subject_decide_pool
aa859:trip_reallocation_pool aa859:marketplace_pool   aa859:reviews_pool
```
Root tool: `.tmp-session/aa859_wave4_flag.py off <key>` then `get` (⚠️ contains underscore env vars, edit manually
in VSCode).

**Flag window (review S247):** if Redis restarts after the deploy and before 265 / 266 are applied, all six keys
vanish and read ON: the routes then fail with 500 (permission denied), not 503; the angle-gate GET (polled by the
wizard) is hit first. To survive a restart, also list the keys in the api env var `TENANT_POOL_FORCE_ADMIN_FLAGS`
until the migration each needs is applied (all six until 265; `aa859:subject_decide_pool`,
`aa859:trip_reallocation_pool`, `aa859:marketplace_pool`, `aa859:reviews_pool` until 266). Applying 265 before the
code deploy would also narrow the window (it is additive for today's main code), but the Copy Guard needs 265 on CIS
main first and a merge deploys.

**Rollout order**

1. Set the six keys `false`, read them back.
2. CIS 254 (AA-847, commit `d3a1a2c7`, branch `feat/aa-847-revoke-tp-raw-grants`): push, merge, Infra copy, apply.
   266 refuses to run before it, and **this PR's CI needs it on main** (the CI replay creates the tripplanner role
   and replays 180: without 254, 266 raises in the replay and every 266 test fails). Rebase this branch on main after
   254 merges. Note: with 254 on main, `test_aa847_migration_258.py::test_grants_policies_and_tripplanner_column_grant_unchanged`
   fails (it expects the 180 grant): the 254 PR must update it.
3. Merge this PR; deploy Dev **api and worker** (the member SQL constants are shared with t8 / t9); rollout
   COMPLETED; the route hub answers 404.
4. Preflight `pre-265`, Infra copy of 265 (and the rules file: byte copy), DB Migrate `check` → `apply` with
   `shared.job` / `booking.job` idle, preflight `post-265`, schema dump PR.
5. Flip `aa859:slate_reads_pool`, then `aa859:angle_gate_read_pool` (flip-verify each).
6. Preflight `pre-266` (D9 = 0, no tripplanner grant), apply 266, preflight `post-266` (catalog_owner and cis_app
   counts unchanged), TripPlanner browse smoke. **TripPlanner EXPLAIN before / after (lesson S238):** the preflight
   connects as `cis_app`, which has no USAGE on `catalog`, so it cannot run TripPlanner's reads. Run them as a catalog
   reader through DB Migrate `mode=run-script` (read-only) before and after the apply: a draft script (TripPlanner
   `origin/main` `coverage.py` `OPTIONS_SQL_CIS` and the `events.py` component read with real `= ANY($1)` id arrays,
   `SET ROLE tripplanner`, else `catalog_owner`; prints plan nodes / index names / ms only) is handed to the
   orchestrator for `AA-CIS-Infra/db/scripts/aa859_266_catalog_explain.py`. Compare the `SCRIPT plan` lines.
7. Flip `aa859:marketplace_pool`, `aa859:reviews_pool`, `aa859:trip_reallocation_pool`, `aa859:subject_decide_pool`
   (writes last). Live confirm only with `accept=false` (audit only); accept=true is covered by CI. A live pick is
   irreversible (the Subject stays `picked`): use the E2E tenant.
8. Merge the schema-dump bot PR, run the nightly isolation probe by hand.

**Rollback:** `SET <key> false` (never `DEL`), plus `TENANT_POOL_FORCE_ADMIN_FLAGS` if it must survive a Redis
restart; confirm with `used_tenant_pool=False`. Migration rollbacks are in the 265 / 266 headers (STEP 0 = flags).

**Infra follow-ups (not in this repo):**
- `db/rls_catalogue_rules.py` (byte-identical to CIS `origin/main` today): lines 98 (`shared.tenant_brand_rules`) and
  103 (`shared.tenant_seo_config`) can go now (262 is live). Lines 106 (`acp_deliver.tenant_tour_pages`), 111–113
  (`acp_shared.quarter_plan`, `year_plan`, `subject`) go only with the 265 apply (before it, the nightly guard reports
  rule-a violations); line 170 `"t": {}` → the `acp_contract.tour_atoms:catalog_owner_read` entry with 266 (before it,
  only a stale entry, not fatal). CIS 254 (`d3a1a2c7`) also edits this file: after this branch is rebased onto 254,
  copy the twin byte for byte from the CIS file then, not from this branch as it is now.
- `db/isolation_probe.py` (C15): probes only tables with a `tenant_id` column (`:369`), so `tour_atoms`
  (`owner_scope`) and `quarter_plan_version` (child) are not covered, and its master-constant regex
  (`UUID_CONST_RE`, `:397`) accepts only `'…'::uuid`, so `tenant_tour_pages`' `'…'::text` master read would read as
  cross-tenant.

**Main-pool shrink (out of scope):** the `aa859_main_pool_gauge` data so far (7 days, ~9k samples, `max(in_use)` 4,
p99 0, no real traffic, 60 s samples) cannot decide it, and the gauge cannot show queueing (main acquires have no
timeout). A decision needs, after every wave-4 flag is ON (moved routes still use MAIN for config, audit and job
status), at least 7 days or a deliberate burst test of per-task `max` / `pct(in_use, 99)` / minutes at `in_use =
max`, plus a main acquire-wait metric:
`filter @message like "aa859_main_pool_gauge" | parse @message /in_use=(?<u>\d+)/ | stats max(u), pct(u,99), count(*) by @logStream, bin(1h)`.

**Review fixes (S247), tests:** unit suite 10837 passed; flake8 clean on every touched file; integration on the
private PostgreSQL 16 scratch cluster (a `git archive` copy of HEAD plus 254 from `d3a1a2c7`; the worktree untouched):
265, 266, wave-4 pool routes (incl. the new pick-seed and trip-reallocation forgotten-WHERE tests), aa817 isolation,
241, 262, aa679 catalog views, catalogue guard, 228, 229, 239, 245, angle-gate decide, tenant-pool routes / content,
t7 / t8 jobs, route guard, segment collision, tenant360, wave 3: 219 + 132 passed, then the route file again with the
two new tests: 13 passed. The system Postgres on 5432 is not usable for this (its `cistest` is not a superuser).

**Tests run locally (first pass):** unit suite (all), flake8 on touched files; the integration suite on a private
PostgreSQL 16 cluster (CI runs 15) with 254 copied in temporarily: all wave-4 / 241 / 262 / 817 / catalog / X1 / catalogue-guard
tests green; `test_007_rls_isolation` errors locally only (password logins for `aa_cis_admin` / `aa_app_user`, set
up by CI only) and `test_aa847_migration_258` fails only because of the 254 copy (see step 2).

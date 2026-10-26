# Data coverage (AA-992) — CIS API + admin page

`GET /admin/data-coverage` and `/admin/data-coverage` (Platform › Overview), per the S252 design
(AA-Ecosys `docs/audits/2026-10-26-S252-data-coverage/design.md`, critic-fixed) and its critic review.
143 rows in 9 stages, 39 sources, counts only.

## Decisions

- **Registry, not page code** (`api/services/data_coverage/registry.py`): every row declares its sources, kind
  (`must` / `backlog` / `info`), polarity (`coverage` / `defect` / `threshold` / `equal`, critic B11), expected gap and
  drill link; the UI computes nothing. `equal` was added for "must equal" rows (`tp.tours`, `crm.cache_served`):
  missing = |denominator − value|.
- **Expected gap failing ≠ gap**: a row whose own numbers are fine but whose expected gap's source failed is
  `na: expected gap unavailable (…)` with value and missing still shown (critic B11, `seg.tours`).
- **Three cache tiers on `ops_overview.cache.cached_section`** (extended, not copied): new `key_prefix` and
  `single_flight` parameters give the DB tiers the lock + last-good behaviour of `needs_cache` while still computing
  without Redis. A tier is "ok" (full TTL, kept as last good) when every source answered or is a deterministic
  `unavailable` (e.g. `contract_view_missing`); an error / timeout caches it 60 s only.
- **Refresh throttle server-side too**: `?refresh=1` drops the fast tier only when its `computed_at` is ≥ 15 s old;
  the slow and CRM tiers keep their TTL. The UI disables Refresh for 15 s after a click.
- **CRM tier**: own key `admin:data-coverage:crm:v1`, computed only when the caller relays a Cognito token (a password
  session gets `na: cognito_session_required` and fills nothing, critic B12). `crm.cache_served` compares the CRM's
  served count with the CIS live count **as of the CRM's last full pass**: tours published after it are the expected
  gap while the pass is ≤ 120 min old (critic B12-3).
- **Catalog reads one statement per `catalog_connection`** (critic B9): the tenant pool has 2 connections per task
  and its gate lets one catalog read in at a time; the group lives in the slow tier only.
- **Pins resolved like TripPlanner** (AA-990): the pins and the id-map edges come from tp-020's views
  (`v_ops_pin_components`, `v_ops_component_id_map`; the design's one-hop `mapped_id` could not reproduce the
  map → redirect → map chains of the AA-990 backfill), walked in SQL with `geo.component_redirect`, map first,
  ≤ 5 hops, cycle-safe (`queries.TP_PIN_CHAIN`, the aa990 `chain_sql` shape).
- **Insert-failure drop reasons are exception text**: only their count leaves the database side
  (`in.drop.insert_failed`, must); the five A0 gate reasons are info rows.
- **Kept set of `master.writer_v2`** read from the `shared.rerun_wave` row `aa645-kept-on-v1`; until that row
  exists the row shows `na: expected gap unavailable (no_data_yet)`.
- **Critic small items fixed**: `master.active` expected gap = review queue only (can go red); `tp.pins_served` is
  info, `tp.pins_dangling` the must row (+ `tp.pins_not_served` info: kept by design, AA-986 L6); `rank.markets`
  denominator = markets with search demand (no hard-coded 6); T0 rows exclude `is_canary` tenants (`ten.canary` info
  row shows them); `IN_DROPS` filters `raw-inbox/` like `IN_BATCHES`; pins labelled "pins ever added". Not taken:
  `tp.purge_no_job` keeps expected 0 (if the 4 events of 23/10 predate the AA-987 job, the fix is a sweep, not a gap).
- **Admin only** (`OPS_READ`, rbac `["admin"]`, BFF `API_READ_RULES` "data-coverage"), as the design's D10.

## Changed

- New: `api/services/data_coverage/{__init__,defs,queries,registry,sources,build}.py`, `api/schemas/data_coverage.py`,
  `api/routers/admin_data_coverage.py`; `api/main.py` includes the router.
- `api/core/staff_authz.py`: `POLICY[("GET", "/admin/data-coverage")] = OPS_READ`; route-policy snapshot regenerated.
- `api/services/crm_console.py`: `("GET", "/data-coverage")` allowed, 5 s timeout (one CRM transaction per agency).
- `api/services/ops_overview/cache.py`: `key_prefix`, `single_flight`, `lock_ttl_s` (defaults unchanged).
- `tests/integration/rls_catalogue_rules.py`: rule `g` allow-list entries for the 5 new contract views (canonical
  copy; the AA-CIS-Infra copy must match byte for byte).
- `docs/api/openapi.json` regenerated (one new path + schemas).
- Frontend: `app/admin/data-coverage/page.tsx`, `lib/dataCoverage.ts` (+ `lib/dataCoverage.test.mjs`), nav entry in
  `app/_kit/adminNav.tsx` (Platform › Overview, second item), `lib/rbac.ts` page rule + BFF read rule.
- Tests: `tests/unit/test_aa992_data_coverage.py` (34), `tests/integration/test_aa992_data_coverage_sql.py` (every
  statement as `cis_app` / `aa_app_user` on the migrated DB, EXPLAIN guard on the log tables, the full build),
  `tests/e2e/admin-evidence/data-coverage.spec.ts` (prepared, agent-only, NOT run).
- `CONTEXT.md`: consumer paragraph + "Data coverage (AA-992)" block.

## Tradeoffs

- Base-table SQL (the S252 audit's) instead of `catalog.*` for the CIS rows: same numbers as the audit, no new grant;
  the TripPlanner rows read the views through the tenant pool ("measure what the consumer reads").
- The AA-991 probe still has its own definitions (design open question 10): `defs.py` is shaped to become the shared
  module, but the Infra copy + probe refactor is a separate change.
- The integration test grants `cis_app` SELECT on the CIS schemas inside its transaction (the CI replay skips 244)
  and adds live-shape stand-ins for what CI lacks (258 `source_key`, 274 views, 285 redirect, pgvector columns): it
  checks SQL shape, not the Dev grant set (the isolation probe does that).

## Should know

- Ships before tp-020 / bk-025 are applied: those rows read `n/a: unavailable: contract_view_missing` until DB Migrate.
- The CRM rows need AA-Partner's `crm.ops.v1` 1.3 deployed; before that the CRM answers 404 and the rows read
  `na: unavailable: crm_Not Found`.
- Local runs: unit tests with AWS disabled (no credentials, `AWS_CONFIG_FILE=/dev/null`); the first local run of
  `scripts/export_openapi.py` without that env tried the Sentry DSN secret and stopped at the MFA prompt (no call
  made). Integration on a throwaway PG 16 cluster: 80 passed, 1 skipped (`catalog.v_tour_component.embedding` needs
  pgvector, as in CI).
- `frontend/node_modules` is not in the worktree; `tsc --noEmit` and eslint ran against the main checkout's
  node_modules through a temporary symlink (removed): 0 errors, 3 className warnings shared with every admin page.

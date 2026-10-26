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
  without Redis. A tier is "ok" (full TTL, kept as last good) when every source answered or is a final
  `unavailable` (`contract_view_missing`, `cognito_session_required`, no tenant pool; narrowed in fix round 2, below);
  an error / timeout caches it 60 s only.
- **Refresh throttle server-side too**: `?refresh=1` drops the fast tier only when its `computed_at` is ≥ 15 s old;
  the slow and CRM tiers keep their TTL. The UI disables Refresh for 15 s after a click.
- **CRM tier**: own key `admin:data-coverage:crm:v1`, computed only when the caller relays a Cognito token (a password
  session gets `na: cognito_session_required` and fills nothing, critic B12). `crm.cache_served` compares the CRM's
  served count with the CIS live count **as of the CRM's last full pass**: tours published after it are the expected
  gap while the pass is ≤ 120 min old (critic B12-3).
- **Catalog reads one statement per `catalog_connection`** (critic B9): the tenant pool has 2 connections per task
  and its gate lets one catalog read in at a time; the group lives in the slow tier only. Fix round 2: the four catalog sources also run
  one after another (`Ctx.catalog_lock`), see below.
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
  denominator = ranking markets with search demand (no hard-coded 6; the PAA top-up market is left out, fix round 3); T0 rows exclude `is_canary` tenants (`ten.canary` info
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

## Fix round 2 (S252 review)

- **crm.ops.v1 minor 1.4, not 1.3**: AA-979 P2b shipped 1.3 (studio invitations) first; comments, the `crm` source's
  `reads` text and CONTEXT.md renamed.
- **Only final answers are `unavailable`** (`contract_view_missing`, `cognito_session_required`, `catalog_unavailable:
  tenant pool not configured`). `CatalogUnavailable` for a busy gate, a pool acquire error or a dropped connection is
  `error` (new `sources.Transient`), its statement_timeout (`QueryCanceledError`) and a CRM 504 are `timeout`, every
  other CRM non-200 (503 `crm_unavailable`, 502 `crm_bad_response`, 401 / 403 / 404 / 429) is `error`: the crm tier
  key is shared by every admin, so one caller's expired token or missing group must not blank the rows for 10
  minutes for the others. A tier with any of these is cached 60 s and never stored as the last good copy.
- **The four tenant-pool sources run one after another**: `Ctx.catalog_lock` (FIFO; tasks start in registry order:
  tp_layers, tp_components, tp_retired, tp_purge) admits one `catalog_connection` at a time, releasing the reader's
  gate in between, so none of them fails on the reader's 5 s gate wait because a sibling is slow. Each catalog
  statement also gets `SET LOCAL statement_timeout = '7s'` under the 8 s `wait_for`.
- **Tier deadline keeps finished sources**: `asyncio.wait(tasks, timeout=TIER_DEADLINE_S)`; only the pending tasks are
  cancelled and marked `timeout: time_budget` (design D7). No separate budget for the multi-step sources (`tp_pins`
  up to 3 × 5 s, `tp_purge` 8 s + 5 s): the 12 s tier deadline cuts them alone now.
- **New row `tp.pins_invalid_id`** (info, defect polarity): tp-020 now lists only UUID-shaped pinned ids and counts
  the rest as `pin_events_invalid_id`.

## Fix round 3 (S252 review r3, blocking)

- **`rank.markets` counts ranking markets only**: `acp_contract.search_demand` also holds the admin SERP top-up
  market (`services.acp_shared.real_questions.TOPUP_MARKET` = `US-PAA2`, AA-779), which is never ranked, so the row
  read 6 of 7 with a red Gap from the first load. `queries.RANK` now leaves it out of both market counts
  (`market <> $2`); `sources.rank` binds `TOPUP_MARKET` as `$2` (imported, not hard-coded in the SQL), so the
  denominator follows the constant if the top-up market is ever renamed. The numerator gets the same filter so it can
  never exceed the denominator.
- Tests: unit `test_rank_source_binds_the_paa_top_up_market_and_a_full_market_set_is_ok` (the bound argument, no
  literal in the SQL, 6 of 6 = ok); integration `test_rank_markets_count_only_ranking_markets_not_the_paa_top_up`
  (US + UK ranked, US / UK / US-PAA2 researched: 2 of 2, `ok`; the old denominator was 3).

## Should know

- Ships before tp-020 / bk-025 are applied: those rows read `n/a: unavailable: contract_view_missing` until DB Migrate.
- The CRM rows need AA-Partner's `crm.ops.v1` 1.4 deployed; before that the CRM answers 404 and the rows read
  `na: error: crm_Not Found` (re-asked every minute, never cached as final).
- Local runs: unit tests with AWS disabled (no credentials, `AWS_CONFIG_FILE=/dev/null`); the first local run of
  `scripts/export_openapi.py` without that env tried the Sentry DSN secret and stopped at the MFA prompt (no call
  made). Integration on a throwaway PG 16 cluster: 80 passed (81 after fix round 3), 1 skipped (`catalog.v_tour_component.embedding` needs
  pgvector, as in CI).
- `frontend/node_modules` is not in the worktree; `tsc --noEmit` and eslint ran against the main checkout's
  node_modules through a temporary symlink (removed): 0 errors, 3 className warnings shared with every admin page.

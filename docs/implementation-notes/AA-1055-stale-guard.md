# AA-1055 (PR 3, S274) — research stale share per market (data coverage + ops check) and a re-atomize research scope

Closes the last two Done-when items of AA-1055 (PR 1 read side and PR 2 buy side are merged and live):
1. "A re-atomize of one tour queues its new places for research (shown on Dev, no paid call until approved)."
2. "The guard reports the stale share per market live, and opens the issue above the threshold."

No migration, no new job kind, no DataForSEO / LLM / Jev call.

## What was built

**One measure, `services/acp_contract/research_coverage.py`** (read only, $0).
- Scope: `segment_research.scoped_rows` (split out of `_places_in_scope`, same SQL and exclusions with stored Jev picks
  only): the Segments of active platform Masters minus transit / unnamed places. The SQL now also returns
  `atoms_touched` (newest `coalesce(updated_at, created_at)` of the Segment's active atoms).
- Stale for market M: `segment_research.stale_tiers` (new, pure, now also used by `_plan`) for M alone. Volume tier per
  market (FRESH_FOR 365 d); the one SERP (SERP_FRESH_FOR) counts against the SERP market (US) only, as the run buys it.
- Markets: every `DFS_LOCATION_MAP` market at least one active tenant targets (`shared.tenants` is_active, not
  cancelled, not the AA master; `LEFT JOIN shared.tenant_seo_config`), each tenant resolved with
  `resolve_buyer_markets` like the Slate (no config / nothing usable = US). Never records an unmapped market.
- Output: per market `places_in_scope`, `places_stale`, `stale_share`, `above_limit` (share > 0.20), plus
  `waiting_after_reatomize` (below).
- `ReadOnlyPool`: every acquire is a READ ONLY transaction with statement and lock timeouts (optionally under a gate).

**Data coverage** (group A-series, source `research`, slow tier: 600 s cache).
- `research.stale_share.<mk>` for each DFS market: kind backlog, polarity defect, value = stale places, denominator =
  places in scope, expected gap = floor(20 % x scope), so the row is a gap exactly when the ops check fires. A market
  no active tenant targets is hidden (new optional `Row.shown_if`; a failed source keeps every row, n/a).
- `research.waiting_after_reatomize`: places atomized since their last research and stale in a targeted market.
- Drill: `/admin/seo-intelligence` (there is no research admin page; the research API is admin-only).
- Source budget: 4 s per statement, 1 s lock, the page's 3-connection gate, 12 s for the whole read (slow tier 20 s).

**Ops check `research_stale_share`** (`shared/ops_checks.py`): the api ops sampler measures once a day on its own
monotonic schedule (`research_coverage.CHECK_EVERY_S`, first on the first tick, like the AA-1056 sweep; part budget
30 s, 10 s per statement), field `research_stale` (or `research_stale_error`). `state_findings` raises ONE finding
listing every market above 20 %; bell once per 20 h, one e-mail per UTC day, page `/admin/data-coverage`.

**Re-atomize queues its places: research scope `reatomized`.**
- `segment_research.reatomized_places`: places whose atoms were written after their newest `segment_research_log` row
  (any market / tier), or never researched. `_places_in_scope(..., reatomized=True)` keeps only those;
  `run_segment_research(reatomized=)`, `ResearchScope.reatomized` (preview + run), job payload key `reatomized`.
- The plan then keeps only the ones stale in the requested markets, so a re-atomized place still fresh buys nothing.

## Decisions
- **No queue table (option D of the brief).** `segment_research_log.tier` has a CHECK (`volume` | `serp`, CIS 393) and
  its key is (place, market, tier), so a "queued" row needs a migration; a `shared.job` row that never auto-runs is
  ruled out (the job table must stay idle for deploys). The queue is a derived read: atomize time (atom
  `updated_at`, written by every atomize upsert) vs the place's newest research row. "Queued for an approved run" =
  the `reatomized` scope, priced by the preview, bought only by `/run`.
- **One finding for all markets**, not one check per market: the issue asks for one issue opened or updated; the bell
  payload lists the markets and their shares.
- **The ops alert is the issue.** The repo has no mechanism that opens or updates a tracking issue from an ops check
  (and the app must not write to GitHub / Linear). The bell (`shared.notifications`, at most once per 20 h while
  above) plus one e-mail a day on the alerts topic is the issue equivalent; it "clears" by not firing on the next daily
  measure.
- **A place the keyword rules would rebuild (`needs_requalify`) is not stale here.** The issue defines stale as "no
  research, or research older than FRESH_FOR"; the preview still selects those places. This also keeps the guard off
  the keyword map table (one read less).
- **The no-map widening of `_plan` is not needed for the share**: for one market it never changes whether a place is
  stale, only which markets a stale place buys.
- Tenant filter = the data coverage `ten` rule (is_active, not cancelled, not the AA master); canary tenants count.
- Threshold: 20 % stale (brief). `STALE_SHARE_LIMIT` is the one constant; the data coverage expected gap is
  floor(limit x scope), equivalent to share > limit for integer counts.

## Changed vs the issue / brief
- The "guard opens or updates one issue": an ops alert (bell + e-mail), see Decisions.
- The data coverage page shows one row per targeted market (static rows per DFS market, the untargeted ones hidden).
- `research_coverage.measure` reads the scope even when no tenant targets a market (the page then shows only the
  waiting row, 0).

## Tradeoffs
- Measured on every api start too (first tick), so a deploy re-measures; the bell dedupe (20 h) keeps it to one alert.
- `reatomized` compares against the newest research row in ANY market: a place researched for the US after its
  atomize but never for the UK is not in the reatomized scope, though it is stale for the UK (it is in the stale
  share and any market-wide run buys it). Kept simple: the scope answers "what did an atomize add since research".
- Every atomize upsert sets `updated_at`, also when a re-read day yields the same place: such a place enters the
  reatomized scope but is only selected when it is stale, so it never buys anything new.
- During a rolling deploy an OLD worker ignores the payload key `reatomized` (it filters to its own scope keys) and
  would run the wider scope, still bounded by `max_usd` and the balance check. Research is admin-triggered and the
  deploy waits for an idle job table, so the window is the length of a rollout.

## Should know
- No migration: SQL reads columns of the live schema reference (`v_active_tour_atoms.created_at / updated_at`,
  `segment_research_log` with `tier` from CIS 393, `shared.tenants`, `shared.tenant_seo_config`).
- `tests/integration/test_aa1055_stale_guard_sql.py` runs the new reads on the real schema (scope + `atoms_touched`,
  newest research row, the tenants' markets, `measure` through `ReadOnlyPool`); run locally on a temporary
  PostgreSQL 16 cluster with the full migration replay, together with the PR 2 SQL, data coverage, ops check and
  AA-1056 integration files (104 passed, 1 skipped). CI runs them on postgres:15.
- `docs/api/openapi.json` regenerated (pinned venv: fastapi 0.161.1, pydantic 2.14.1): one new request field.
- Live checks after deploy:
  - `GET /admin/data-coverage?refresh=1`: rows `research.stale_share.<mk>` for each targeted market (value, denominator,
    pct, status) and `research.waiting_after_reatomize`; source `research` status ok.
  - ECS log `ops_metrics_sampled` of the first api tick: field `research_stale` with `by_market`; above 20 % a
    `platform.ops_check.research_stale_share` bell.
  - Re-atomize one tour (admin), then `POST /admin/segment-research/preview` with
    `{"markets": [...], "tour_ids": ["<tour>"], "reatomized": true}`: `reatomized: true`, its new places in
    `places_in_scope` / `places_selected`, `estimate.dfs_usd` shown, nothing bought until `/run`.

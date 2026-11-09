# AA-1055 (S274, lane l10) — research priced at its queue, a stage on every DataForSEO row, a quality report

Follow-up of the buy side (`AA-1055-dfs-buy.md`) after the first queued research run on Dev (09/11/2026, markets
US / UK / AU, `dfs_queue: standard`). That run's preview said 3.486 USD of DataForSEO (12 volume tasks x 0.09 + 1,203
SERPs x 0.002, the LIVE prices); the ledger billed 1.41 USD (0.06 per volume task of up to 1,000 keywords, 0.0006 per
SERP). The estimate also bounds the selection (`max_usd`), so the cap picked about 2.4 times too few places.

## What was built

**One price table, the run's endpoint.**
- `shared/cost_guard.py`: `search_volume_bulk_queue` 0.06 and `serp_advanced_queue` 0.0006 (measured);
  `DFS_RESEARCH_ENDPOINTS` = the endpoint labels research buys through per mode, (volume, SERP), and
  `dfs_research_prices(queue)` reads their prices from `DFS_CALL_ESTIMATE_USD`. An unknown mode raises.
- `segment_research_batch._prices` delegates to it, so `estimate_plan` (preview, `dfs_usd` / `dfs_usd_cap`),
  `select_within_usd` (the USD cap) and the run budget's check before every paid post (`DataForSEOClient._precheck`,
  which reads the same table by endpoint label) use the same numbers. The estimate now returns `unit_usd`
  (`{"volume_task", "serp"}`) so the preview shows which prices it used.
- The balance check is unchanged: the route and the job still compare the balance with `dfs_usd_cap`.
- `admin_segment_research._kwargs` resolves the mode (`research_queue_mode`: body, else `DFS_RESEARCH_QUEUE` of the
  api task, else live) and writes it into the job payload: the job buys through the mode the preview and the balance
  check priced, even if the worker's env differed.

**A stage on every DataForSEO ledger row at the call site.**
- `DataForSEOClient.fetch_balance` writes stage `dfs_balance` (`BALANCE_STAGE`) on its row, also on a failed read and
  inside a job: before, the AA-1058 fallback gave it the bound job's kind (`segment_research`) inside the research job,
  and the rows written before AA-1058 had no stage at all (the 6 `dfs.(none)` rows of the R1 window).
- `_request`, `_log_live`, `_log_failed` take an optional `stage` that overrides the client's.
- The research run's store-hit row (`record_dfs_call_with_pool`, `cache_hit`) carries the run's stage
  (`RESEARCH_STAGE` = `a3_search_demand`), like its paid rows; queue polls (`tasks_ready`, `task_get`) already wrote
  only on failure and with the client's stage.
- Guard `tests/unit/test_aa1055_dfs_ledger_stage.py` (extends the AA-1058 guard to the call sites): AST scan of api /
  services / shared / scripts, every `record_dfs_call*` call passes `stage=` and every `DataForSEOClient(...)` passes
  `stage=` unless it is only `DataForSEOClient(...).fetch_balance()`.

**Quality report** `scripts/aa1055/research_quality.py` (READ ONLY session, counts only) for `JOB=<job id>`, window
= the job's `created_at` .. `finished_at`:
- (a) volume agreement against S1's older live purchase (`seo_context.keyword_ideas`, read with
  `seo_context_copy.plan_copy`): n, equal share, within-10 % share, median relative difference, per market;
- (b) SERP richness of the SERPs stored in the window: >= 3 PAA, >= 1 PAA, >= 1 related, >= 1 domain, median / mean
  domains, distinct domains;
- (c) keyword map of the places logged in the window: keywords, kept vs enforced-reject, shadow rejects, zones;
- (d) demand landing: Segments of those places ranked in that market, with demand, recomputed since the run;
- the bill per stage and endpoint from `dfs_call_log.job_id`, and a sample of <= 30 `keyword|place|kept|rejected`
  lines per market between `=== RQ SAMPLE BEGIN market=<m> n=<k> ===` / `=== RQ SAMPLE END market=<m> ===`.

## Decisions
- Measured prices, not a margin over them: the run budget is narrowed to `max_usd` and checked before every paid
  post, so an estimate a little low cannot overspend; a live-priced estimate under-selects by 2.4x every run.
- The volume price is per task (up to 1,000 keywords), as DataForSEO billed the queue task; the live note that the
  live task price rises with keyword count did not show on the queue (12 tasks, 0.06 each).
- The balance read is `dfs_balance` whoever asks (explicit), instead of the bound job kind: it is not a purchase of
  the stage that asked, and the Cost page sums research spend by stage.
- The quality script's live reference is S1's `seo_context` purchase: research's own older live volumes are gone (the
  store keeps one row per keyword and market and the queue run overwrote them). If the free copy ran before the queued
  run, the overlap is small (those keywords were fresh and not bought again); the report prints n so this is visible.
- The sample includes rejected keywords marked `rejected`, so the relevance panel also judges Jev's rejects.

## Changed vs the issue / the lane brief
- Nothing dropped. Added: `unit_usd` in the estimate, and the job payload carries the resolved `dfs_queue`.
- The old test that held queue prices at or above the live ones (`test_aa1055_buy_side.py`) is replaced by one that
  pins the measured prices below the live ones.

## Tradeoffs
- A later run that researches the same place moves its `segment_research_log` row out of this job's window (the log
  is upserted), so (c) and (d) describe the places still dated in the window; fine for a report run right after.
- (a) counts any store row written in the window as the run's; another writer of `search_demand` in the window (the
  admin SERP top-up) could add rows. Research runs one at a time; the top-up is admin-triggered and rare.

## Should know
- After deploy: preview with `{"markets": [...], "dfs_queue": "standard", "max_usd": 5}` shows
  `estimate.unit_usd = {"volume_task": 0.06, "serp": 0.0006}` and about 2.4x more `places_selected` than before.
- Run the report: `tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1055/research_quality.py JOB=<id>` (no write, no
  provider call).
- `scripts/aa1058/ledger_attribution.py` should show no `dfs.(none)` / `dfs.unattributed` rows for balance reads after
  this deploy; the balance read appears as `dfs.dfs_balance.cis`.

# AA-1051 follow-up (lane l3-job-cost-double, S286) — a job's LLM spend counted twice in shared.job.cost_usd

Symptom (S285 W5 proofs): the Jobs page showed the S1 proof job at 6.22 USD while its `shared.llm_call_log` rows sum to
3.11 USD; the T2 proof 4.99 vs 2.50. Exactly twice the ledger.

Root cause: since AA-1078 (S277) `shared.job.cost_usd` = the handler's `ctx.add_cost` total + the job's
`llm_call_log` sum (the worker binds the job id around every handler, `call_log.bind_job`, so every gateway call inside
a job is tagged; the stamp `progress.cost_stamp.llm_usd` keeps the LLM part). `add_cost` was meant for non-LLM spend,
but six handlers also passed their LLM spend to it.

## Decisions

- **Audit of every `add_cost` site** (each checked for whether its amount is written to `llm_call_log` with the job id
  inside the job context):

  | Site | Amount | Ledger | Verdict |
  |---|---|---|---|
  | `writing_memory_job.run_r2_job` | `out.cost_usd` = `run_spend`, an `llm_call_log` sum | llm_call_log | double: removed |
  | `writing_memory_job.run_shadow_job` | `out["cost_usd"]` = `store.run_spend`, an `llm_call_log` sum | llm_call_log | double: removed |
  | `writing_memory/mining.run_mine` (kind `writing_memory_mine`) | `draft.cost_usd` (`drafter.draft_rule` logs it) | llm_call_log | double: removed |
  | `writing_memory/preference.run_preference` (same kind, mode preference_summary) | `draft_summary` cost (logged there) | llm_call_log | double: removed |
  | `supply_content_draft_job` | `usage["cost_usd"]` (`writer.draft_content` -> `record_call_sync` in a thread, context copied) | llm_call_log | double: removed |
  | `place_dedupe_job` | `models.cost_usd` (each call `record_call_with_pool`; an unusable reply's gateway row) | llm_call_log | double: removed |
  | `embedding_reembed_job` -> `reembed.fill(add_cost=)` | each embed call's cost (`record_call_with_pool`, role embed) | llm_call_log | double: removed |
  | `place_geocode_job`, `place_relocate_job`, `place_shape_job` | Geoapify credits x price | geo.geocode_call | keep |
  | `s1_seo_prefetch_job`, `segment_research_job`, `tour_paa_job` | DataForSEO `run_spent` (dfs budget only) | shared.dfs_call_log | keep |
  | `cost_explorer_refresh_job` | Cost Explorer requests x 0.01 | none (job only) | keep |

- **Budget and cap checks stay** (`budget.charge`, `month.charge`, `run_budget.charge`, the place dedupe `Models.cost_usd`
  in its log line, the result's `cost_usd` fields): only the `add_cost` reporting went.
- **The re-embed cap across retries now reads the ledger.** `fill(already_spent=)` used `ctx.cost_usd` (the handler
  part, which held the LLM spend through `add_cost`). With no `add_cost` that part is 0, so the job passes
  `queue.llm_spend(pool, job_id)` (new helper: the job's `llm_call_log` sum, every attempt) instead. `fill()` lost its
  `add_cost` parameter.
- **Contract written down**: `JobContext.add_cost` docstring says non-LLM spend only and points at the guard.
- **Guard**: `tests/unit/test_aa1051_add_cost_non_llm_only.py` scans `api/`, `services/`, `shared/` with `ast` for
  every `.add_cost(<arg>)` call and `add_cost=<arg>` keyword and compares them with an explicit allow-list of the seven
  non-LLM sites (each with what it pays for). A new site fails until reviewed; an argument that looks like an LLM
  result's cost (`.cost_usd`, `["cost_usd"]`, `usage`, `draft`, `resp`, `llm`) fails with a message that explains the
  double count. A stale allow-list entry fails too.
- **Regression test** (integration, real Postgres): `tests/integration/test_aa1051_job_cost_once.py` runs the REAL
  R2 and shadow handlers in the worker with their paid run faked to write two `llm_call_log` rows: the column, the
  stamp and the Jobs page read (`queue.get`) all equal the ledger sum once; a handler with non-LLM spend still adds it.
- **No migration.** Existing rows are repaired by a one-off script for the orchestrator (not in this repo):
  `AA-Ecosys/.tmp-session/S286/aa1051_job_cost_fix.py` (DRY RUN default, `APPLY=1 FINGERPRINT=<pin>`), runs in the
  CIS api container as `cis_app` (it has UPDATE on `shared.job`).

## Changed

- `shared/jobs/registry.py` (docstring), `shared/jobs/queue.py` (`llm_spend`), the seven handler sites above,
  `services/embeddings/reembed.py` (`add_cost` parameter removed, docs), tests.

## Tradeoffs

- Exact allow-list rather than a name heuristic alone: a reviewed list catches a new site whatever its variable name
  (`cost`, `spent`), at the price of one line to add for each genuine non-LLM site.
- The repair script only touches rows whose handler part equals the stamped LLM part (within 0.000002 USD). A row whose
  handler part is close but not equal (a late ledger row, a gateway row the handler did not count) is counted as
  "near" and left alone: a wrong subtraction would be worse than a visible double.
- Rows written before the AA-1078 deploy (no stamp) were doubled on the read side too (cost_usd + live ledger). They are
  repaired only with `INCLUDE_LEGACY=1` (the lane scope is "since the AA-1078 deploy"; succeeded jobs older than 30 days
  are deleted by retention anyway).

## Should know

- After deploy the per-job cost of these kinds is the ledger once; the Jobs page of an old row stays doubled until the
  repair script runs.
- `JobContext.cost_usd` is now 0 for LLM-only kinds; nothing reads it as an LLM total any more (the re-embed cap was the
  only reader).

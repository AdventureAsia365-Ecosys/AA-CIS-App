# AA-804 — ledger v2 (lane w2a, slice A2 of the S262 gateway plan; AA-642 S7 ledger part)

Lane file: the AA-804 UI lane keeps `AA-804.md`; this file is the backend ledger lane only.

## Decisions

- **Migration 319 is contract, in its own PR (MIG-4).** It adds CHECKs and replaces the role CHECK on existing
  tables, adds columns to the ledger hot tables (`llm_call_log`, `dfs_call_log`, `geocode_call`, `llm_call_daily`),
  and builds two plain indexes on tables above 100k rows (`llm_call_log` ~384k, `decision_log` ~428k). Every one of
  those is "contract" under ADR 0017 d3, so no `-- migrate: expand` header. Order: migration-only PR → Infra byte
  copy → manual DB Migrate apply (job tables idle) → `bot/db-schema-reference` → this code PR.
- **All new columns are nullable with no default** (`is_shadow`, `is_experiment`, `app` too): a constant default
  would label every old row (an old shadow row would read `is_shadow = false`). Readers use
  `coalesce(app, quality_signal->>'app', 'cis')` and `coalesce(is_shadow, quality_signal @> '{"shadow": true}')`.
  No backfill (plan).
- **One attempt = one `LLMClient._attempt` call, at exactly one level per path:** `_call_target` (deployment plan,
  alias steps, the admin probe), each legacy-chain tier (`_legacy_tier`, the connection budget check inside it, so a
  budget refusal is an attempt row), and the catalog paths without a plan (pin, S2 vendor, `openai_chat`, Converse
  fallback). `_dispatch_target` and the adapters are not wrapped, so nothing is counted twice.
- **The net:** a request refused before any attempt (model not in the catalog / disabled, every deployment behind an
  open circuit or over budget, no usable deployment) gets one row from `_call_key_logged` (and `probe`). It writes
  nothing when an attempt row or a billed row already covers the failure: logged exceptions are marked, and the net
  walks the cause chain, so `_all_failed`'s aggregate error is never logged again. A real attempt is always logged,
  even if a provider raises the same exception object twice.
- **status / error_class:** `refused` = nothing billed because the provider or the gateway said no (access_denied,
  quota_exhausted, credential, throttled, open circuit, limiter, data-scope block, budget); `error` = anything else
  (outage, timeout, unbilled bad reply). `error_class` = `breaker.failure_class` with its `request` class mapped to
  `other` (the CHECK keeps the plan's list: health classes + deadline, budget, truncated), or `budget`.
- **Success rows:** status derived when the caller says nothing — `billed_unusable` for a `billed_unusable` signal,
  `truncated` (+ `error_class` truncated) for stop reason `max_tokens` / `length`, else `ok`. `app` = 'cis'.
  `connection_key` column = the vendor connection, else the row's account (a Bedrock account key is its connection).
- **`billed_fields(resp)` grows by the attempt facts** (`V2_BILLED_KEYS`: latency_ms, attempt_no, deployment_wire_id,
  request_id, effort, reasoning_tokens, prompt_hash), so every existing call site carries them with no edit.
  Structures that copy billing (`brand_fit.RUN_BILLING_KEYS` / `BrandFitResult`, the T10 judge dict) now take
  `call_log.BILLED_FIELD_KEYS`, so the next addition flows on its own.
- **`prompt_hash`** = sha256 hex of system + NUL + user prompt (64 chars), computed once per request scope. Not in the
  plan's `LLMResponse` list; added because the gateway's failure rows have it and the success row should match.
- **Fallbacks kept:** the v2 INSERT falls back to the S6 / plain one on `UndefinedColumnError` (and on a
  `CheckViolationError`, so a billed row is never lost), inside a savepoint when the caller holds a transaction
  (`record_calls`). Same for `dfs_call_log` and `geocode_call`. The readiness requirement is still raised to 319
  because the readers name the new columns.
- **AA-729 harness:** `evalpatch.relabel_v2_sql` relabels the v2 statement (eval stage, NULL tenant / piece /
  request / tour — the tour kept as `quality_signal.aa729_tour` — and `is_experiment` forced true). `call_log` uses
  v2 only when both statements are shipped or both replaced, so an old harness that patches only `_INSERT_SQL` still
  writes the relabelled base row.
- **Jev rows** stay `role='validate'`, `provider='typesafe'` (the role CHECK now allows `decide`, but switching would
  split today's reports; left for the registry v2 slice). Answered asks add `latency_ms`, `units` = questions,
  `unit_kind` 'question'. Failed / refused asks write a 0 USD row (breaker open and 402 → refused quota_exhausted,
  daily cap → refused budget, other → classified).
- **DataForSEO:** one `_request` helper times every live HTTP call and writes the failure row (401/402/403 → refused,
  else error) before re-raising the same exception, so each method keeps its own handling; `_precheck` writes the
  budget refusal row. A 200 with a failed DFS status is `error` / `refused` on the live row. `record_dfs_call_sync`
  under a running loop now writes on a non-daemon thread (`call_log._in_background`) instead of an unreferenced
  `ensure_future` task (the AA-927 lost-row bug; every DataForSEOClient method runs under a loop).
- **S1 Bedrock Batch (AA-642 S7 ledger part):** `ingest_s1_batch` writes one row per record before processing the
  tours (`batch_ledger_rows`): stage `s1_generate`, model `haiku-4-5` (what `submit_batch_job(model="haiku")` runs),
  priced with `pricing.quote(..., Usage(batch=True))` → `catalog+batch`; a record with usage but no usable text is
  `billed_unusable`; no usage / missing output → 0 USD error row. The registry test now registers the batch site
  with `ingest_s1_batch` as its writer (the EXCLUDED entry is gone).

## Changed (vs. the plan text)

- `llm_call_daily` also gets `latency_ms_count` (bigint NOT NULL DEFAULT 0): a sum alone cannot give a mean when old
  rows and TripPlanner rows have no latency.
- Readers changed beyond the three named (rollups, platform_apps, platform_cost): `services/reports/kpis.py`,
  `money_sources.py`, `planner.py`, `api/routers/admin_tenant360.py` (shadow), and `api/services/llm_availability.py`
  (answered calls only: `status` refused / error rows are left out, otherwise a refusal would read as an answer).
- `services/places/shape_sources.py` (Place Details) counts failed requests too; Overpass (free) does not.
- `CONTEXT.md` ledger paragraph rewritten (the "a refused call writes no row" sentence was no longer true).

## Tradeoffs

- **Row growth:** refused attempts at 0 USD add rows (AA-825 keeps 180 days). During an outage the breaker opens
  after one account-level refusal, so later requests are refused by the net (one row per request, not per
  deployment). Accepted: the trace value is the point of the slice.
- **Plain CREATE INDEX** holds writes on `llm_call_log` / `decision_log` for seconds; CONCURRENTLY cannot run in the
  DB Migrate transaction. Apply with job tables idle.
- **Calls counts now include attempts:** usage views that `count(*)` llm_call_log rows (admin usage tree, cost-by-
  stage, KPI calls, daily `calls`) count failed attempts too (cost 0, no ok-rate key). Cost sums and ok-rates do
  not move. A reader that wants answered calls filters `coalesce(status, 'ok') NOT IN ('refused', 'error')`.
- Embeddings: a failed `embed()` still writes no row (out of this slice; the caller logs a success).

## Should know

- Live check after the code deploy (0 USD): `POST /admin/llm-connections/anthropic-direct/test` (no key) answers
  `ok: false, error_class: credential` and writes exactly one `llm_call_log` row: stage `adhoc_conn_test`, role
  `validate`, `status='refused'`, `error_class='credential'`, `cost_usd=0`, `connection_key='anthropic-direct'`.
  Q0 route hash must stay `d13dd30e60453c4fa11a870e6b1fdc83` (no route change in this slice).
- The base branch is the AA-642 S0 PR (#1197); this branch was cut from it and is rebased on `main` once #1197 merges.
  The migration commit is also on the migration-only branch `feat/aa-804-migration-319` (same patch, dropped by the
  rebase once that PR is on `main`).
- Tests: `tests/unit/test_aa804_ledger_v2.py`, `tests/integration/test_aa804_ledger_v2_db.py`,
  `tests/integration/test_aa804_migration_319.py`; updated `test_aa927_one_row_per_call.py` (m1),
  `test_aa1002_s6_billed_fields.py`, `test_aa606_bedrock_batch.py`, `test_aa660_decide.py`,
  `test_aa642_s0_jev_budget.py`, `test_aa493_stop_reason.py`, `test_aa617_llm_account_instrumentation.py`.

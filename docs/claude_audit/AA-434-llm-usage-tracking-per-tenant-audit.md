# AA-434 — LLM Usage Tracking Per-Tenant + T-Stage: Audit (investigate-only)

Date: 22/08/2026 | Branch: main (no changes made — audit only) | DB: aa-cis-dev-db (live query, ECS S3-mediated exec)

## Question being answered

Nghiep: "theo dõi lưu lượng sử dụng của tenant thế nào, ví dụ chi tiết mỗi lượt gọi LLM cho
từng T?" — Can we currently answer *"tenant X called the LLM N times, at $Y cost, at which
T-stage, in time window Z"*? This is investigate-only per AA-434 scope — no code changed.

## TL;DR — answer to the decisive question (§5 of the task)

**THIẾU (insufficient) — not a small "add one query" job.** The tenant-rewrite pipeline
(T1–T5, `services/acp_produce/tenant_pipeline.py` + `api/routers/v1_tours.py`'s
`trigger_rewrite()`) computes `cost_usd`/tokens **in memory** for every LLM call, at every
stage, then **discards all of it** before anything is persisted. Zero DB column anywhere
stores tenant-rewrite LLM cost, tokens, model, or stage. The one existing cost-tracking
system in this codebase (`services/acp_shared/cost_utils.py`, built for AA-118) is real and
working, but it instruments a **different, unrelated pipeline** (the admin/ACP N1–N8
flywheel) — and confirmed live query shows that pipeline's own run table currently holds
**zero rows** in this environment, so even that system has nothing to show yet.

## Evidence

### 1. T2 (rewrite) computes cost_usd, then it's thrown away

`services/content_generation/graph.py` accumulates `cost_usd` across every node in the
chain (`generate_node` line ~369/375, `judge_node.py:164`, `flag_fix_node.py:252/362`,
`brand_audit_node.py:277`) — this is real, working per-call cost math (input/output tokens
× Bedrock pricing), logged via structlog (`logger.info("content_generated", ...,
model=resp.model_used, cost=resp.cost_usd)`).

**But `graph.py` has zero references to `tenant_id` anywhere in the file** (`grep -n
tenant_id services/content_generation/graph.py` → no matches). The graph state that carries
`cost_usd` never carries which tenant the run is for — so even the CloudWatch log line
above can't be filtered/grouped by tenant, structurally, not just because no one queries it.

`api/routers/v1_pipeline.py::_rewrite_tour()` (the function both admin batch-rewrite and
tenant rewrite call) reads `result.get("cost_usd", 0.0)` back out of the final graph state
and puts it in the dict it returns (line 161) — the value is available to the caller.

`api/routers/v1_tours.py::trigger_rewrite()` → `_do_rewrite_and_save()` (the tenant flow,
lines 309–410) calls `_rewrite_tour()`, gets that dict back as `result`, and then only ever
reads `result["generated"]` and `result.get("quality_score")` from it (lines 329, 341). The
`cost_usd`, `model_used`, `cache_read_tokens`, `cache_write_tokens` keys that ARE present in
`result` are never read, never logged, never written anywhere. The only DB write in this
whole function is:

```python
UPDATE gold_aa_internal.tenant_tour_versions
SET rewritten_content = $1::jsonb, status = $2, quality_score = $3,
    qa_status = $4, qa_repair_count = $5, qa_checked_at = now()
WHERE id = $6::uuid
```

— confirmed live (`information_schema.columns` query against the actual RDS instance,
22/08/2026): **`gold_aa_internal.tenant_tour_versions` has no cost/token/model column at
all.** Full column list as it exists today:

```
id, tenant_id, published_tour_id, version_number, parent_version_id, rewritten_content,
status, quality_score, edit_source, rewrite_language, seo_mode, edited_at,
edited_by_user, created_at, qa_status, qa_repair_count, qa_checked_at
```

(Note: an *older*, since-dropped `gold.tenant_tour_versions` from migration
`001_initial_schema.sql` DID have a `generation_cost_usd NUMERIC(8,6)` column — migration
`003_schema_v3.sql` drops that whole table (`DROP TABLE IF EXISTS tenant_tour_versions
CASCADE`) when moving to the `gold_aa_internal` schema, and no migration ever re-added a
cost column on the replacement table. So this isn't a case of "never built" — it's a case
of "existed once, was dropped in a schema migration, never re-added.")

### 2. T3 (QA gate repair loop) multiplies the untracked cost 1–3×

`run_t3_qa_gate()` (`services/acp_produce/tenant_pipeline.py:113`) re-invokes
`_rewrite_tour()` up to `TENANT_QA_MAX_REPAIRS=2` more times on failure. Each repair attempt
is a full extra LLM round-trip with its own `cost_usd` computed inside `graph.py` — same
fate as above: read into `result`, never persisted. A tour that fails QA twice before
escalating burns 3× the LLM cost of a clean pass, with the same **zero** dollars recorded
either way. Live data confirms repair rounds actually happen in production: querying the 10
most recent `tenant_tour_versions` rows for the one active test tenant
(`9fb0a3db-59aa-468a-a082-ded01ac50bee`) shows 6 of 10 rows with `qa_repair_count = 2`
(escalated) — real repeated LLM spend with no cost trail.

### 3. T5 (atomize) doesn't even compute cost — it discards raw token usage

`run_t5_atomize()` (`tenant_pipeline.py:195`) calls `invoke_claude()`
(`shared/llm_client/bedrock_satellite.py:212`) directly. `invoke_claude()` returns a
`BedrockInvokeResult(text, model_used, latency_ms, usage)` — `usage` is the raw Bedrock
`{input_tokens, output_tokens}` dict, straight from the API response. `run_t5_atomize()`
reads `llm_result.text` only (line 247) — `llm_result.usage` is never touched. Unlike T2's
path, this call site doesn't even have a cost formula available to it (`BedrockInvokeResult`
has no `cost_usd` field, unlike the `LLMResponse`-style object `graph.py`'s nodes use) — so
this is a stage where cost isn't just unpersisted, it isn't even *computed*.

### 4. The only per-tenant signal that exists today is a bare COUNT, no cost/stage

`GET /admin/metrics` (`api/routers/admin_pipeline.py:3370`, the endpoint behind
`/admin/dashboard`'s **Model Usage** panel — confirmed via
`frontend/app/admin/dashboard/page.tsx:45` reading `data.model_usage`) does have one
tenant-grouped query:

```python
tenant_breakdown = await conn.fetch("""
    SELECT t.slug, t.plan_tier, COUNT(ttv.id) AS rewrite_count
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
    GROUP BY t.slug, t.plan_tier ORDER BY rewrite_count DESC
""")
```

This is a **count of rewrite attempts per tenant**, nothing about cost, tokens, model, or
which T-stage. It can't be extended into a real cost/stage breakdown because the source
table (`tenant_tour_versions`) has no cost data to aggregate (§1).

The **Model Usage** panel itself (`models` query, `admin_pipeline.py:3412`) groups by model
from `silver_{tenant_slug}.generated_content` — with `tenant_slug` **hardcoded to
`"aa_internal"`** (line 3378: `tenant_slug = "aa_internal"`), i.e. it's scoped to the
admin/master-content table only, structurally incapable of showing any tenant's usage even
if that table had cost columns (it doesn't — see column list below). Its cost figure
(`cost_by_model`, joined in by model name only) comes from `shared.pipeline_runs`, a
completely different, batch-level ingestion-pipeline table — not joined to
`generated_content` by any row-level key, only merged by matching model-name string.

### 5. The one real cost-tracking system in this repo (AA-118) instruments a different pipeline, and has zero rows right now

`services/acp_shared/cost_utils.py` (`record_stage_cost`, `finalize_run_cost`,
`calc_bedrock_cost`, `extract_usage_from_response`) is a genuine, working
per-stage-cost-accumulator — called from `services/acp/s2/tools/synthesize.py`,
`services/acp_s4/graph.py`, `api/routers/v1_s4_social.py`, `api/routers/v1_acp.py`. It
writes to `acp_shared.acp_stage_runs` (migration `055_acp_cost_columns.sql`, AA-118) keyed
by `run_id` + `stage`, and sums into `acp_shared.acp_runs.total_llm_cost_usd`. This is the
**admin-side ACP N1–N8 flywheel** (S1 keyword→S2 synthesize→S3 plan→S4 blog/social), an
entirely separate pipeline from the tenant T1–T5 rewrite flow this task is about.

Live query confirms columns exist as designed:
```
acp_shared.acp_runs:        ... cost_usd, total_llm_cost_usd, tenant_id (varchar), ...
acp_shared.acp_stage_runs:  run_id, stage, llm_cost_usd, tokens_input, tokens_output, ...
```

But: **`SELECT COUNT(*) FROM acp_shared.acp_runs` → 0 rows, in the live dev DB, right now.**
`acp_shared.acp_stage_runs` → also 0 rows. AA-118 (closed Done in June) noted "likely not
being populated" as a self-flagged doubt; the live count today shows there is currently
nothing to populate — the whole ACP N-pipeline apparatus this table serves has not recorded
a single run in this environment as of this audit. (AA-258/AA-255's "scattered, no rollup"
framing checks out for this table too, just via a different mechanism than expected: it's
not partially-filled-but-unqueried, it's empty.)

### 6. Where per-tenant cost tracking DOES already work (different pipeline, for contrast)

`shared.pipeline_runs` (the **admin ingestion pipeline**, S0→S4 batch upload flow) is the
one table in this codebase where cost tracking is real and populated: `tenant_id`,
`cost_usd`, `tokens_input`, `tokens_output`, `tokens_cached`, `llm_model` all present and
filled. Live query: 38 rows total, 38/38 have `tenant_id`, 27/38 have `cost_usd > 0`
(non-ingesting rows presumably; `status != 'ingesting'` rows are the ones actually costing
anything). This is genuinely a working per-tenant cost table — it's just for a different
pipeline (Excel-upload ingestion batches) than the one Nghiep asked about (T1–T5 tenant
rewrite), and it's per-*batch*, not per-LLM-call or per-T-stage.

### Schema reference (as queried live, 22/08/2026)

| Table | tenant_id? | cost/tokens columns? | stage column? | Rows now |
|---|---|---|---|---|
| `gold_aa_internal.tenant_tour_versions` (T-pipeline's only persisted state) | yes | **none** | no (only `qa_status`, not "which T") | 10 (1 test tenant) |
| `silver_aa_internal.generated_content` (admin/N4, feeds Model Usage) | yes (unused by that query) | **none** | no | — |
| `acp_shared.acp_runs` / `acp_stage_runs` (AA-118, admin N1-N8) | yes | yes, real schema | yes (`stage` varchar) | **0** |
| `shared.pipeline_runs` (admin ingestion S0-S4) | yes, populated | yes, populated | `step_name` column exists | 38 |
| `acp_contract.tour_atoms` (T5 output) | no (`owner_scope` text, holds tenant_id as a string for tenant rows) | none | n/a | 2629 platform / 15 tenant |

## Answer to Q5, restated for the "how big is the fix" decision

**Not a small fix.** To answer "tenant X, N calls, $Y, which T-stage, in window Z" for real,
every one of these needs code changes, not just a new SELECT:

1. **New column(s)** on a table that can hold one row per LLM call (or per T-stage per
   rewrite) with `tenant_id`, `stage` ('T2'|'T3-repair-N'|'T5'), `model`, `tokens_in`,
   `tokens_out`, `cost_usd`, `timestamp`. `tenant_tour_versions` is 1 row per rewrite
   *version*, not per LLM call — T3's repair rounds and T5's atomize call would need to
   either fold into that row (losing per-call granularity) or go in a new child table.
2. **T2/T3 (graph.py path):** wire `tenant_id` into the graph state (currently entirely
   absent) so cost accumulation can be attributed, then persist `result["cost_usd"]` /
   `cache_read_tokens` / `cache_write_tokens` (already computed, just discarded) at the
   `trigger_rewrite()` call site — 1 call site, 1 UPDATE to extend.
3. **T3 repair rounds:** each `_rewrite_tour()` call inside `run_t3_qa_gate()`'s loop
   produces its own cost — needs its own row/increment if per-attempt granularity matters,
   or at minimum accumulate into the version row's total if only sum-per-version is needed.
4. **T5 (tenant_pipeline.py `run_t5_atomize`):** currently doesn't even compute cost —
   needs `calc_bedrock_cost`-equivalent math added (the `usage` dict from
   `invoke_claude()`'s `BedrockInvokeResult` is already available, just unread) plus a
   persist call.
5. **New endpoint/view** for the actual "tenant X usage in window Z" query — cheap, once
   1–4 exist.

Rough shape: 3 call sites need a persist call added (T2 in `trigger_rewrite`, T3's repair
loop, T5's `run_t5_atomize`), 1 schema migration (new column(s) or child table), 1 new
endpoint. Not a 1-query afternoon job, but not a redesign either — the cost numbers already
exist in memory at every one of these call sites; the gap is entirely "compute-but-never-
write" (T2/T3) or "receive-but-never-compute" (T5), not "no way to get the number."

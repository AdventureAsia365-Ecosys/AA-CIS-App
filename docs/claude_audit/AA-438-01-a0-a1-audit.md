# AA-438 — Audit A0→A1 (Raw Ingest → Generic Rewrite with DFS)

Audit only. No code changed. Every claim below is backed by a `path:line` snippet, a real
DB query result (S3-mediated ECS exec, `aa-cis-dev-cluster` / `aa-cis-dev-db`, run
22/08/2026), or a live AWS CLI call. Where I could not confirm something, it's flagged
explicitly rather than guessed.

**ADR-2026-038 itself: NOT FOUND.** `find`/`grep -rl "ADR-2026-038"` across
`~/projects/aa-cis` (all 4 repos + `.venv`) returned nothing. There is no ADR document in
this codebase defining A0-A3/T0-T11. Everything below about "what A0/A1 mean" is inferred
from the sidebar naming (`AA-323 round 6, Phần D` comment,
`frontend/app/admin/_components/AdminSidebar.tsx:21-31`) and from what the code actually
does — **not** from the ADR text, which I never had access to. If the ADR exists in
Notion/Linear, it should be linked from this doc later.

---

## 1. Trigger

**A0→A1 is 100% manual — there is no automatic trigger.**

- Upload (S0) commit (`doCommit()`, `frontend/app/admin/upload/page.tsx:1080-1128`) POSTs to
  `/api/admin/ingest-s3` with `dry_run:false`. On success it shows a toast
  ("Tours saved to database successfully") or, if duplicates were staged, routes to a
  Duplicate Review step. It does **not** call any rewrite/run-tour endpoint.
- The only link between the two pages is a UI navigation link — "Go to S1 Rewrite"
  (`upload/page.tsx:394-399` and `:928-935`) — that just does `<a href="/admin/s1-rewrite">`.
  Nothing is queued or auto-started.
- On S1 Rewrite (`frontend/app/admin/s1-rewrite/page.tsx`), a human must select tours via
  checkboxes and click **"Run Rewrite (N)"** (`:596-611`), which fires
  `POST /api/admin/run-tour-async` per tour (`:313-325`), 3 concurrent workers
  (`runWorker()`, `:348-361`).

So "A0 done" just means rows exist in `raw_tours` with `pipeline_status='ingested'`
(or `'preview'` pre-commit). Nothing advances them to A1 until an admin opens S1 Rewrite and
explicitly runs it.

**Separately, there IS a real automatic path that bypasses the admin UI entirely**: a Lambda
`aa-cis-dev-ingestion` (`AA-CIS-Infra/modules/lambda/main.tf:179-207`) is wired via
`aws_s3_bucket_notification` (`:219-229`) to fire on `s3:ObjectCreated:*` for
`raw-inbox/*.xlsx` in the Bronze bucket — the exact same bucket+prefix the Upload (S0) page's
presigned PUT writes to (confirmed live:
`aws lambda get-function --function-name aa-cis-dev-ingestion` → env
`BRONZE_BUCKET=aa-cis-bronze-{acc2-id}`, matching the ECS task def's `BRONZE_BUCKET` env
var, also `aa-cis-bronze-{acc2-id}`). This means **every Upload (S0) file upload likely
double-fires ingestion**: once via the real S3-triggered Lambda, and once when the admin
clicks "Commit" and the FastAPI backend calls `services.ingestion.handler.process_file()`
**in-process, directly, not by invoking the Lambda** (`api/routers/admin_pipeline.py:1204-1206`
comment: `# dry_run=False: insert raw_sources + raw_tours, no pipeline trigger` →
`from services.ingestion.handler import process_file`). Both paths call the same
`process_file()`, which has its own `file_hash`-based idempotency check
(`services/ingestion/handler.py`, dedup before insert) — so this is very likely wasted Lambda
spend/log noise rather than a duplicate-data bug, but I have **not** confirmed actual
double-invocation with CloudWatch Logs Insights — flagging as unconfirmed.

---

## 2. Backend

### A0 = Upload (S0) — confirmed
- `POST /admin/upload-url` (`api/routers/admin_pipeline.py:1059-1075`) — presigns an S3 PUT
  into `raw-inbox/{tenant_id}/{uuid}_{filename}` on `BRONZE_BUCKET`.
- `POST /admin/ingest-s3` (`:1080` onward):
  - `dry_run=true` → parses the `.xlsx` with `services.ingestion.excel_parser.ExcelParser`,
    checks dup by `file_hash` and by normalized `src_name` against
    `silver_aa_internal.raw_tours`, returns a preview (no DB write for tour rows).
  - `dry_run=false` → calls `services.ingestion.handler.process_file()`, which:
    1. `INSERT INTO shared.pipeline_runs (batch_id, tenant_id, s3_source_path, status='ingesting', tours_total)` — **this is the only INSERT into `pipeline_runs`, and `status` is hardcoded `'ingesting'`** (`services/ingestion/handler.py:132-144`).
    2. Inserts a `silver_aa_internal.raw_sources` row (upload history entry).
    3. Inserts rows into **`silver_aa_internal.raw_tours`** (confirmed table — not a
       different bucket/table), `pipeline_status='ingested'`, tagged with the same
       `batch_id`.
  - Duplicate tours (by name) go to `silver_aa_internal.upload_staging` instead
    (`admin_pipeline.py:1080-1180`), resolved later via
    `POST /admin/upload-staging/{staging_id}/decide` → INSERT into `raw_tours` on
    bypass/replace/keep_both (`:1300-1400`).

### A1 = "S1 Rewrite" — confirmed as the Generic Rewrite step, and DFS = DataForSEO
- `POST /admin/run-tour` / `POST /admin/run-tour-async` (`admin_pipeline.py:979-1014`) both
  wrap `_execute_run_tour()` (`:334` onward), the actual rewrite executor.
- **DFS is not defined anywhere as an acronym in a comment**, but the code makes the mapping
  unambiguous: `_execute_run_tour` imports
  `services.seo_intelligence.seed_builder.build_seed` (`:460`) and the request's `seo_mode`
  (from the S1 Rewrite UI's "SEO Mode" selector — standard/aggressive/minimal) is mapped via
  `_SEO_MODE_MAP = {"standard": "dataforseo", "aggressive": "dataforseo", "minimal": "disabled"}`
  (`:456`). The result flag is literally named `dataforseo_used` (`:479`, `:542`). The
  provider client class is `DataForSEOClient`
  (`services/seo_intelligence/dataforseo_client.py`, also used directly in
  `tests/unit/test_aa197_dfs.py`). **DFS = DataForSEO** (the third-party SEO/keyword-data
  provider), confirmed by code, not by ADR text (ADR not found — see header).
- Input source: reads the tour from `silver_aa_internal.raw_tours` by `tour_id` (passed from
  the S1 Rewrite table row, which is populated by `GET /admin/tours`), runs the LangGraph
  pipeline (`generate → validate → llm_judge → brand_audit → flag_fix → revalidate`, per
  `frontend/app/admin/s1-rewrite/page.tsx:73-92` comment, itself sourced from
  `services/content_generation/graph.py::build_graph`), and writes the result into
  `silver_aa_internal.generated_content`.
- **"Generic"**: this is AA-internal's own tour-copy rewrite (`aa_internal` tenant), separate
  from the B2B tenant-specific ACP v2 rewrite flow — matches the sidebar's own framing
  (`AdminSidebar.tsx:25-27`: "AA-internal's own content-authoring pipeline… a different, older
  system for AA's own tour copy, unrelated to the B2B tenant flow").

---

## 3. Frontend — both pages are real, not placeholders

- **Upload (S0)** (`frontend/app/admin/upload/page.tsx`): real 5-step flow — file select → S3
  PUT via presigned URL → `POST /api/admin/ingest-s3` (dry-run parse) → review → commit
  (`POST /api/admin/ingest-s3` dry_run=false) → duplicate review
  (`GET/POST /api/admin/upload-staging/...`). Also renders live "Tours Ready for Rewrite"
  (`GET /api/admin/tours-ready`) and "Upload History" (`GET /api/admin/upload-history`)
  sections with real trash/restore actions (`PATCH /api/admin/tours/{id}/trash|restore`).
  No mock data, no stub state — every action is a real fetch to the same-origin
  `/api/admin/[...path]` proxy (per `AA-CIS-App/.claude/CLAUDE.md`'s documented proxy
  convention, which attaches `X-Admin-Secret` server-side).
- **S1 Rewrite** (`frontend/app/admin/s1-rewrite/page.tsx`): real tour table
  (`GET /api/admin/tours`), brand-identity picker (`GET /api/admin/brand-rules`), a 3-worker
  async run queue against `POST /api/admin/run-tour-async` with 5s polling of
  `GET /api/admin/jobs/{job_id}` for `current_stage`/`status`, and a live stage progress bar
  mapped from the 7 real LangGraph node names. Not a placeholder.
- I did not have a Playwright/browser session available in this environment, so "click
  through it live" is based on a full read of both route components plus their called
  endpoints, not an actual browser click — noting this as the verification method used.

---

## 4. Data — real queries against `aa-cis-dev-db`, run 22/08/2026 (ECS exec, `api` container)

```
raw_tours_total: 793
raw_tours_by_pipeline_status: [ {ingested: 721}, {published: 72} ]
raw_tours_by_source_status:   [ {active: 788}, {superseded: 5} ]

generated_content_total: 228
generated_content_by_status: [ {approved: 189}, {hitl: 39} ]
generated_content last 14d: 16 rows (most recent: 2026-08-21 11:59:08 UTC, status='hitl')

pipeline_runs_total: 38
pipeline_runs_by_status:
  completed:  22 rows  (started_at range 2026-05-12 → 2026-05-27)
  ingesting:  16 rows  (started_at range 2026-05-25 → 2026-07-29)
MAX(started_at) across ALL pipeline_runs: 2026-07-29 04:05:46 UTC
NOW() at query time: 2026-08-22 15:09:38 UTC  →  no pipeline_runs row in ~24 days

published_tours (gold): 72 (matches raw_tours pipeline_status='published' count)
tenant_tour_versions (gold): (queried, not the bottleneck here — dashboard's "23 tenant rewrites" figure, not re-verified independently since it isn't part of the A0/A1 question)
```

A0 (raw ingest) output lives in `silver_aa_internal.raw_tours` — confirmed 793 rows.
"Passed through A1" is tracked by `pipeline_status`: `ingested` (721, not yet
rewritten/published) vs `published` (72, went all the way through). There is **no** separate
boolean column marking "A1 ran" independent of the export outcome — a tour that was rewritten
but landed in `generated_content.status='hitl'` (needs human review) still shows
`pipeline_status='ingested'` in `raw_tours`, same as a tour never rewritten at all. So
`raw_tours.pipeline_status` alone **cannot** distinguish "never rewritten" from "rewritten but
stuck in HITL" — you have to join to `generated_content` to see that (39 rows currently sit at
`status='hitl'`).

**Traced the 16 real generated_content rows from the last 14 days back to their batch_id**
(`raw_tours.batch_id` → `shared.pipeline_runs.batch_id`):

```
tour_id 00164580… → batch_id a3183eb2…  (generated 2026-08-21, status=hitl)
                     → pipeline_runs row: status='ingesting', started_at=2026-07-20
tour_id 4e84b2fb…  → batch_id e1292a49…  (generated 2026-08-17 x2, status=hitl)
                     → pipeline_runs row: status='ingesting', started_at=2026-07-20
(3 more batch_ids from the same 16-row set, all status='ingesting', started_at
 2026-06-01 or 2026-07-20)
```

**Every single one of these real, recent rewrite runs traces back to a `pipeline_runs` row
that (a) is still `status='ingesting'` and (b) was ingested 3-12 weeks before the rewrite
actually happened.** This is the direct evidence for the dashboard bug below.

---

## 5. Gap — why Dashboard shows "No pipeline activity (7D)" / "Pass Rate 0%" despite real runs

**Root cause, confirmed by code + the query above — not a hunch:**

`GET /admin/metrics` (`api/routers/admin_pipeline.py:3370-3395`), the handler behind the
Dashboard's Overview tab, builds `daily_runs` (→ both the "Daily Volume" chart and the
"Pipeline Activity (7D)" table, `frontend/app/admin/dashboard/page.tsx:114-160`) from:

```sql
SELECT DATE(started_at) AS day, COUNT(*) runs, ...
FROM shared.pipeline_runs
WHERE started_at >= NOW() - ($1 || ' days')::interval
  AND status != 'ingesting'
GROUP BY DATE(started_at)
```

Two independent problems, both confirmed against real data, that combine to guarantee this
table is empty right now:

1. **`started_at` is set once, at A0 ingest time, and never touched again.** A1
   (S1 Rewrite) runs are bucketed by *when the source file was uploaded*, not by when the
   rewrite happened. Every one of today's/this-week's real rewrites (see §4) belongs to a
   batch ingested 20/07 or 01/06 — outside any reasonable 7-day window — so they can never
   appear in "Pipeline Activity (7D)" no matter how much A1 activity happens, unless a fresh
   A0 upload also happens in the same week.

2. **`status` only ever leaves `'ingesting'` via `services/export/handler.py:92-98`**:
   ```python
   if pending == 0:   # ALL tours in the batch reached pipeline_status='published'
       UPDATE shared.pipeline_runs SET status='completed', completed_at=NOW()
       WHERE batch_id=$1::uuid AND status='ingesting'
   ```
   The only other status transition is `'failed'`, set by `_run_tour_safe`
   (`admin_pipeline.py:761-767`) only after 3 exhausted retries. **There is no code path that
   marks a batch `'completed'` (or anything else) when its tours land in `generated_content.
   status='hitl'` instead of fully publishing** — which is exactly what happened to all 16
   recent rows (§4). So even ignoring problem #1, these batches would still fail the
   `status != 'ingesting'` filter forever, because none of their tours got all the way to
   `published` (HITL review is still pending on them).

   Separately, `_execute_run_tour`'s own accounting UPDATE
   (`admin_pipeline.py:693-708`, cost/tokens) is **silently skipped** whenever `batch_id`
   isn't a valid UUID matching an existing row (`AA-210` comment, `:709-714`) — a secondary,
   smaller gap: a rewrite fired with a non-matching/fallback `batch_id` (e.g. the frontend's
   `tour.batch_id || TENANT_ID` fallback, `s1-rewrite/page.tsx:318`, where `TENANT_ID` is a
   fixed UUID that is **not** a real `pipeline_runs.batch_id`) leaves zero trace in
   `pipeline_runs` at all, by design (logged as `pipeline_runs_accounting_skipped`), not
   flagged as an error anywhere visible in the UI.

**"Pass Rate 0%" is a direct downstream symptom of #1/#2**, not a separate bug: the frontend
computes `passRate = totalPassed / totalTours` from the same (always-empty) `daily_runs` array
(`dashboard/page.tsx:48-50`) — 0 tours in the window ⇒ 0%, not "0% quality."

**"Total Content: 94 (71 master + 23 tenant rewrites)" is correct and NOT from the same buggy
query** — it comes from `published_count` (`gold_aa_internal.published_tours`, unconditional
`COUNT(*)`) + `tenant_rewrite_count` (`gold_aa_internal.tenant_tour_versions`, unconditional
`COUNT(*)`), neither of which is date- or status-filtered
(`admin_pipeline.py:3434-3441`). That's why this number is real and non-zero while "Pipeline
Activity (7D)" right next to it is empty — they read from genuinely different tables with
genuinely different filters, not from one query that's "sometimes right."

**Verdict: this is a real bug, not a UI artifact of A0/A1 running outside job orchestration.**
The task prompt's alternative hypothesis ("A0/A1 don't run through job orchestration, so
pipeline activity isn't counted for admin-tier stages") is **not what's happening** — A0
*does* write a `pipeline_runs` row every time (§4, confirmed), and A1 *does* update it when
cost/tokens are non-zero and the batch_id matches. The table just structurally can't reflect
"rewrite happened this week" because it's keyed to ingest date and to a `status` that only
advances on full publish — not on rewrite completion.

**Separately (dashboard's "Ingestion Lambda: Idle" card, Pipeline Health row):** this reads
`shared.tenant_api_usage` for `/admin/upload-url` calls in the **last 1 hour**
(`admin_pipeline.py:3469-3492`), and falls back to `"idle"` if none. This is unrelated to
whether the *actual* `aa-cis-dev-ingestion` Lambda (§1) has been invoked by AWS — that table
only logs HTTP endpoint calls through the API, not real AWS Lambda invocation events, so this
card cannot ever reflect the real Lambda's AWS-side invocation history even if the health
model were otherwise correct. Not independently verified against CloudWatch Lambda invocation
metrics for `aa-cis-dev-ingestion` — flagging as unconfirmed, would need a
`GetMetricStatistics`/Logs Insights call to close out.

---

## Open items — explicitly unconfirmed, need more access/time

- ADR-2026-038's actual text (definition of A0-A3/T0-T11, and DFS's canonical meaning per the
  ADR rather than inferred from code) — not found in any of the 4 repos under
  `~/projects/aa-cis`. Needs Notion/Linear lookup outside this environment's reach.
- Whether Upload (S0) actually double-fires ingestion (real Lambda + in-process
  `process_file()` call) in practice — architecturally very likely (same bucket/prefix,
  confirmed live) but not confirmed via CloudWatch Logs Insights invocation counts for
  `aa-cis-dev-ingestion`.
- `gold_aa_internal.tenant_tour_versions` count (dashboard's "23 tenant rewrites") — queried
  but not cross-checked against a second source; out of scope for the A0/A1 question, noted
  only for completeness.

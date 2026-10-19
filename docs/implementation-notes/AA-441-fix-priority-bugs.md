# AA-441 — Fix 6 Priority Bugs from AA-438/439 Audit

**Post-merge update (23/08/2026):** PR #194 merged to `main` (`17305fc`) after CI green (Nghiep's
explicit go-ahead: "CI green thì merge luôn"). `Deploy Dev` workflow ran clean (Vercel + ECR build
+ Lambda + ECS Dev all `success`) — confirmed the running ECS task's image digest
(`sha256:cd4f6e77...`) matches ECR `:latest` exactly, task def `aa-cis-dev-api:122`. Migration 110
re-confirmed still applied post-deploy. With the fix now actually live, re-ran bugs #1, #2, #4,
#5, #6 as **true HTTP calls against the real deployed endpoint**
(`https://api-cis.lumiguides.it.com`) instead of direct function calls — strictly stronger
evidence than the pre-merge verification below, which is kept as-is for the record. See the new
"Post-deploy live HTTP verification" section at the bottom.

Branch: `feature/aa-441-fix-priority-bugs` (off `main` @ 950a159, post AA-438/439/440 docs merge)
Executed: 23/08/2026. Task prompt saved verbatim at
`docs/claude_tasks/AA-441-01-fix-priority-bugs.md` (gitignored, per repo convention for that dir
— not in this PR's diff).

All 6 bugs were pre-confirmed by the AA-438/AA-439 audits (code + live query) — no STEP0
re-investigation was done, per the task's explicit instruction. Each bug was verified
**independently** against the real dev DB/environment, not just unit-tested in isolation.

---

## Decisions

1. **Bug #1 (admin usage tracking) — schema design confirmed via AskUserQuestion, not guessed.**
   `shared.tenant_api_usage.tenant_id` is `NOT NULL` + FK'd to `shared.tenants` — cannot hold an
   admin identity. Chose: migration 110 makes `tenant_id` nullable, adds `actor_type`
   (`'tenant'|'admin'`) + `admin_user_id` (FK to `shared.admin_users`), and wires
   `rate_limit_middleware`'s new `/admin/*` branch to read the `x-admin-user-id` header — already
   sent by the admin BFF proxy (AA-232) but never read by any backend code until now. Rejected
   alternatives: reusing the aa_internal sentinel tenant_id (conflates real aa_internal
   tenant-portal traffic with staff traffic, no way to split back apart) and a separate
   `shared.admin_api_usage` table (clean but forces the dashboard query to UNION two tables for
   no real benefit here).
2. **Bug #4 (T0 upload) — scope expanded beyond the audit's literal wording, per Nghiep's
   explicit confirmation mid-session.** Investigation surfaced the bug was deeper than "401 +
   hardcoded tenant_id": the backend endpoint expected a JSON `{filename,content_type}` body and
   only ever returned a presigned S3 URL (a 2-step flow nothing ever completed the second half
   of), while the frontend always sent the real file as multipart FormData in one request — a
   full request-shape mismatch, not just an auth-routing bug. Nghiep's direction (chat, mid-task):
   fix all three layers this pass — proxy multipart support, frontend routing, AND backend
   request-shape (switch to direct multipart-to-S3, drop the unused presigned-URL contract) —
   but explicitly do NOT build the "AI extracts rules from the upload" step (no spec exists for
   it; tracked as a separate future item, no Linear issue created yet).
3. **Bug #5 (reject status) — `content_status_enum` already has a `'rejected'` value** (migration
   002) and `approve_review()` right above `reject_review()` in the same file already sets
   `generated_content.status` on its own claim — reject now mirrors that exact pattern instead of
   inventing a new status value or a different update shape.
4. **Bug #5 real-data fix — confirmed with Nghiep before touching production rows**, per the
   task's own instruction. Found the exact 2 real rows the audit flagged (review_id `2afbf069...`
   Seoul-Busan tour rejected 02/07/2026, `adaaeb35...` Ulaanbaatar tour rejected 21/08/2026),
   presented both generated_content_ids for confirmation, then applied the fix after explicit
   go-ahead.
5. **Bug #2 (Run Health) — per-run "stages" now sourced from `acp_shared.acp_v2_slots`** (real
   channel/kind/status/due_at/produced_at data) instead of left permanently empty. This goes
   slightly beyond "just swap the table" but was low-risk (same shape the endpoint already
   returns, `check_stage_slo()` safely no-ops on unrecognized v2 stage names) and avoids trading
   one kind of misleading-empty response for another. The v1-only hitl-gate/evaluator-score joins
   (`acp_hitl_requests`, `acp_silver_s4.blog_drafts`) have no v2 equivalent yet and were dropped
   rather than left querying tables that can structurally never match a v2 `run_id` — `country`
   filtering was also dropped (acp_v2_runs has no country column) but the query param is still
   accepted for FE backward-compat, just silently unused.

## Changed (vs. the literal audit wording)

- Bug #1: audit said "ghi `shared.tenant_api_usage` cho path `/admin/*`" — required a schema
  change (migration 110) to do so correctly, which the audit itself didn't specify (correctly
  left as a decision for this task, per its own "KHÔNG đoán, hỏi lại" instruction).
- Bug #2: the "table swap" is not a 1:1 column rename — `acp_v2_runs`'s schema is materially
  different from `acp_runs` (no country/cost/error_message, `tenant_id` is TEXT not UUID,
  `started_at` doesn't exist). Adapted field-by-field, documented inline in
  `api/routers/acp_health.py`'s docstring.
- Bug #4: scope grew from "fix the FE route" to "fix proxy multipart + FE route + backend request
  shape" — see Decision #2 above.

## Tradeoffs

- Bug #1: `admin_user_id` stays nullable (legacy `ADMIN_SECRET`-only callers with no BFF in front
  of them won't send `x-admin-user-id`) rather than forcing every admin call to resolve a user —
  matches how the codebase already treats that fallback path elsewhere (`_resolve_brand_tenant_id`
  has the same admin-secret-only fallback).
- Bug #2: `country` filtering and per-run LLM cost are just gone for v2 runs (no source of truth
  exists yet) rather than approximated via a slots→raw_tours join or a fabricated number — being
  visibly absent (`null`/`0.0` with an inline comment) was judged more honest than a guessed
  reconstruction.
- Bug #4: did not build the "AI extracts rules" parsing step BrandTab.tsx's UI copy already
  implies ("✅ Uploaded — AI extracting rules") — explicitly out of scope per Nghiep's direction,
  no spec exists for what it should extract or where it would write results.

## Should know (before reading the diff)

- `api/middleware/rate_limit.py`: the `/admin/*` branch (`_track_admin_call`) is a **separate
  function**, not a modification of the existing `/v1/*` rate-limit logic — no rate limiting is
  applied to admin traffic, only tracking.
- `shared/services/billing_service.py::track_api_call()` gained `actor_type`/`admin_user_id`
  kwargs with safe defaults (`actor_type="tenant"`) — every pre-existing call site
  (`rate_limit.py`'s `/v1/*` branch) is unchanged in behavior.
- `api/routers/admin_pipeline.py::upload_brand_file()` is a near-total rewrite (JSON+presigned-URL
  → multipart+direct-S3-put) — read it as a new function, not a diff of the old logic.
- `shared/repository/published_catalog_repository.py::insert()`'s `ON CONFLICT` now updates 18
  columns (was 4) — every column the INSERT sets except `id`/`tour_id` (the conflict key).
- `tests/unit/test_aa211_212_gate_hitl.py` was updated (not just left passing) — `reject_review()`
  now does a second `conn.execute()` and expects `generated_content_id` back from the claim
  `RETURNING`; the two affected tests' mocks were updated to match, not weakened.

---

## Verify evidence, per bug (all against the real dev DB via SSM tunnel to
the `aa-cis-dev-db` RDS endpoint, calling the actual modified Python
functions directly — not a re-implementation, not mocked)

### Bug #1 — Pipeline Health / admin usage tracking
- Applied migration 110 live (`shared.schema_versions` row confirmed).
- Called the real `rate_limit_middleware()` end-to-end (full ASGI `Request` → `_track_admin_call`
  → `track_api_call()`) with `X-Admin-Secret` + a real `shared.admin_users.id` as
  `x-admin-user-id`, path `/admin/dashboard`.
- Result: `shared.tenant_api_usage` gained a new row —
  `{tenant_id: None, endpoint: '/admin/dashboard', actor_type: 'admin', admin_user_id:
  c26f6938-2ae4-4977-b5e4-c185ff24981e}` — `admin_user_id` matches the real admin_users row used.
  Before: 0 admin-actor rows ever (table only ever had `actor_type` default `'tenant'` rows once
  the column existed).

### Bug #2 — Run Health reading dead table
- Confirmed live: `acp_shared.acp_runs` (old table) = 0 rows; `acp_shared.acp_v2_runs` (real
  table) = 12 rows — exactly matching the audit's "12 real runs" figure.
- Called the real `get_run_health()` handler directly (admin caller, no filters).
- Result: **12 runs returned** (was 0 before this fix, by construction — old code queried the
  0-row table). Sample row confirmed real fields (`run_id`, `tenant_id`, `status`, `started_at`,
  `completed_at`) and real per-slot `stages` data (2 stage entries with real `channel:kind`,
  `duration_seconds`, `status` from `acp_v2_slots`).

### Bug #3 — `published_tours` partial UPSERT
- Created a fully synthetic test tour (`raw_tours` + `generated_content`, name "AA-441 TEST
  TOUR" — not touching any real tour).
- Called the real `PublishedCatalogRepository.insert()` twice: 1st publish with one set of
  subtitle/SEO/highlights/etc, 2nd "republish" with entirely different values for all 18 fields.
- Result: after the 2nd call, **all 18 non-key columns matched the 2nd call's values** — none
  stayed stale from the 1st publish (explicitly checked `aa_name`, `aa_subtitle`, `aa_summary`,
  `aa_description`, `aa_highlights`, `aa_itineraries`, `mobile_card_text`, `seo_title`,
  `seo_meta`, `seo_keywords_used`, `og_tags`, `quality_score`, `s3_gold_path`, `approved_by`).
  Row count for the tour stayed at 1 (real upsert, not a duplicate insert).
- Synthetic test data fully deleted afterward (published_tours, generated_content, raw_tours rows
  removed; confirmed 0 remaining).

### Bug #4 — T0 Upload Brand Guide 401
- Minted a real tenant JWT for the real, active `test-n1-flow` tenant (`_create_jwt()`, the exact
  function tenant login uses).
- Called `_resolve_brand_tenant_id()` (the dependency `upload_brand_file()` now uses) with that
  JWT: resolved to `test-n1-flow`'s own `tenant_id` — **not** the hardcoded aa_internal UUID the
  old code always used, confirming the 401-causing hardcode/admin-only-proxy issue is gone for a
  real tenant JWT.
- Called `upload_brand_file()` directly with a real multipart file (`UploadFile`): request reached
  the backend, resolved the correct tenant, correctly built the S3 key
  (`brand-identity/6fbaf284-e3cd-4b4b-b53b-c9a04e8fae8e/...`) and reached the `s3.put_object()`
  call.
- The final `PutObject` itself failed under my personal debugging session specifically — my local
  script's ambient AWS credentials resolved to a **different AWS account** ([acc1 id],
  `pqnghiep-admin`) than the bucket's owning account ([acc2 id]); after forcing
  `AWS_PROFILE=aa365-admin`, it failed again on an MFA-credential-refresh limitation specific to a
  long-lived boto3 session object under an MFA-sourced profile (not reproducible via the ECS
  task's own IAM role, which needs no MFA and is what actually runs this code in production).
  Separately confirmed the exact `brand-identity/` prefix on that bucket IS writable under the
  `aa365-admin` account (`aws s3 cp` succeeded, then cleaned up) — isolating the failure to my
  local session's credential plumbing, not the app logic. **Not fully end-to-end S3-write
  verified** — flagged here rather than claimed; the tenant-id/401 fix (the actual bug) is fully
  verified.
- Frontend: `npx tsc --noEmit` on the whole `frontend/` project — **0 errors** (covers
  `BrandTab.tsx` and both proxy route files). The Next.js proxy's multipart pass-through was not
  exercised via a live browser/cookie session in this pass (out of scope — see Decisions).

### Bug #5 — Reject doesn't reset `generated_content.status`
- **Code fix**: created a synthetic review_queue + generated_content pair
  (`status='hitl'`/`review_status='pending'`), called the real `reject_review()` directly. Result:
  `review_status='rejected'` AND `generated_content.status='rejected'` (was staying at `'hitl'`
  before this fix). Confirmed the pre-existing AA-212 double-claim protection still works (2nd
  reject call → `409`). Synthetic rows deleted after.
- **Real-data fix** (confirmed with Nghiep first): found exactly 2 real rows matching the audit's
  claim (`review_status='rejected'` AND `generated_content.status='hitl'`) —
  `aeb14cca-6962-4a00-bf7d-87152b3456a4` (Seoul-Busan tour) and
  `31ac8c8c-cf40-44a1-9527-ad9d50056b37` (Ulaanbaatar tour). After the UPDATE: both rows show
  `content_status='rejected'`; live re-query of "rows where review_status='rejected' AND
  generated_content.status='hitl'" returns **0** (was 2).

### Bug #6 — `/v1/tours/pool` missing filter
- Created a synthetic published test tour (`master_status` default `'active'`).
- Called the real `browse_pool()` directly as a tenant caller, filtered by the test tour's unique
  name: **visible** while active.
- Set `master_status='trashed'` (simulating an admin trashing it in Master Content) → called
  `browse_pool()` again: **hidden** (empty result for that name).
- Reset to `active` + set `deleted_at=NOW()` (simulating soft-delete) → called `browse_pool()`
  again: **hidden** again — both gate conditions independently verified.
- Synthetic test data fully deleted afterward.

### Regression check
- `pytest tests/unit/ -v` (CI's exact invocation/env): **1359 passed, 0 failed** (2 pre-existing
  failures in `test_aa211_212_gate_hitl.py` were real regressions from the bug #5 fix — the
  mocks needed `generated_content_id` in the `RETURNING` — fixed by updating the mocks to match
  the new, correct behavior, not by weakening the assertions).
- `pytest tests/integration/` was **not** run locally — no Docker available in this environment to
  stand up the Postgres/Redis services CI's workflow provisions. Left to CI (no integration test
  in the suite touches any of the 6 changed code paths, confirmed via grep before relying on this).
- `flake8` (project's own `.flake8` config): 0 findings on all 7 changed Python files.
- `tsc --noEmit` (whole frontend project): 0 errors.

---

## Post-deploy live HTTP verification (23/08/2026, after merge)

All calls below hit `https://api-cis.lumiguides.it.com` directly — real HTTPS, real deployed
code (task def `aa-cis-dev-api:122`, image digest confirmed == ECR `:latest`), no local function
calls, no mocks.

- **Bug #1 + #2 together**: `GET /admin/acp/run-health?limit=50` with real `X-Admin-Secret` +
  `x-admin-user-id` → **200, 12 real runs** returned (bug #2). Re-queried
  `shared.tenant_api_usage` immediately after: newest row is `{tenant_id: null, endpoint:
  '/admin/acp/run-health', actor_type: 'admin', admin_user_id: c26f6938-...}` — this exact live
  call wrote the tracking row (bug #1).
- **Bug #4**: minted a real tenant JWT for `test-n1-flow` (verified against the deployed server's
  own `JWT_SECRET` — it accepted the token), then `POST
  /admin/brand-identity/upload` with `Authorization: Bearer <tenant JWT>` **only** (no admin
  secret at all) + a real multipart file → **200
  `{"status":"uploaded","s3_key":"brand-identity/6fbaf284-.../..."}`**. Confirmed via `aws s3 ls`
  the file actually landed under the tenant's own S3 prefix. This supersedes the pre-merge
  attempt above, which stalled on my local debugging session's AWS credentials — the real
  deployed backend (ECS task's own IAM role) has no such issue. Test file deleted after.
- **Bug #5**: created a fresh synthetic hitl row, `POST
  /v1/pipeline/review-queue/{id}/reject` with `x-admin-secret` → **200
  `{"status":"rejected",...}`**. Re-queried the DB: `review_status='rejected'` AND
  `generated_content.status='rejected'` (not stuck at `'hitl'`). Synthetic row deleted after.
- **Bug #6**: created a synthetic published test tour, `GET
  /v1/tours/pool?search=...` with a real tenant JWT → tour **visible**. Set
  `master_status='trashed'` via DB (simulating an admin trashing it), same live call again → tour
  **hidden**. Synthetic tour deleted after.
- Bug #3 was not re-run live post-deploy (already exercised the real repository code directly
  pre-merge, and re-publishing over live HTTP would require going through the full
  review/approve chain — no additional signal for the risk).

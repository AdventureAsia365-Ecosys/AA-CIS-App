# AA-436 STEP0 — T3 (Tenant QA Gate) tenant-facing UI investigation

Status: **investigate-only, no product code changed.** Only files touched: this report +
`docs/claude_tasks/AA-436-01-step0-investigate-t3-ui.md` (verbatim task prompt copy).

Investigated: 22/08/2026. All findings below are from source reads on `main` (working tree had
pre-existing unrelated uncommitted changes — see "Repo state" note at the bottom — none of them
touched by this session) plus live queries/calls against the real dev DB and the real running ECS
task, account `[acc2 id]`, region `us-west-1`, profile `aa365-admin`.

---

## 1. T3 route — does NOT exist yet, not even a placeholder

Confirmed by listing `frontend/app/(tenant)/portal/`:

```
portal/{dashboard,t0-brand,t1-rewrite,t4-pool,t6-atoms,api,activity,billing,settings}/page.tsx
```

No `t2-*`, `t3-*`, or `t5-*` folder exists anywhere under `portal/`. `Sidebar.tsx`'s `NAV1` array
(`frontend/app/(tenant)/portal/_components/Sidebar.tsx:24-31`) has exactly 6 entries — Dashboard,
Browse Pool (t1), My Catalog (t4), Brand Identity (t0), Atom Curation (t6, AA-431), API Access —
**no T3 entry, commented-out or otherwise.** `layout.tsx`'s `BREADCRUMBS` map is the same 9 routes,
no T3 key.

This contradicts the task's premise that a T3 route was "chừa sẵn theo convention." It was not —
AA-430's own implementation notes say so explicitly, twice:

- `docs/implementation-notes/AA-430-route-migration-tenant-portal.md` line 103-105: *"T2/T3/T5 chạy
  ngầm trong job T1 (không có UI riêng). T6 (`/portal/t6-atoms`) và `/portal/t8-produce` **chưa
  tạo** trong task này... AA-431 tự tạo route t6-atoms."*
- `frontend/app/(tenant)/portal/t4-pool/page.tsx:5-6` (comment, AA-430): *"Route slug confirmed
  against the ADR-2026-038 mapping — NOT T3 (T3 is the tenant-facing QA-failure view, which has no
  UI yet, see AA-430 implementation notes)."*

So T3 has zero FE footprint today: no folder, no nav item, no breadcrumb entry, no reserved slug
string anywhere in the frontend. **No exact route name for T3 is fixed in code.**

**Discrepancy with the Linear issue text, checked against the actual ADR (not assumed):** the
AA-436 Linear issue states *"ADR-2026-038 mục 10.4: route đã convention hóa `/portal/t3-review`"*.
Fetched the real Notion page (`3c3b8a41-ec5d-8123-911f-e0c308841e79`) directly to check this — §10.4
("Bảng scope FE cập nhật") is a status table (`Stage | Trạng thái thật | Việc cần`); its T3 row
reads *"Không tồn tại (chỉ có `quality_score` số) → Build mới — tenant xem diagnosis structured"*.
**The string `t3-review` does not appear anywhere in the ADR page** (§10.4, §11, or anywhere else)
— nor in the earlier `memory.md` S153 pointer that §11 defers detail to, which this session did not
have access to. So "`/portal/t3-review` is the ADR-convention name" is not something this audit
could verify from the source it was attributed to; it may be correct (a reasonable slug given the
`t0-brand`/`t1-rewrite`/`t4-pool`/`t6-atoms` pattern) but isn't *confirmed* by anything read this
session. AA-430's own decision log (line 5-6 of its implementation notes) states the team's
convention here is "ask Nghiep for the tab→T-number mapping instead of guessing" — that's how T4 vs
T3 got disambiguated last time (T4, not T3, for `catalog`). Recommend the same before the next build
task starts: confirm the exact slug with Nghiep rather than treating the Linear issue text as
settled, since it doesn't trace back to the ADR section it cites.

---

## 2. `review_queue` — write path (T3, real) vs. read paths (2 endpoints, neither T3-aware)

### 2a. Write path — real, already shipped (AA-425)

`services/acp_produce/tenant_pipeline.py::escalate_t3_failure()` (called from
`api/routers/v1_tours.py:378`, inside the tenant rewrite background task, after T3's QA gate
exhausts `TENANT_QA_MAX_REPAIRS=2`):

```python
# services/acp_produce/tenant_pipeline.py:156-190
INSERT INTO silver_aa_internal.review_queue
    (tour_id, tenant_id, tenant_tour_version_id, failure_summary, escalate_detail)
VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb)
```

`generated_content_id` is left NULL (migration 107 dropped its NOT NULL — a tenant-rewrite
escalate has no `silver_aa_internal.generated_content` row to point at; that table belongs to the
older N0-N6 admin pipeline). `escalate_detail` carries the `[{check_id, field, description,
source_span, suggested_fix}, ...]` array the ADR calls for.

### 2b. Read paths — 2 existing endpoints, both wrong for T3

**`GET /admin/review-queue`** — `api/routers/admin_pipeline.py:2161-2205`
- `verify_admin_secret(x_admin_secret)` — **admin-only**, no tenant JWT branch at all.
- `tenant_id = "00000000-0000-0000-0000-000000000001"` hardcoded (line 2169) — always the master/
  admin tenant, never derived from a caller.
- `INNER JOIN silver_aa_internal.generated_content gc ON gc.id = rq.generated_content_id` — a T3
  row's `generated_content_id` is NULL, so this JOIN silently drops every T3 escalation from the
  result set, regardless of the tenant_id issue above.
- SELECT list does not include `tenant_tour_version_id` or `escalate_detail` at all — even a T3 row
  that somehow survived the JOIN would come back with no usable payload.

**`GET /v1/pipeline/review-queue`** — `api/routers/v1_pipeline.py:364-401`
- Uses `_get_tenant` (`v1_pipeline.py:311-325`), which **does** accept a real tenant Bearer JWT
  (`_verify_jwt(credentials.credentials)`, returning the real `sub`) as well as an admin-secret
  fallback — this is the modern JWT-capable dependency, correctly wired as far as auth goes.
- But the endpoint body never uses the resolved tenant — line 373-375: *"AA-229: CIS is
  single-tenant; review_queue rows are enqueued under master (`_MASTER_TENANT_ID`). Pin fetch to
  master..."* then hardcodes `tenant_id = "00000000-0000-0000-0000-000000000001"` again (line 376),
  discarding whatever real tenant the JWT resolved to.
- Same `INNER JOIN generated_content` (line 389) → same NULL-FK exclusion of T3 rows.
- Same missing `tenant_tour_version_id` / `escalate_detail` columns.

**Net: there is currently no endpoint that returns a real tenant's own T3 escalations.** Both
existing `review_queue` readers are relics of the pre-multi-tenant "CIS is single-tenant" era
(AA-229 comment) and would need either a real fix or — more likely, since they also serve the
unrelated N0-N6 admin HITL flow (approve/reject with `step_fn_task_token`, `human_edited`,
`revalidate_passed` etc., none of which apply to a T3 escalate row) — a **new, dedicated endpoint**
rather than retrofitting either of these two.

One more schema note, lower severity: `api/migrations/008_rls_gold_silver.sql` documents
`silver_aa_internal.review_queue` as having no RLS policy (comment: *"raw_sources and review_queue
excluded (no tenant_id column)"* — stale as of migration 002, which already had `tenant_id NOT
NULL`, but RLS genuinely was never added for this table). So tenant isolation on `review_queue` is
100% application-query-level (`WHERE tenant_id = ...`) with no DB-level backstop — worth keeping in
mind for whatever new endpoint gets written, though not itself part of this task's scope.

---

## 3. Live evidence — real DB rows + real endpoint calls

Queried the real dev DB directly (S3-mediated ECS exec, `silver_aa_internal.review_queue`):

```
TOTAL review_queue rows: 52
T3-style rows (tenant_tour_version_id NOT NULL): 11
```

One real T3-escalated row (`generated_content_id` confirmed NULL, real `escalate_detail` payload):

```json
{
  "id": "eeb3b57f-e994-4b64-a65b-f517ec3b6ce6",
  "tour_id": "66ebe919-3bbc-423a-a695-005ddf53781f",
  "tenant_id": "9fb0a3db-59aa-468a-a082-ded01ac50bee",
  "generated_content_id": "None",
  "tenant_tour_version_id": "fb2e4e44-e450-4161-ba7f-30c38d96f2d3",
  "review_status": "pending",
  "failure_summary": "T3 QA failed after 2 repair attempt(s) — 3 structural, 0 grounding issue(s)",
  "escalate_detail": [
    {"field": null, "check_id": "structural:FORBIDDEN_WORD", "description": "FORBIDDEN_WORD", "source_span": null, "suggested_fix": null},
    {"field": null, "check_id": "structural:SEO_TITLE_TOO_LONG", "description": "SEO_TITLE_TOO_LONG", "source_span": null, "suggested_fix": null},
    {"field": null, "check_id": "structural:META_TOO_SHORT", "description": "META_TOO_SHORT", "source_span": null, "suggested_fix": null}
  ],
  "created_at": "2026-08-21 15:00:23.148752+00:00"
}
```

Note the real shape is sparser than the ADR's nominal `{check_id, field, description, source_span,
suggested_fix}` implies: `field`/`source_span`/`suggested_fix` are only ever populated for
`grounding:*` check_ids (see `tenant_pipeline.py:169-176`) — `structural:*` entries (the majority in
this sample) carry `field=None, source_span=None, suggested_fix=None` always, just a bare code +
its own name as `description`. A T3 UI showing "suggested fix" per-item will be empty for most rows
today, not a rendering bug.

The tenant that owns these 11 rows (`9fb0a3db-59aa-468a-a082-ded01ac50bee`) is `Test Agency`
(`slug=test-agency`, `plan_tier=starter`, **`is_active=false`**, created 30/04/2026) — an existing
internal test tenant, not a live customer; these rows are very likely AA-425's own verification
exhaust, not cleaned up.

**Called the real endpoint** (in-container, `http://localhost:8000`, bypassing the API Gateway per
AA-430/432's already-documented gateway quirk — this is FastAPI's own auth, unaffected by that
issue) with a JWT minted for that exact tenant (`api.routers.auth._create_jwt`, no DB writes, no
fake data — same real tenant/rows as above):

```
GET /v1/pipeline/review-queue?page_size=20
Authorization: Bearer <real JWT, sub=9fb0a3db-...>
→ HTTP 403
{"detail":"Tenant is deactivated"}
```

That 403 is `api/middleware/rate_limit.py`'s `is_active` gate (AA-432) working exactly as designed
— not a bug, just confirms this specific tenant can't be used for a full end-to-end live check
without either reactivating it or seeding a fresh active test tenant (Nghiep's call, not done here
per "không tạo data giả").

For comparison, called both endpoints with the admin secret (staff view):

```
GET /admin/review-queue?status=all           → 200, 20/N rows, none are the 11 T3 rows (JOIN drops them)
GET /v1/pipeline/review-queue (admin branch)  → 200, total=37 (master tenant, pending only), none are the 11 T3 rows
```

Confirms §2's read-path analysis empirically, not just from reading the SQL: **the 11 real T3
escalations are invisible through every endpoint that exists today**, admin or tenant.

---

## 4. FE reuse — T4 (`CatalogTab.tsx`) vs. T6 (`AtomsTab.tsx`)

Both are plain `fetch()` + `useState`/`useEffect`/`useCallback` — **no SWR, no React Query**
anywhere in this portal. Every tenant-portal API call goes through the same-origin BFF proxy
`frontend/app/api/tenant/[...path]/route.ts`, which attaches `Authorization: Bearer
<cis_tenant_token>` server-side from the httpOnly cookie (AA-427) — no component ever handles the
JWT itself.

**`frontend/app/(tenant)/portal/_components/AtomsTab.tsx` (190 lines, T6, AA-431) — closer match
for T3, recommend as the template:**
- Shape: summary stat row (`GET .../summary`) + filterable flat list + "Load More" pagination
  (`GET .../atoms?limit=&offset=&...`), no per-tour grouping, no bulk actions, no detail panel.
- Structurally almost identical to what T3 needs: a summary (count of pending escalations?) + a
  flat list of escalate rows, each showing `failure_summary` + the `escalate_detail` array as
  badges/lines, filterable by `review_status`.
- Uses this portal's own `ui.tsx` tokens (`Card`, `Badge`, `Btn`, `LoadingScreen`, `EmptyState`) —
  not `adminUi.tsx`. AA-431's own notes are explicit that this distinction is deliberate portal-wide
  convention, not incidental.

**`frontend/app/(tenant)/portal/_components/CatalogTab.tsx` (711 lines, T4) — heavier, only useful
for one pattern:**
- Full edit UI (name/subtitle/summary/highlights/SEO fields), version history, approve/reject
  actions, polling (`pollingRef`), multi-select export — none of this applies to a read-only
  QA-failure list.
- Only transferable piece: the master/detail split (`list` + `selected`/`detail` state, a
  side-panel that fetches full detail on click) — worth it only if a T3 row needs a "compare
  before/after" or "view the version's full content" drill-down. Given `escalate_detail` is
  already a self-contained structured array (no separate detail fetch needed to explain *why* it
  escalated), **AtomsTab's flat-list pattern alone is probably sufficient** — recommend not
  pulling in CatalogTab's heavier detail-panel machinery unless Nghiep specifically wants a
  content-diff view.

---

## 5. Conclusion — this is "FE + backend," not "FE-only," same shape as AA-431

Backend is **not** ready as-is. Recommend following AA-431's exact precedent (backend fix + FE, one
PR, gated on `_resolve_atom_owner_scope()`-style real JWT derivation — see
`api/routers/admin_atoms.py:86`) rather than retrofitting either existing `review_queue` endpoint:

- **New backend endpoint** (not a patch to `admin_pipeline.py`'s or `v1_pipeline.py`'s
  `/review-queue` — both are wired to the older N0-N6 admin HITL flow with actions/columns that
  don't apply to T3 rows, and retrofitting risks breaking that flow). Something like `GET
  /v1/tours/qa-escalations` (naming Nghiep's call), tenant-JWT-only (mirror `get_tenant()` from
  `v1_tours.py:19`/`v1_exports.py:15` — no admin-secret fallback needed, this is tenant-only), real
  `WHERE tenant_id = tenant["sub"]`, and either `LEFT JOIN gold_aa_internal.tenant_tour_versions`
  (T4's own table, for tour name/content context) or no join at all — never `INNER JOIN
  generated_content_id` for this query, since it's NULL by design for every T3 row.
- **New FE route** `/portal/t3-review` (per the Linear issue text — confirm with Nghiep first, see
  §1's discrepancy note; this audit could not independently trace that exact string to the ADR) + a
  new component modeled on
  `AtomsTab.tsx`'s flat-list-with-summary pattern, wired through the existing `/api/tenant/*` proxy
  (no proxy change needed — it already forwards Bearer for any `/v1/*` path).
- **Open question for Nghiep, not assumed here**: what action (if any) a tenant can take on an
  escalated row. AA-425's own implementation notes and the ADR excerpt in the task context describe
  only the write side (self-repair → escalate); nothing in the codebase defines a tenant-reachable
  "resolve/retry/dismiss" action on a `review_queue` row today (the existing `approve`/`reject`
  endpoints in `v1_pipeline.py` are the old N0-N6 flow, gated on `generated_content_id`/
  `step_fn_task_token`, and don't fit a T3 row's shape either). If T3 is meant to be more than a
  read-only "here's why it failed" view, that's a second backend surface not yet designed.

---

## Repo state note (not part of this investigation, flagging so it isn't mistaken for AA-436 fallout)

`main` had pre-existing uncommitted local changes when this session started (not made by this
session): deleted `AA45_S3_SPEC.md` and `docs/CIS_Runbook_v1.md:Zone.Identifier`, modified
`frontend/app/(tenant)/portal/layout.tsx` and `services/acp_shared/grounding.py`, untracked
`kirocli/`. `AA-CIS-App/handoff.md` (S154-ish entry, AA-427 session) already flags this same WIP as
"not mine to resolve." Left entirely untouched by this session — only the 2 new doc files below
were added/committed.

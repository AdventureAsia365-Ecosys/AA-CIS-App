# AA-437 STEP0 — A4 Cross-Tenant Oversight: investigate-only

Status: **investigate-only, no product code changed.** Only files touched this session: this
report + `docs/claude_tasks/AA-437-01-step0-a4-investigate.md` (verbatim task prompt copy).
Branch `feature/aa-437-a4-step0-investigate`, committed locally, **not pushed** — per the task's
own instruction, this stays local until a build task (AA-437-02) is ready to fold in.

Investigated: 23/08/2026. Findings below are from source reads on `main` (post AA-443 merge)
plus live read-only queries against the real dev DB (`005097885195`, `us-west-1`,
`aa365-admin`, via SSM port-forward to the RDS instance — no ECS/app code touched, no writes).
ADR-2026-038 fetched directly from Notion (`3c3b8a41-ec5d-8123-911f-e0c308841e79`), not assumed
from memory. Linear AA-255→259, AA-436, AA-437, AA-434 read directly, not paraphrased from the
task prompt.

---

## 1. `review_queue` — full real schema + real T3 rows

```
silver_aa_internal.review_queue (15 columns):
  id                      uuid        NOT NULL  default gen_random_uuid()
  tour_id                 uuid        NOT NULL
  generated_content_id    uuid        NULL      -- N0-N6 admin HITL rows only
  tenant_id               uuid        NOT NULL
  failure_summary         text        NULL
  score_overall           numeric     NULL
  step_fn_task_token      text        NULL       -- legacy Step Functions path, N0-N6 only
  step_fn_execution_arn   varchar     NULL
  review_status           enum        NOT NULL  default 'pending'
  reviewer_notes          text        NULL
  reviewed_by             varchar     NULL
  reviewed_at             timestamptz NULL
  created_at              timestamptz NOT NULL  default now()
  tenant_tour_version_id  uuid        NULL       -- T3 rows only (AA-425)
  escalate_detail         jsonb       NULL       -- T3 rows only (AA-425)
```

Live counts (unchanged from AA-436's 22/08 numbers — no new T3 escalations since):
```
total rows: 52
T3-style (tenant_tour_version_id NOT NULL): 11
N0-N6-style (generated_content_id NOT NULL): 41
```
All 11 T3 rows belong to **one tenant**: `9fb0a3db-59aa-468a-a082-ded01ac50bee` (`Test Agency`,
slug `test-agency`, **`is_active=false`**) — same tenant AA-436 already flagged as very likely
AA-425's own verification exhaust, not a live customer. All 11 are `review_status='pending'`
(nobody has ever acted on one).

**Real `check_id` distribution across all 11 rows** (`jsonb_array_elements(escalate_detail)`,
grouped) — this is the concrete "repeated pattern" example the ADR's use case describes:
```
structural:FORBIDDEN_WORD                 11/11  ← every single row
structural:META_TOO_SHORT                  5/11
structural:SEO_TITLE_TOO_LONG              3/11
grounding:novel_numeric_claim              1/11
structural:MISSING_FIELD                   1/11
structural:ITINERARY_DAY_COUNT_MISMATCH    1/11
```
`FORBIDDEN_WORD` firing on literally every row is exactly the "prompt/brand-rule hệ thống sai"
signal the ADR describes. **Caveat**: since all 11 rows are one tenant, this is currently a
same-tenant repeated pattern, not yet a demonstrated *cross-tenant* one — there is no live data
today showing the same check_id firing for two different tenants (only one tenant has any T3
rows at all).

**A basic `GROUP BY tenant_id, check_id` (via `jsonb_array_elements(escalate_detail)`), run
directly against `review_queue`, is enough to surface this today — confirmed by running exactly
that query above. No new aggregate table/view is needed for step 5(c)'s pattern-detection use
case at current data volume (52 rows total).** If volume grows into the thousands, a materialized
view would be a performance optimization, not a correctness requirement — not needed to ship a
first version.

### Important timing gap: no live example of the *new* auto-pass flow yet

`gold_aa_internal.tenant_tour_versions.qa_auto_passed` (AA-436's migration) exists live, boolean,
default `false` — confirmed. **But 0 rows currently have it `=true`.** All 11 real T3 rows in
`review_queue` were written *before* AA-436 shipped (dated 21/08, AA-436 merged 22/08 14:45) —
they're artifacts of the *old* escalate-block semantics, not examples of a tour that hit 2 failed
repairs, got `qa_auto_passed=true`, and continued into T4/T5 under the *new* behavior. **A4's build
task will have zero live rows to test against that actually exercise the new write path
end-to-end** — worth flagging to Nghiep; either accept building against the old rows' shape
(the JSON schema is identical either way, so this is low risk) or trigger one real tenant rewrite
that intentionally fails QA twice to get a fresh, representative row before/while building.

### Old read endpoints — re-confirmed still broken for T3, unchanged since AA-436

Re-ran the live check: the old `INNER JOIN generated_content` (`admin_pipeline.py`'s
`/admin/review-queue` and `v1_pipeline.py`'s `/v1/pipeline/review-queue`) still excludes **0 of
11** T3 rows survive that join — confirmed live, not just re-read from code. AA-436's
recommendation to build a **new, dedicated endpoint** (not retrofit either existing one) still
stands; nothing has changed here since yesterday.

RLS note (unchanged from AA-436): `review_queue` still has no RLS policy — tenant isolation is
100% application-query-level. Worth keeping in mind for the new endpoint (must filter
`WHERE tenant_id = ...` correctly, no DB-level backstop), not a blocker.

---

## 2. Trust Ramp — schema + code, re-verified for the A4 use case (not T8)

- Ramp state lives on **`acp_deliver.packets.publish_mode`** (migration 094) — **per-packet, not
  per-tenant**. A tenant's "current ramp level" is not a single stored value anywhere; it's
  whatever the tenant's packets' `publish_mode` values happen to be. In principle two packets for
  the same tenant (different weeks) could sit at different modes if approved separately — nothing
  in the code prevents that. **Real, unresolved design question for a Trust Ramp dashboard**: what
  does "tenant X is at level Y" mean when ramp state is packet-scoped? (latest packet's mode? most
  common? worst-case/lowest?) — not decided anywhere in the ADR or code.

- **`suggest_ramp_transition(current_mode, engagement_ok, weeks_active)`** — pure function, no DB
  access, confirmed **zero real callers** repo-wide (only `tests/unit/test_aa365_trust_ramp.py`
  exercises it directly) — re-verified today, unchanged since AA-439-06. Its 2 inputs
  (`engagement_ok`, `weeks_active`) are **not computed anywhere else in the codebase either** —
  confirmed via grep, 0 hits outside `trust_ramp.py` and its own test file. If A4 (or anything)
  wants to actually surface "this tenant should ramp up next," those 2 metrics need to be defined
  and computed from scratch — this is not a small wiring gap, it's undesigned.

- **`confirm_ramp_transition()`** — real, live, called from exactly one place:
  `POST /admin/produce/packets/{packet_id}/gate-c/approve` (`admin_produce.py:407-452`),
  admin-secret-gated (staff manual click only, via the existing `/admin/produce` Gate C UI —
  `frontend/app/admin/produce/page.tsx`). Always writes to `acp_shared.audit_log`
  (`action='publish_mode_transition'`) whether the transition succeeds or is BOFU/F6-blocked.

- **Live data, freshly queried (not from AA-439-06's older numbers):**
  ```
  acp_deliver.packets: only ONE tenant has ANY packets at all —
    tenant_id=00000000-0000-0000-0000-000000000001 (aa_internal/master), 4 packets,
    ALL 4 still at publish_mode='propose_only' (the lowest ramp level, never advanced).

  acp_shared.audit_log WHERE action='publish_mode_transition': 0 rows. Still zero —
    re-confirmed, unchanged since AA-439-06. Not one ramp transition has ever been
    confirmed, manually or otherwise, in this system's history.
  ```
  **This is the single most important finding for scoping A4's Trust Ramp half**: there is
  currently **no real B2B tenant with any packet at all**, let alone a ramp history to show. A
  "Trust Ramp dashboard" built today would show one row (the internal tenant) permanently at the
  lowest level. This doesn't mean don't build it — it means the dashboard's value is currently
  latent (waits on real tenants running N7/N8), not immediately demonstrable, which changes the
  urgency case relative to the `review_queue` use case (which has live data today, if thin).

- ADR-2026-038 §0.5 (22/08) confirms Trust Ramp is a **keep, not a T8 leftover to delete** — it's
  chị Thư's original design (verified AA-439-07), reframed as a "new tenant probation" safety
  mechanism, not a content gate. §0.5 also explicitly flags the `suggest_ramp_transition()` wiring
  gap above as a known TODO, but **does not decide the mechanism** ("cron/job định kỳ, hoặc trigger
  khi packet mới được tạo" — both floated, neither chosen). This is a real open decision point for
  Nghiep, not something this investigation should resolve.

---

## 3. Command Center backlog (AA-255→259) — confirmed scope, confirmed 0% built

Read all 5 issues directly (not the task prompt's paraphrase):

| Issue | Title | Status | Real scope |
|---|---|---|---|
| AA-255 | Unified Admin Dashboard (parent) | Backlog | Infra + LLM ops + Cost, explicitly **not** Prometheus/Grafana — reuses existing DB columns/CloudWatch |
| AA-256 | [1/4] AWS Cost Explorer integration | Backlog | Cost breakdown by service/day |
| AA-257 | [2/4] Live infra state endpoint | Backlog | ECS/RDS/NAT/Redis health via boto3 |
| AA-258 | [3/4] LLM ops rollup | Backlog | Model tier usage, fallback rate, cost/run — **not tenant-content-escalation data** |
| AA-259 | [4/4] Frontend Command Center page | Backlog | `/admin/command-center`, 4 tabs: Infra / CIS Pipeline / ACP Pipeline / Cost |

**Confirmed: Command Center's scope is infra/cost/LLM-ops observability — zero overlap with
content/tenant-oversight (review_queue, escalate_detail, ramp state).** None of the 4 tabs in
AA-259's own spec mention `review_queue`, T3, or ramp transitions. This is not a judgment call —
it's what the issue text itself says.

**Confirmed: 0% built.** All 5 issues are `Backlog` status. No `/admin/command-center` route
exists (`grep -rl "command-center"` across `frontend/` and `api/` → 0 hits; no such directory
under `frontend/app/admin/`). The ADR's repeated instruction to "nối vào Command Center" (§0.1,
§10.3, §10.4, §11.2) means **join its future roadmap slot**, not **reuse existing code** — there
is no existing code there to reuse. This matters: the ADR's phrasing could be misread as "some
Command Center infrastructure already exists that A4 should plug into" — it does not.

**One concrete reuse point that *does* exist, independent of Command Center**: AA-259's own spec
says *"Style/component tái dùng từ `admin/run-health` đã có (polished reference UI) — không tạo
design pattern mới."* `/admin/run-health` (AA-141) is real, live, and already the acknowledged
visual/structural template for exactly this kind of admin monitoring page — this is a genuine,
already-decided precedent A4 can follow, separate from Command Center's own (unbuilt) code.

**Related, also unresolved**: AA-434 (STEP0, referenced by AA-255's relations) asked "is there
already a per-tenant LLM usage breakdown?" — still `Backlog`, not investigated yet. So Command
Center's own LLM-ops tab (AA-258) has an open prerequisite question of its own; nothing there is
usable as a foundation for A4 either.

---

## 4. Existing `/admin/*` FE routes — checked for near-duplicates

Full list of existing admin routes (`frontend/app/admin/*`):
```
_components, atomize, brand, curation, dashboard, marketplace, master-content,
pipeline, produce, quarter-plan, review, run-health, s1-rewrite, settings, tenants, upload
```

Two worth calling out specifically for A4:

- **`/admin/review`** (AA-234/241) — confirmed by reading its own header comment: this is the
  N0-N6 HITL review/edit/approve/reject UI, consuming the *old* `/admin/review-queue` endpoint.
  Same conclusion as AA-436: **not** a T3/A4 fit, don't retrofit it — different action set
  (approve/reject/revalidate) that doesn't apply to a read-only T3 escalation row.
- **`/admin/tenants`** (AA-159/AA-389) — a real per-tenant listing page (lifecycle stats, country,
  onboarding tab). Not an oversight dashboard itself, but a plausible **link target** ("click
  tenant X → see their T3 escalations") rather than something to build inside.
- **`/admin/produce`** (AA-405/412) — already has the *only* live Trust Ramp UI that exists
  (Gate C packet list + approve button, calling `confirm_ramp_transition()`). Not tenant-oversight
  shaped (it's a per-packet action queue, not a per-tenant ramp-level view), but it's the closest
  existing precedent for "what does a ramp UI look like in this codebase" — worth reading before
  designing a new Trust Ramp *dashboard* view, so the two don't diverge in look/data shape
  needlessly.

No route anywhere named or shaped like "oversight," "cross-tenant," or "A4" — confirmed via grep,
0 hits. Nothing to accidentally duplicate.

---

## 5. Real gaps to build — summary

**(a) New backend endpoint(s) needed:**
1. A **new** admin-only, read-only endpoint over `review_queue` filtered to
   `tenant_tour_version_id IS NOT NULL` (T3 rows) — NOT a patch to either existing
   `/admin/review-queue` or `/v1/pipeline/review-queue` (both wired to the incompatible N0-N6
   action flow, per AA-436 and re-confirmed live above). Needs: filter by `tenant_id`, and
   either a `GROUP BY check_id` aggregate mode or raw list mode (or both) to serve the
   pattern-detection use case. No new columns/tables required for this at current volume (see §1).
2. (Trust Ramp half, if built now) An endpoint reading `acp_deliver.packets` joined to
   `shared.tenants`, showing current `publish_mode` per tenant — trivial query, but see §2's open
   question on what "current ramp level" means when state is per-packet, not per-tenant.
   `suggest_ramp_transition()`'s 2 inputs are undefined — a dashboard could show *current state*
   without needing them; showing a *suggested next state* would require designing
   `engagement_ok`/`weeks_active` from scratch first (real scope-in for a later task, not this one).

**(b) New FE page/route needed:**
- A new `/admin/*` route (naming Nghiep's call — `/admin/oversight`? Nothing reserves a name
  today). AA-259's `run-health`-style pattern is the confirmed template to follow (already
  Nghiep-endorsed via AA-259's own text, independent of Command Center's build status).
- **AA-437's own scope note is decisive here and should anchor the build task**: it explicitly
  excludes flag/suspend/force-unpublish ("Không thuộc scope issue này") — the first version is
  **read-only**. This significantly shrinks the FE surface: a filterable table/list (tenant ×
  check_id × count, or a flat escalation list), no action buttons, no confirm dialogs.

**(c) New columns/tables?** No — confirmed above (§1) that `GROUP BY` over the existing JSONB
column is sufficient for the review_queue use case at current volume. Trust Ramp half needs no
new schema either (state already lives on `packets.publish_mode`); only new *code* (a
tenant-level rollup query) if that half gets built now.

---

## 6. Open decision points for the build task (AA-437-02) — not resolved here

Following AA-436's STEP0 precedent: options laid out, no architecture decided.

1. **Scope: both use cases in one build task, or split?** AA-437's own Linear text is scoped to
   *only* the `review_queue`/T3 use case ("use case đầu tiên"), explicitly deferring
   flag/suspend/force-unpublish. Trust Ramp dashboard isn't explicitly in AA-437's text either —
   it's in the original task prompt's context, sourced from the ADR more broadly. Given §2's
   finding (only 1 tenant, the internal one, has any packet at all — zero real B2B ramp data
   today), there's a real argument to **ship the review_queue half first** (has live data, has an
   explicit Linear scope) and treat Trust Ramp dashboard as a fast-follow once real tenants have
   packets to show — vs. building both now so the page doesn't need a second pass later. Nghiep's
   call.
2. **Trust Ramp "current level per tenant" semantics** (§2): latest packet's mode, or something
   else, given state is per-packet not per-tenant? Needs a decision before that half can be built
   at all, independent of the scope question above.
3. **Route name and exact URL** — nothing reserves `/admin/oversight` or any other slug; same
   "confirm with Nghiep before hardcoding" pattern AA-436 already established for `/portal/t3-review`
   (which turned out not to exist either).
4. **Endpoint shape for the pattern-detection view**: raw list (tenant + row + escalate_detail,
   client aggregates) vs. server-side `GROUP BY check_id` aggregate endpoint vs. both. Low-stakes,
   but affects FE complexity — a raw list matches `AtomsTab.tsx`'s already-proven flat-list
   pattern (AA-436's own FE recommendation) most directly; an aggregate would be a new shape.
5. **Whether to trigger one real tenant QA-fail-twice run** before/during the build task, so there's
   at least one live row exercising the *new* `qa_auto_passed=true` write path (§1's timing gap) —
   or accept building/testing against the 11 pre-AA-436 rows, whose JSON shape is identical anyway.

---

## Repo state note

Working tree was clean on `main` (post AA-443 merge) when this session started — no leftover WIP
found this time, unlike the last several sessions. Only this report + the task-prompt copy were
added, both committed locally on `feature/aa-437-a4-step0-investigate`, not pushed.

# AA-501 STEP0 — New pre-T11 review screen: investigation (no build)

Date: 30/08/2026. Worktree: `aa-501-step0` (docs/aa501-step0-investigation), branched off
`main` @ `695e1d2` (post AA-469 Việc 5, PR #263). All findings below are from reading the live
repo checkout directly — no skill (`aa-cis-schema`/`aa-ecosys-repos`, both stamped 27/08/2026)
was trusted for schema/route facts; every claim was re-verified against the file/migration cited.

## 0. One-line summary

The data AA needs (status/gate/retry/held-reason) already exists and is already exposed via
`GET /admin/a4/content-log` (AA-469 Việc 5) — it needs small additions, not a new page. The data
Tenant needs (full content + full context) is mostly *derivable* but not currently *assembled*
anywhere for tenant consumption without leaking gate/error detail — this needs one new tenant
endpoint and one new tenant page. The single hardest gap is DFS/PAA: it was **never persisted**,
only used transiently in the T8 angle-generation prompt, so a "what the tenant actually saw"
replay is not literally possible today — see §1.4.

## 1. Schema — confirmed live, post-AA-494 (Decisions 1–3) + AA-497 + AA-469 Việc 4

Read directly: migrations 113–126 (`api/migrations/`). Ordered by what changed and why it
matters for this screen.

- **113** (`angle_gate_request` / `angle_gate_option`, AA-449): `angle_gate_request` = one row
  per (atom_id, [channel at the time]) request — `atom_id` (TEXT), `trip_id` (UUID, nullable —
  an atom need not belong to a trip), `channel`, `goal`, `status`. `angle_gate_option` = exactly
  3 child rows (`name`, `why_it_works`, `formula_fit`, `best_final_style`, `recommended`,
  `chosen`).
- **114**: `angle_gate_request.cta`.
- **115** (`content_piece`, AA-450): one row per *write attempt* — `content_text`, `status`
  (`approved`/`held` originally), `held_reason`, `gate_ledger` (JSONB), `repair_log` (JSONB),
  `attempt_number`, FK `angle_gate_request_id`. Deliberately **no** denormalized `channel`/
  `goal`/`cta`/`angle_name` at this point — caller was expected to JOIN back to the parent.
- **118** (AA-466): `content_piece.status` gains `processing` (in-flight placeholder, from the
  202-Accepted-plus-poll rework) and `failed` (background task crashed before producing content —
  distinct from `held`, which is a real reviewed-and-blocked outcome).
- **124** (AA-494 Decisions 1–5, **this is the "post-AA-494" schema the task asked me to
  confirm**): `content_piece` gains — **`angle_gate_option_id`** (FK, nullable, which of the 3
  options this piece was actually written from — denormalized because `chosen` on the parent is
  mutable), **`channel`** (denormalized copy, nullable), **`content_summary`** (LLM-written 1-2
  sentence summary, for a *future* prior-pieces-context feature — column exists, **nothing
  populates it yet**), **`content_embedding`** (`vector(1536)`, for a *future* similarity check —
  column exists, **nothing populates it yet**, confirmed 0 embedding call sites in the repo).
  `angle_gate_request.status` CHECK gains `'reusable'` (value only; the transition trigger is not
  wired — see AA-497 below, which supersedes this with real code).
- **125** (AA-497): dropped `content_piece`'s `UNIQUE(angle_gate_request_id, attempt_number)` —
  **a request can now have more than one `content_piece` row over time** (reopen → re-choose
  angle → write again). Consequence for this screen: "the piece for this request" is not a 1:1
  lookup by `angle_gate_request_id` — must pick the latest by `created_at` (confirmed no code
  anywhere relies on `attempt_number` for this ordering; `attempt_number` is purely T9's internal
  1-2 retry counter within one write session, not a request-scoped sequence).
- **126** (AA-469 Việc 4, same-day as PR #263): `angle_gate_request.channel` is now **nullable**
  and is set at a *new* step 8 (`set_channel()`, **after** angle choice), not at request creation.
  `create_request()` no longer takes `channel` as a param.

### 1.1 Where does `channel` actually live now? (memory flagged this as needing re-verification)

**Both**, at different times, and the memory's instinct was right that it moved but not fully —
confirmed by reading `services/acp_content_writing/service.py:150-167`:

- `angle_gate_request.channel` is the *pre-write* value — set once by the tenant at workflow
  step 8, **before** `start_write()` is callable at all (`start_write()` hard-guards on it being
  non-NULL). It can be **re-set** on a `reusable`/`approved` request before the *next* write
  session (a request is not one-channel-for-life).
- At the moment a `content_piece` row is inserted, `req["channel"]` is copied into
  `content_piece.channel` — this copy is what makes a written piece's channel immutable even if
  the parent request's channel is later changed for a future write. **This is populated by app
  code now** (as of AA-469 Việc 4) — migration 124's own comment ("not yet populated") is stale,
  correctly superseded.
- Every real read site (`v1_publish.py`'s two queries, `admin_a4.py`'s content-log) already reads
  `COALESCE(cp.channel, agr.channel)` for exactly this reason — **the new screen must follow the
  same COALESCE pattern**, not read either column alone.

### 1.2 `angle_gate_option` — enough to show "which angle" to Tenant?

Yes, directly. `content_piece.angle_gate_option_id` → `angle_gate_option` row gives
`name`/`why_it_works`/`formula_fit`/`best_final_style` verbatim — no re-derivation needed. One
join subtlety (AA-497's own lesson, already applied in `v1_publish.py`): join on
`ago.option_id = cp.angle_gate_option_id` **first**, falling back to `ago.chosen = true` only
for the ~0 pre-AA-497 rows where `angle_gate_option_id` is NULL — never join on `chosen = true`
alone, since a reopen can re-point `chosen` after the piece was written.

### 1.3 `acp_contract.tour_atoms` — atom-level context for Tenant

Confirmed live shape (migration 079, unchanged since): `text`, `activity_type`,
`emotional_hook`, `visual_potential`, `persona_fit` (JSONB), `season_note`, `distinctiveness`,
`media` (JSONB). All of this is real, already fetched by `_fetch_atom_for_tenant()` inside
`acp_angle_gate/service.py` at goal-set time — it is just never carried forward into a
tenant-facing "review what you wrote" read. Tour/destination name comes via `trip_id` →
the existing trip-registry fetcher (`fetch_tenant_trips()`), same one `set_goal_and_generate()`
already uses — **only present when the request has a `trip_id`** (tripless atoms have no
tour/destination to show, by design, not a bug).

### 1.4 DFS/PAA — the real gap (highest-value finding of this STEP0)

`silver_aa_internal.seo_context.people_also_ask` / `.related_keywords` are real columns
(migration 069, AA-218) and **are** read at angle-generation time — but only as a **transient
prompt input**, never persisted anywhere:

- `services/acp_shared/dfs_relevance.py::fetch_search_demand_signal(trip_id, pool)` queries
  `seo_context` live, builds a `SearchDemandSignal(relevance, people_also_ask, related_keywords)`.
- `set_goal_and_generate()` (`acp_angle_gate/service.py:246-254`) calls this **once**, feeds the
  result into `generate_angles()` → `prompts.py::build_user_prompt()`, which interpolates up to 5
  PAA questions + 8 related terms into the LLM call.
- **The `SearchDemandSignal` itself is discarded after that one call.** Nothing on
  `angle_gate_request` or `content_piece` records what PAA/keywords were actually shown to the
  LLM for this specific angle generation.

Consequence: a review screen wanting "the DFS/PAA context this piece was written with" **cannot
replay it exactly** — it can only re-run `fetch_search_demand_signal(trip_id, pool)` live against
`seo_context` *today*, which will usually match (², T2 DFS doesn't re-run often) but is **not
guaranteed identical** if DFS was re-run on that tour between angle-generation and review. This
is a genuine design question for Nghiệp, not something STEP0 should silently resolve — see §5.

### 1.5 Field-mapping table

**AA (internal/A4) view** — "trạng thái/điểm/gate pass-fail/retry/lỗi cụ thể/held-vì-lý-do-gì":

| Field | Already available? | Where |
|---|---|---|
| status (approved/held/failed/processing) | ✅ yes | `content_piece.status`, already in `GET /admin/a4/content-log` |
| gate pass/fail detail | ✅ yes | `content_piece.gate_ledger`, already in content-log response |
| retry count | ✅ yes | `content_piece.attempt_number`, already in content-log response |
| retry **feedback trail** (what changed between attempts) | ⚠️ in DB, not exposed | `content_piece.repair_log` exists but content-log's `SELECT` does not fetch it — small query addition |
| held/failed reason | ✅ yes | `content_piece.held_reason`, already in content-log response |
| numeric "score" | ❌ does not exist | T10 gates are per-criterion pass/fail (1/0 in `quality_gates.py`'s rubric checks) — there is no aggregate 0-10 score anywhere in T9/T10, unlike T3's `quality_score`. See open question in §5. |
| which angle was used | ❌ not joined | `content-log`'s query joins `angle_gate_request` only, not `angle_gate_option` — needs 1 more JOIN via `cp.angle_gate_option_id` |
| full content text | ❌ truncated | content-log returns `LEFT(cp.content_text, 280)`, not the full text — fine for a list view, needs a detail fetch for full text |
| atom/tour/goal context | ⚠️ partial | `agr.atom_id`, `agr.goal` (raw key, not label) already selected; tour/destination/atom-text not joined |

**Tenant view** — "nội dung bài theo channel + đầy đủ ngữ cảnh, KHÔNG thấy điểm/lỗi kỹ thuật":

| Field | Already available? | Where |
|---|---|---|
| full content text, by channel | ⚠️ exists but not exposed cleanly | `content_piece.content_text` is real; `GET /v1/content-writing/pieces/{piece_id}` (tenant-JWT, existing) returns it **but also returns `gate_ledger`/`repair_log`/`held_reason` verbatim** — see §4 warning, this endpoint is NOT safe to reuse as-is for a tenant-facing "hide the technical detail" screen |
| atom text / activity_type / emotional_hook | ✅ data exists, not joined for tenant | `acp_contract.tour_atoms`, needs a new join |
| tour/destination name | ✅ data exists, not joined for tenant | via `trip_id` → existing trip-registry fetcher, only when `trip_id` is set |
| goal (human label, not raw key) | ⚠️ key only | `angle_gate_request.goal` is the snake_case `key`; the label (`Goal["name"]`) is a static lookup in `services/acp_angle_gate/goals.py::get_goal()`, not stored — trivial to resolve, needs code |
| angle used (name/why_it_works/etc) | ✅ data exists, not joined for this purpose | `angle_gate_option` via `angle_gate_option_id`, same join as §1.2 |
| DFS/PAA "as generated" | ❌ never persisted | see §1.4 — only re-derivable live, not an exact replay |
| ready-or-not (no gate detail) | ✅ derivable | `content_piece.status IN ('approved','held')` is enough to say ready/not-ready — but a new endpoint must **deliberately omit** `gate_ledger`/`repair_log`/`held_reason` to honor the "no technical detail" requirement, since the existing piece-read endpoint does not omit them |

## 2. UI pattern survey

Read: `frontend/app/(tenant)/portal/_components/AngleGateTab.tsx` (796 lines, the T8+T9 wizard),
`frontend/app/(tenant)/portal/_components/PublishPendingList.tsx` (T11's tenant list),
`frontend/app/admin/a4-oversight/page.tsx` (700 lines, the A4 oversight page).

- **AngleGateTab.tsx** already renders a piece's result inline as the wizard's last step
  (`piece.status === "approved" || "held"` block, lines 755-790): a status badge, the full
  `content_text` in a bordered box, `held_reason` when held, and `req.cta`. **This already shows
  `held_reason` to the tenant** — a real, existing precedent that partially conflicts with
  AA-501's "tenant sees no technical detail" framing (held_reason is closer to plain-language than
  raw gate names, but it is still a quality-gate verdict, not just "not ready yet"). Worth
  flagging to Nghiệp rather than silently deciding whether the new page should be stricter than
  the wizard already is (§5).
- **This inline block only ever shows the single piece just written in the current session** —
  there's no route to come back and re-view it later, and no link out of the wizard to
  `/portal/t11-publish` (confirmed: zero references to `t11-publish` in `AngleGateTab.tsx`). A
  tenant currently has to use the sidebar to get from "Write Content" to "Publish" — the sidebar
  (`Sidebar.tsx:26-33`) already lists them as directly adjacent nav items with **nothing between
  them**, which is exactly the gap AA-501 describes and a clean, obvious slot to insert a new
  entry into.
- **PublishPendingList.tsx (T11)** shows only `title` (= angle name, from `v1_publish.py`'s
  `/pending`), a 280-char `content_preview`, `channel`, `created_at` — **no full content, no
  atom/tour/goal/DFS context**, and it is blog-channel-only by T11's own current scope. It is not
  a substitute for the review screen AA-501 wants.
- **a4-oversight/page.tsx** already has the exact list+expandable-row pattern
  (`ContentLogRowLine`, `expanded` state toggling a detail block) that a widened AA content-log
  view would reuse directly — no new component pattern needed, just more fields in the existing
  expand panel.
- Both tenant and admin surfaces reuse a shared design-token file per their own side
  (`(tenant)/portal/_components/ui.tsx`'s `T`/`Card`/`CardHead`/`Btn` vs `admin/_components/
  adminUi.tsx`'s `A`/`Card`/`Badge`/`TH`/`TD`) — a new tenant page should match the tenant
  token file, not adminUi.tsx, and vice versa if any admin-side change is made.

## 3. T10 = automatic gate, no manual stop — confirmed

Read `services/acp_content_writing/service.py` (`start_write()` / `run_write_background()`) and
`api/routers/v1_content_writing.py`. Confirmed: `POST /v1/content-writing/requests/{id}/write`
returns 202 immediately with a `processing` placeholder; `run_write_background()` runs the
write → T10 gate-check → (up to 1) repair-and-recheck loop **fully unattended**, landing on
`approved` or `held` with no pause for a human decision anywhere in that path (AA-450's
"single-endpoint, no async admin gate" architecture, unchanged since). There is no existing
screen — tenant or admin — that intercepts a piece *between* T10 finishing and T11 becoming
available; the closest thing today is the wizard's own end-of-flow inline card (§2), which is
transient (only exists for the piece just written in that session) and not really a standalone
"review before you publish" step. AA-501's premise holds exactly as stated.

## 4. Important warning for the build task (not something to fix now)

`GET /v1/content-writing/pieces/{piece_id}` (`services/acp_content_writing/service.py::
fetch_piece()`) already `SELECT`s and returns `gate_ledger`, `repair_log`, and `held_reason` in
full to the tenant, gated only by tenant-JWT ownership. **If the new Tenant review page is built
by simply calling this existing endpoint, it will leak exactly the technical detail AA-501 says
Tenant must not see.** The build task should either (a) add a new, deliberately-narrower tenant
endpoint that omits those 3 fields server-side, or (b) explicitly decide reusing this endpoint
and stripping fields client-side is acceptable (weaker — the raw data still reaches the browser).
Recommend (a).

## 5. Open questions for Nghiệp / Claude Chat before the build prompt

1. **"Điểm" in the AA view** — does this mean a literal numeric score (which does not exist for
   T10 today; T10 is pass/fail per-criterion, unlike T3's `quality_score`), or is "gate pass/fail
   count" (e.g. "7/9 gates passed") an acceptable stand-in? Building a real score would mean
   inventing a new aggregation, not just reading existing data.
2. **DFS/PAA replay fidelity** — is a *live* re-fetch of `seo_context.people_also_ask`/
   `related_keywords` (today's DB state, may drift from what the LLM actually saw at generation
   time) acceptable for the Tenant view's "ngữ cảnh" section, or does this need a real snapshot
   column added to `angle_gate_request` at generation time (a schema change, out of this STEP0's
   scope to decide unilaterally)?
3. **held_reason precedent** — `AngleGateTab.tsx`'s own wizard-end card already shows
   `held_reason` to the tenant today. Should the new review screen be *stricter* than the existing
   wizard (hide it entirely), or *consistent* with it (keep showing held_reason as a plain-language
   "not ready" explanation, just not the raw `gate_ledger`)? STEP0 did not resolve this — it's a
   product call.
4. **One page two modes, or two pages?** — see §6 below for the evidence-based recommendation;
   confirming before the build prompt avoids re-litigating architecture mid-build.
5. **Route slug** — proposed below, needs explicit sign-off (§6).

## 6. UI architecture recommendation + proposed route slugs

**Recommendation: two separate pages, not one page with a toggle.** Evidence, not preference:

- The two views sit behind **genuinely different auth mechanisms** already in this codebase —
  Tenant is `get_tenant` (JWT, `Depends`, per-tenant scoped) on `/v1/*`; AA/admin is
  `verify_admin_secret` (`X-Admin-Secret` header) on `/admin/a4/*`. A single page would need to
  either hold both credentials in one client bundle (bad — admin secret has no business reaching
  a tenant-portal page at all) or proxy through two different `/api/*` server routes anyway,
  which is most of the cost of two pages with none of the simplicity benefit.
- The **data shape genuinely differs**, not just the styling: AA's need is a cross-tenant list
  (content-log's existing "no hard tenant scoping, optional filter" shape) with gate/error detail;
  Tenant's need is a single-tenant, single-piece-at-a-time deep view with gate/error detail
  removed. These aren't the same query with a field mask — Admin already has its list view
  (content-log); Tenant needs a new, different kind of view.
- This matches how the rest of the AA/Tenant split already works in this repo (T7 planning has a
  tenant page and a completely separate admin surface; T11 publish/A4-force-unpublish is the same
  split) — no precedent anywhere in this codebase for one page serving both audiences via a mode
  toggle.

**Proposed slugs** (not yet confirmed — flagging for sign-off per the task's own instruction):

- **Tenant**: `/portal/t10-review` — "t10" is the one number in the existing t0/t1/t4/t6/t7/t8/t11
  sequence with **no page of its own** (T9/T10 were folded into `t8-angle-gate`'s wizard by
  AA-450's own explicit decision), so it reads naturally as "the review of what T10 decided" and
  slots by number exactly where it belongs in the sequence — between `t8-angle-gate` and
  `t11-publish`. Sidebar label suggestion: **"Review"**, inserted between "Write Content" and
  "Publish" (`Sidebar.tsx:31-32`).
- **Admin**: **no new route** — extend the existing `/admin/a4-oversight` Content Log section
  (widen the query per §1.5, add the fields to `ContentLogRowLine`'s existing expand panel). A new
  admin route would only repeat the exact `middleware.ts` `PROTECTED_ROUTES` gap this repo has
  already hit independently 4 times (AA-384/388/405/437, per `a4-oversight/page.tsx`'s own header
  comment) for zero benefit, since the oversight page already exists, is already allow-listed, and
  already has the right list+expand pattern.

## 7. New endpoints to build (list only — no code this STEP0)

1. **Tenant**: `GET /v1/content-writing/requests/{request_id}/review` (or similar) — tenant-JWT,
   returns the latest `content_piece` for the request (ordered by `created_at DESC`, per §1's
   AA-497 note that attempt_number is no longer a per-request sequence) joined to
   `angle_gate_option` (via `angle_gate_option_id`), `tour_atoms` (via `atom_id`), and
   trip/destination (via `trip_id`, when present) — full `content_text`, goal label, angle detail,
   atom context, tour/destination — **deliberately omitting** `gate_ledger`/`repair_log` and,
   pending Q3 in §5, possibly `held_reason`. A simple ready/not-ready boolean derived from
   `status` instead.
2. **Admin**: widen `GET /admin/a4/content-log`'s existing query — add `cp.repair_log` to the
   `SELECT`, add a JOIN to `angle_gate_option` for the angle name, and (pending Q1 in §5) decide
   whether to add a computed gate-pass-count as a stand-in "score". No new route.

## 8. Should-know for whoever builds this next

- Don't trust `angle_gate_request.channel` alone anywhere — always `COALESCE(cp.channel,
  agr.channel)`, matching the 3 existing call sites that already do this.
- `content_piece` is no longer 1 row per request (migration 125) — always pick latest by
  `created_at`, never assume `attempt_number` orders across a request's lifetime.
- `content_piece.content_summary`/`content_embedding` exist but are populated by nothing — do not
  build against them expecting real data.
- DFS/PAA is a live re-fetch, not a stored fact — see §1.4/§5 Q2 before promising the tenant an
  exact replay.

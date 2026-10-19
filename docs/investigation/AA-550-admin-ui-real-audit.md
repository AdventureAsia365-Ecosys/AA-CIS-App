# AA-550 — Real UI audit of `/admin/atom-curation` (Playwright, production)

**No code changed in this task.** Audit + report only, per issue requirement.

## Method

- Logged into `https://aa-cis.lumiguides.it.com/login` as `e2e-test-admin` (Playwright,
  headless Chromium, real production frontend + real production backend — no local dev server,
  no mocked API). Credential was stale in the repo (`e2eTest2026!`, set by AA-437, invalidated by
  AA-505's own later reset); current value retrieved fresh from Secrets Manager
  `aa-cis/dev/e2e-test-admin` this session (AWS MFA re-auth done live by Nghiệp mid-task).
- Captured all 8 sections (01–08) of `/admin/atom-curation` in both states the issue asked for:
  **"All tours (Atomize only)"** and **"1 Tour cụ thể"** — 24 full-page screenshots total, plus a
  dedicated scroll test and a follow-up run on a tour with real pipeline data. 12 of the most
  evidentiary screenshots are attached to the Linear comment on this issue; the rest exist locally
  (not committed — this repo's `docs/` is gitignored except `implementation-notes/`, see
  `project_docs_gitignored_notes_tracked` memory).
- Read `frontend/app/admin/atom-curation/page.tsx` (893 lines) and
  `api/routers/admin_dashboard.py` (261 lines) directly — the two files that actually render/serve
  these 8 sections — rather than trusting AA-546/548's own summaries.

## Root cause of the AA-546/548 vs. real-UI contradiction

**There is no contradiction in what was actually built or verified — the contradiction is between
two different, both-true claims that got collapsed into one sentence.**

1. **AA-546 (investigation) claim**: "UI Admin giám sát Segment/Score/Route/Hub ĐÃ TỒN TẠI... hiển
   thị đủ 8 section T5-T11." — **True, verified again here.** The page exists, is wired to
   `admin_dashboard.py`'s 4 endpoints, and does render all 8 sections. AA-546 never claimed these
   sections show data in "All tours" mode — it wasn't asked to, and its own report doesn't say it.
2. **AA-548 (bug fix) claim**: "Live-verify qua browser thật... Segment/Score/Route/Hub đều hiện
   MARKET, không còn TENANT... dữ liệu thật hiển thị đúng." — **True, verified again here** (see
   screenshots 04/05/06 below, tour = SOUTHERN LAOS, which has real T5–T8 data). AA-548's own PR
   description says this test used a tour with real data loaded via a local Next.js dev server
   pointed at the real deployed backend — i.e. it always had **one tour selected**, because that
   is the only state in which these panels render anything but a prompt. AA-548 never tested, and
   was never asked to test, "All tours" mode.

**The actual gap**: nobody — not AA-546, not AA-548, not the original AA-527 "bổ sung" build — ever
flagged that sections 02–08 **cannot show anything platform-wide, ever, by explicit design**, which
directly contradicts AA-545/546's own stated goal ("giám sát Segment/Score/Route/Hub **platform-wide**").
This is documented in the code itself, not a bug that crept in:

- `frontend/app/admin/atom-curation/page.tsx:29-31`:
  > "Sections 2-8 all REQUIRE one selected Tour (their endpoints are Tour-scoped by schema) — the
  > header's 'All tours' option is only meaningful for Atomize (section 01)."
- `frontend/app/admin/atom-curation/page.tsx:66-75` (`SECTIONS` array): every section except
  `atomize` has `requiresTour: true`.
- `frontend/app/admin/atom-curation/page.tsx:489-505` (`useTourScopedFetch`): when `tourId` is
  `null`, the hook returns `data: null` immediately without ever calling the API.
- `frontend/app/admin/atom-curation/page.tsx:108-115` (`PickTourPrompt`): every one of sections
  02–08 renders this exact component (`"Chọn 1 Tour cụ thể"`) when no tour is selected — line
  515/545/572/618/663/697/727, one call site per section, all identical.
- `api/routers/admin_dashboard.py:10-15` (module docstring, its own words): "REQUIRE `tour_id` —
  the dashboard's header anchor. There is deliberately no 'all tours' mode for these 4." Backend
  enforces this too: `tour_id: str = Query(...)` (required, no default) on all 4 endpoints
  (`segments`/`score`/`routes`/`slate`) — a request without `tour_id` gets a 422 from FastAPI
  itself, before any handler code runs.

So: **AA-527's original "bổ sung" build (05/09/2026) shipped Segment/Score/Route/Hub/Slate as
strictly per-Tour audit views, not the platform-wide monitoring AA-545/546 asked for** — a real,
un-flagged scope gap that has existed since that PR merged, and that neither AA-546 (investigation,
same day) nor AA-548 (follow-up bug fix, same day) caught, because both were scoped to something
else: AA-546 to "does a UI exist at all" (yes) and "what fields does the API return" (answered),
AA-548 to "fix 2 named column bugs on the per-Tour view" (fixed, correctly, confirmed below) — never
"does this satisfy the platform-wide ask."

## Screenshot evidence

### "All tours (Atomize only)" — sections 02–08 all show the same PickTourPrompt

| Section | Result |
|---|---|
| 01 Atomize | Full working dashboard: totals, distinctiveness breakdown, per-tour list, 377 atoms, filters, star/delete. **(#12)** |
| 02 Segment | `PickTourPrompt`: "Chọn 1 Tour cụ thể" **(#01)** |
| 03 Score | same `PickTourPrompt` |
| 04 Route/Hub | same `PickTourPrompt` |
| 05 Slate | same `PickTourPrompt` |
| 06 Write/Gate | same `PickTourPrompt` |
| 07 Review | same `PickTourPrompt` |
| 08 Publish | same `PickTourPrompt` **(#02)** |

All 7 non-Atomize sections are byte-for-byte the same empty-state component with only the section
name substituted — confirms this is one shared code path (`PickTourPrompt`), not 7 separate bugs.

### "1 Tour cụ thể" selected — two very different outcomes depending on the tour

**Tour with genuinely no pipeline data yet** ("3.5-Hour Shuri Castle...", a 1-day family tour,
`atomize` shows 10 atoms but nothing downstream has run): sections 02–08 correctly switch from
`PickTourPrompt` to a **real, tour-specific empty state** with its own message and its own
`EmptyState`/`ErrorState` component per section — e.g. Segment: "No Segments yet — No atom on this
tour has been grouped into a Segment (`services/acp_contract/segment_matching.py`) yet." **(#03)**.
This is not the same code path as `PickTourPrompt` — the panel is doing a real fetch, getting a
real empty `total: 0` back from the API, and rendering that truthfully.

**Tour with real T5–T8 data** ("SOUTHERN LAOS", 41 atoms, 138 Segment rows, 138 Score rows, 13
Route rows, 49 Slate rows, 18 content pieces): every panel renders real rows.

- Segment **(#04)**: real place/action rows, `MARKET` column shows AU/DE/FR/NL/UK/US (not
  `TENANT`), Total rank/Recurrence/Route columns populated. Matches AA-548's fix exactly.
- Score **(#05)**: same Market breakdown, demand/recurrence/questions/said rank columns all
  populated with real numbers.
- Route/Hub **(#06)**: `CURRENT`/`SUPERSEDED v1` version history visible (AA-532), Market column
  present, real Hub name ("Vat Phou → Tad E-Tu Waterfall → Si Phan Don → Khone Phapheng Falls"),
  real scores (23.8125–24.75, matching the exact number AA-548's own comment cited).
- Slate **(#07)**: real rows across 7 channels (instagram/tiktok/linkedin/blog/landing_page/email/
  ads/facebook), correctly **still shows a `TENANT` column** ("WanderLux Travel") — this is
  correct, not a regression: `acp_shared.subject` genuinely is per-tenant (a Subject is a specific
  tenant's proposal), unlike Segment/Score/Route which AA-545 made platform-wide. AA-548's own
  code comment (`page.tsx:632`) documents this distinction explicitly.
- Write/Gate **(#08)**: real gate-ledger cards, held/approved states, actual F1_GROUNDING/
  F9_BRAND_VOICE violation badges and held reasons visible.

**AA-548's fix is real, correctly scoped, and verified again here independently — it fixed exactly
what it claimed to fix, on exactly the state (1 tour selected) where these panels have ever shown
anything.**

## New finding — not previously reported in AA-546/548/550

**Section 08 (Publish) returned a real `HTTP 401`** on the SOUTHERN LAOS tour, in the same
authenticated session, in the same page load, immediately after Segment/Score/Route/Slate/Write-Gate
all succeeded (`200`) against sibling admin_a4/admin_dashboard endpoints:

```
Could not load this panel
/api/admin/a4/publish-log?tour_id=4bf83a2c-cec0-4127-ac82-1a26fbbd48a3 → HTTP 401
```

Screenshot **(#09)**. The section-count badge in the sidebar shows "8" for Publish (i.e. a
**separate** fetch to the same endpoint, from `useSectionCounts`, succeeded), while the panel's own
fetch 401'd — so this isn't a blanket auth failure, it's specific to one of the two call sites
hitting the same URL. A first run on a different tour (Shuri Castle, no publish rows) got `200` on
the identical endpoint shape. Not reproduced a second time in this session (see "Should know"
below — repeated login attempts against production got rate-limited by this environment's own
safety tooling, not by the app). **Flagging as a real, observed-once anomaly worth its own ticket
— not confirmed as reproducible, not something this audit fixed or explained further.**

## Vietnamese-string audit (point D of the issue)

Confirmed exactly 2 locations, both identified by direct grep of the page source
(`frontend/app/admin/atom-curation/page.tsx`) — not guessed:

1. **`page.tsx:111`** — `PickTourPrompt`'s title, `"Chọn 1 Tour cụ thể"` (+ body text same line),
   the one AA-550 already named. Appears on all 7 non-Atomize sections in All-tours mode.
2. **`page.tsx:578-580`** — `BacklogNote` inside `RouteHubSection` (section 04 specifically):
   *"Backlog (AA-525 Phần 12 mục 8): chưa có UI liệt kê Hub/Family (tour nào được gộp chung 1 hành
   trình, vì sao) — bảng dưới đây chỉ hiện Route.hub_name (text), không hiện lý do gộp Hub."*
   Visible on **every** Route/Hub panel view regardless of tour or data state — see screenshot
   **(#06)**, where it renders directly above the real Market data table. AA-550's own text said
   "và có thể còn chỗ khác" — this is that other place.

No other Vietnamese string literals found in this file (full grep of Vietnamese-diacritic and
common-word patterns across the file returned only these 2 hits).

## Layout / sticky audit (points A.1–A.2 of the issue) — refined, not just confirmed

Direct scroll test (mouse-wheel scroll + viewport screenshots, not full-page composites, so this
reflects true on-screen behavior): screenshots **(#10)** at scroll-top and **(#11)** at +2500px
inside the long Atomize list (377 atoms, "All tours" mode).

**Result is more specific than the issue's framing**:

- The **site-wide outer `AdminSidebar`** (dark nav column common to every `/admin/*` page —
  Dashboard/Tenants/Run Health/.../Atom Curation, admin user badge at the bottom) **is already
  sticky and stays correctly pinned** across the scroll test. This contradicts a literal reading
  of AA-550 point 1 ("Sidebar bên trái KHÔNG sticky") if "sidebar" means this outer nav.
- What actually disappears on scroll is **this page's own header** (`"T5–T11 Content Pipeline"` h1
  + the Tour dropdown) **and this page's own inner 8-section list** (the "01 · Atomize / 02 ·
  Segment / ..." column) — both scroll away together, confirmed gone by +2500px. This matches
  AA-550 point 2 (header not sticky) precisely, and is likely what point 1's screenshots (ảnh 1→2)
  actually showed — the *inner* section nav, not the *outer* site nav, going missing.
- Root cause in code: `page.tsx:844-849` gives the inner section-nav
  `position: "sticky", top: 28` — but its containing/scrolling context is the page's own
  `overflowY: "auto"` wrapper (`page.tsx:815`), and the `h1`/dropdown header block right above it
  (`page.tsx:816-842`) has **no sticky styling of its own at all**. Whether the CSS `sticky` on the
  inner nav is failing to take effect (wrong scroll-container assumption) or the header was simply
  never given sticky styling in the first place, the net visible effect for a user is: scroll down
  more than ~1 section's worth of content and both the page title/Tour-picker and the section
  navigation are gone, while the outer site chrome remains — needs a frontend fix, not something
  this audit changes.

## What's backend-ready vs. frontend-wired (point 3 of the issue's ask)

| Data | Backend endpoint | Frontend caller | All-tours mode |
|---|---|---|---|
| Segment | `GET /admin/dashboard/segments?tour_id=` (required) | `SegmentSection` | Never called — `PickTourPrompt` short-circuits |
| Score | `GET /admin/dashboard/score?tour_id=` (required) | `ScoreSection` | same |
| Route/Hub | `GET /admin/dashboard/routes?tour_id=` (required) | `RouteHubSection` | same |
| Slate | `GET /admin/dashboard/slate?tour_id=` (required) | `SlateSection` | same |
| Write/Gate, Review | `GET /admin/a4/content-log?tour_id=` (optional filter) | `WriteGateSection`/`ReviewSection` | same (frontend never omits `tour_id`, even though the backend filter is optional there) |
| Publish | `GET /admin/a4/publish-log?tour_id=` (optional filter) | `PublishSection` | same |

**Important distinction the issue's "platform-wide" ask needs to reckon with**: `admin_dashboard.py`
(Segment/Score/Route/Slate) makes `tour_id` a **hard-required** FastAPI query param — building a
platform-wide view for these 4 needs **new backend query shapes**, not just a frontend change to
stop passing `tour_id`. `admin_a4.py` (content-log/publish-log, used by Write/Gate/Review/Publish)
already supports an **optional** `tour_id` — a platform-wide view of those 3 could be built
**frontend-only**, by simply not sending `tour_id` when "All tours" is selected; nothing new needed
on the backend for that half.

## Proposed layering (point 4 of the issue's ask — proposal only, not decided here)

Two real groups exist in the data itself, not just the UI:

- **01–05 (Atomize/Segment/Score/Route-Hub/Slate)**: platform data. Atomize and (per AA-545) Score/
  Segment/Route have no tenant concept at all; Slate is the one exception that is genuinely
  per-tenant even in this group (a Subject is one tenant's proposal).
- **06–08 (Write/Gate/Review/Publish)**: tenant activity data. Every row is a specific tenant's
  content_piece/publish_log entry.

Two options, not chosen here:

**A. Two visually distinct zones on one page** — a divider + subheading between 05 and 06 (e.g.
"Platform pipeline data" / "Tenant activity for this Tour"), keep one page, one Tour-picker. Lowest
effort, keeps the current 8-section mental model AA-527 already shipped.

**B. Split into two pages** — `/admin/atom-curation` keeps 01 (+ maybe 02-05 once platform-wide is
real) as pure platform monitoring with no Tour-picker requirement; a new `/admin/tenant-activity`
(or similar) becomes the Tour+Tenant-scoped audit view for 06-08. Cleaner separation, more
consistent with the Admin/Tenant boundary work in `CONTEXT.md`/ADR-0002, but a bigger frontend
change and a second nav entry.

Neither is built here — flagging for Nghiệp/Claude Chat to decide before any build issue is opened,
per the issue's own instruction.

## Should know

- AWS re-auth: this session's cached `aa365-admin` STS session had expired; Nghiệp did a live MFA
  re-auth mid-task (2 codes exchanged) so this audit could fetch the current
  `aa-cis/dev/e2e-test-admin` credential from Secrets Manager — the repo's own documented value
  (`e2eTest2026!`, from AA-437) was stale, silently replaced by AA-505's later reset without a new
  plaintext ever being committed (by design, per that file's own security note).
- A repro attempt for the Section-08 401 (multiple login+retry cycles against the production login
  endpoint in a short window) was blocked by this environment's own automated safety classifier —
  it read as credential-testing/brute-force-shaped traffic even though the actual credential was
  already known-valid. Did not attempt to route around it; the anomaly is reported as observed-once
  from the one successful session instead of confirmed-reproducible.
- Local screenshots (31 total, more than the 12 attached to Linear) are in this session's
  ephemeral scratchpad, not the repo — `docs/` is gitignored in this repo except
  `implementation-notes/*.md` (force-tracked), so this file itself is the durable record; the
  images live only as the 12 Linear attachments on this issue.
- Temporary `.js.tmp` script copies used to resolve this repo's `node_modules/playwright` were
  deleted after use — `git status` at the end of this task shows no new/leftover files from this
  audit.

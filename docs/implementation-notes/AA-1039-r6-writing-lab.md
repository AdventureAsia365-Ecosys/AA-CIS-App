# AA-1039 — R6 slice 3: Writing Lab per stage and the AI & spend group (lane r6-writing-lab, S267)

Plan: root ADR 0019 d13, clarification TL-13 (`regroup_lab`), root audit
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §6.2 (IA table), §6.3 (AI gateway), §6.4 (Writing
Lab). Slices 1 and 2: `AA-1039-r6-admin-ia.md`, `AA-1039-r6-gate-fanout.md`.

## Decisions

- **A Lab stage is a writing-memory writer.** Every learning table (`writing_signal`, `writing_rule`,
  `writing_memory_release`, `writing_memory_shadow`, `eval_golden_item`, `eval_item_result`, `prompt_eval_runs.writer`)
  CHECKs the writer against the same four values `a1`, `t2`, `t9_blog`, `t9_social`, so those are the four stages.
  URL slugs (`lib/writingLab.ts`): `a1-s1`, `t2-rewrite`, `t9-blog`, `t9-social`. §6.4 also lists `place_card` and
  `tp_compose`: they have no row in any learning table (the CHECKs refuse them), so they are not stages yet; adding
  them is "lift the freeze per stage" work, not a regroup.
- **One read endpoint, no new logic.** `GET /admin/writing-memory/lab/{writer}` (`services/writing_memory/lab.py`) reads
  the five sections the issue names over existing tables: samples (active golden sets), labels (golden label verdict
  / source, signal fact status / reason / action / actor), experiments (`prompt_eval_runs` of the writer, plus A1's
  legacy `s1_old` / `s1_from_atom` regression runs; release shadow runs from `writing_memory_shadow` grouped by run;
  model shadow comparisons from `llm_shadow_log` of the writer's prompt stages over 30 days), prompt versions
  (`prompt_version` rows of the writer's prompt stages) and the eval gate (memory switch, active / newest platform
  release, releases per status, tenant releases live, rules per status, newest finished r1 / r2 / reverify / shadow
  run). No write, no job, no LLM (unit test parses the module's imports).
- **Prompt stages per writer** are a fixed map in `lab.PROMPT_STAGES`, checked against
  `shared/llm_client/prompt_versions.json` by a unit test (a renamed stage fails CI, not the page).
- **Gate state is a read, not a rule.** `gate_state()` names one of `no_release`, `memory_off`, `reverify`,
  `regression`, `passing` from stored rows. The activation checks stay in `releases.py` and the CIS 287 guard.
- **The gate state reads the ACTIVE release's evaluation only** (review fix, S267). `active_eval` is the newest
  finished run of the live release: one of its stored evidence runs (eval / shadow / reverify run ids; this covers
  release 0's baseline import, which has no `release_id`) or an r1 / r2 / reverify / shadow run whose `release_id`
  is the live release's. An r2 counts only as `releases.release_link_problem` counts it (no seeded-fault operator
  check, no `candidate: current` code-train run). The writer's newest run of any release stays on the page as
  `last_eval` (tile "Newest evaluation, any release", with `for_active`) but never sets the state: a candidate
  release failing its r1 / r2 is the gate working, while the live release keeps passing.
  On the page: tile "Live release evaluation" (`active_eval`, red on fail / regression, green on pass or release 0's
  baseline import, `lib/writingLab.evalTone`) and tile "Newest evaluation, any release" (`last_eval`, no colour, hint
  says whether it ran the live release, another release or none).
- **No text on the Lab.** The SQL selects ids, labels, counts and hashes only (a unit test lists every `pii:free_text`
  column of these tables and fails if one is selected). The logged `ai_ops` read of tenant text is R2's lane; the
  Writing memory tools (which do show masked text, admin only) stay one click away.
- **Each section is read on its own connection**: a missing table (code before a migration) leaves that section
  `ready: false` with no error; any other failure says "<section> could not be read" (no exception text).
- **Writing memory moved under the Lab**: `/admin/writing-memory` → `/admin/lab/writing-memory` (307, query kept).
  A static segment beside the dynamic `/admin/lab/[stage]` (Next.js matches the static one first; a unit test keeps
  every stage slug different from `writing-memory`). The API path `/api/admin/writing-memory/*` does not move. Jev
  Decisions keeps its URL (already in the Writing Lab group since slice 1).
- **Stage page**: `/admin/lab` redirects to the first stage (`a1-s1`); the stage bar (kit Tabs, track) is the only
  navigation between stages, the five sections are kit Tabs (underline) with `?tab=` kept by replaceState. Nav: Writing
  Lab › Stages (`/admin/lab`, active on every stage page), Writing memory, Jev Decisions.
- **AI & spend**: two items. "AI providers" is renamed **AI gateway** (IA §6.2) in the nav, the page title and the
  user-visible copy that names the page (EN "AI gateway", VI "Cổng AI"); its tabs (Providers with Connections /
  credit views, Routing, Models & trials, Budgets with app profiles, Calls, Guide) already bring the providers,
  routing, budgets and calls together, so the page stays as it is. **Spend** (`/admin/spend`) is the one spend page:
  Cost Operations moved there (`git mv cost-ops spend`) and Cost per app became its **By app** tab
  (`?tab=apps`, `CostByApp.tsx` without its own frame; `?month=YYYY-MM` opens a month). The day window and the
  tenant / account filters are hidden on By app (a month view of its own). Old URLs: `/admin/cost-ops` → `/admin/spend`
  (query kept, so `?tab=spend&view=dfs` still lands on its view), `/admin/platform/cost` → `/admin/spend?tab=apps`
  (Next.js merges the queries, the target's `tab` wins; `resolveAdminRoute` now does the same). `/admin/llm-usage`
  goes to `/admin/spend` in one hop.
- **Routing `pii_class`**: the data does not exist yet (no column in the 04/11 schema dump, no field in
  `GET /admin/llm-config`, no code that classifies a stage). The slice-1 "Routing by data class" card already lists
  stages per class as soon as the API sends `pii_class`; nothing to show until then, so no change here (no invented
  classes). It lights up with the gateway work that adds the field (AA-1024 / TL-12).
- **Server links follow the moves** (no redirect hop in mails and reports): `shared/ops_alerts.py`
  `WRITING_MEMORY_PAGE_PATH`, `DFS_SPEND_PAGE_PATH`, `services/reports/money.py` `COST_PAGE` / `PLATFORM_COST_PAGE`,
  `services/reports/quality.py`; in-app links (Overview, Tenant 360 voice card) too.

## Changed

- Backend: `services/writing_memory/lab.py` (new), route `GET /admin/writing-memory/lab/{writer}` in
  `api/routers/admin_writing_memory.py`, staff POLICY entry (tenant.read, admin only like every writing-memory read),
  route snapshot regenerated; page links in `shared/ops_alerts.py`, `services/reports/money.py`, `quality.py`.
- Frontend: `lib/writingLab.ts` (new), `app/admin/lab/{page.tsx,[stage]/page.tsx,WritingLabStage.tsx,messages.ts}`
  (new), `app/admin/writing-memory` → `app/admin/lab/writing-memory` (imports one level deeper),
  `app/admin/cost-ops` → `app/admin/spend` (+ By app tab), `app/admin/platform/cost/{CostByApp,messages}` →
  `app/admin/spend/{CostByApp,byApp.messages}.ts(x)` (page deleted), `lib/adminIa.ts` (groups, moves, query merge),
  `app/_kit/adminNav.tsx` (icon `lab`), `nav.messages.ts`, `lib/rbac.ts` (`/admin/spend`, `/admin/lab` admin only),
  `lib/aiGateway.ts` (`COST_OPS_PATH` = `/admin/spend`, `SPEND_APPS_TAB`), `next.config.ts` (llm-usage target), copy
  that names "AI providers" / "Cost Operations" / "Cost per app" as a page (EN + VI messages and guides).
- Tests: `tests/unit/test_aa1039_writing_lab.py`, `tests/integration/test_aa1039_writing_lab_sql.py` (incl. a live
  release 0, a newer failed candidate r1 = still passing, a seeded-fault r2 ignored, a failed reverify of the live
  release = regression),
  `frontend/lib/writingLab.test.mjs` (new); `lib/adminIa.test.mjs` test 10; path fixes in `guides`,
  `writingMemoryGuide`, `writingMemoryLayout`, `aiGatewayPolish`, `aiGateway` node tests and the Python
  `test_aa994_*`, `test_aa998_*`, `test_aa1003_r3_money`, `test_aa642_l2_alerts_digest`; e2e: `ui-smoke` page list
  (Spend, Spend · By app, Writing Lab stage, Writing memory; AI gateway labels), `aa858-ops-overview` (d),
  `admin-evidence` PAGES (+ `/admin/lab/t9-social`, `/admin/ai-gateway`, `/admin/spend`, `/admin/spend?tab=apps`, with
  ready markers), legacy `aa663-admin-ia`.
- `CONTEXT.md`: admin console layout, page list, Spend, Writing memory path.

## Tradeoffs

- One wide read per stage (13 small queries over 5 connections) instead of reusing the six existing Writing memory
  list endpoints from the browser: the existing endpoints have no writer filter on several reads (summary, metrics,
  clusters) and none on golden sets or prompt versions; one read keeps the page to one request and the rules about
  text in one place.
- **Two PRs** (backend first): the stage page calls a new API route, and a Vercel production deploy plus its signed-in
  smoke can finish before the ECS rollout of the same merge; the backend PR goes first so the page never meets a 404.

## Should know

- No migration in this slice.
- Not built (follow-ups): `place_card` and `tp_compose` stages (no learning rows; the CHECKs refuse them until the
  freeze is lifted per stage); the IA's Lab tabs "Regression set" and "Metrics" per stage stay in Writing memory
  (Metrics tab, weekly KPI per writer) for now; showing masked tenant text on the Lab is R2's logged `ai_ops` read.
- Local visual check (dev server, mocked API, invented values): every Lab tab and Spend › By app at 1440 / 390, light /
  dark, no horizontal overflow (document and the admin main region). The real screenshots are the orchestrator's
  admin-evidence run.

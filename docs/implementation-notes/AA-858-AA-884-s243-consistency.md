# AA-858 / AA-884 — S243 UI verify: one answer per question across Overview and Platform pages

Branch `fix/admin-overview-platform-consistency`. The S243 production UI check of https://aa-cis.lumiguides.it.com found
11 places where two admin pages told a different story about the same thing (screens:
`AA-Ecosys/docs/audits/2026-10-19-S243-platform-lanes/screens/`).

## Decisions
- **One health definition** (`api/services/app_health.py`): status = the worst KNOWN signal of the app's own check
  (CIS: live workers on the API's commit + schema; Booking: `/booking/health` + `/health/ready`; TripPlanner: none)
  and its CloudWatch traffic of the last 24 h (the AA-858 section, same Redis key, no extra AWS call). Traffic
  `warn` (≥ 1 % 5xx, throttles) and `alarm` (alarms firing) → `degraded`; only a failing health endpoint is `down`.
  A signal with no data (no metrics, probe timeout) is ignored; `unknown` only when no signal is known.
- The verdict lives in the card's **`overall`** key, not `health`: Booking's card already has `health` (its
  `/booking/health` answer). The overview copies each Apps card's `overall` onto its tile (`_attach_overall`, passing
  its own traffic section so one page view reads CloudWatch once); if the Apps build fails, the tile uses the same rule
  on traffic alone. Apps cache key bumped to `admin_platform_apps:v2` (new shape).
- One wording of statuses (`APP_STATUS_LABEL`: Healthy / Needs a look / Down / Unknown) and of the basis
  (`basisText`) in `frontend/lib/platform.ts`, used by both pages.
- **Workers alive now** = rows with the `alive` flag (`shared.job_worker` not stopped, seen ≤ 60 s) — the Apps page /
  deploy smoke rule. `worker_health` lists up to 20 recent workers incl. dead ones; that historical number is not
  shown any more. Queue depth / running now were counting job *kinds*; now they sum jobs.
- **Staff readiness is live**: `readiness_from_rows()` (pure, used by the reconcile too) recomputed from
  `shared.admin_users` on every `GET …/staff-directory/health` (`readiness_now` + `as_of`); Cognito drift stays "as of"
  the last run. The health query key moved under `["platform-staff", …]` so StaffManage's existing invalidation
  refetches it after any write. Chose recompute over enqueueing a reconcile on each write: no Cognito call, no job.
- **Cost, one presentation**: labels/hints from `platform_cost.ROW_LABELS/ROW_HINTS` (the overview's friendly words);
  every row and the totals carry `ai_outside_aws_usd`; both tables show AWS bill + AI outside the AWS bill = Total
  (`frontend/lib/costWording.ts`). The Cost page KPI strip is AWS bill + AI estimate − AI already on the AWS bill =
  Total, with the operators drawn; the overview card adds the same sentence under its table.
- **Legacy overview provider table** (finding 5): month-to-date came from raw `llm_call_log` grouped by the raw
  provider string, so `bedrock-satellite` (acc3, most of the spend) never matched the `bedrock` row ("—"), and rolled-up
  days were missing. Now `platform_cost.ai_spend_by_provider()` (the cost module's own SQL, families bedrock / openai /
  jev / other); the card shows "AI this month, all providers" = the Cost page AI estimate. Today's numbers stay
  `cost_guard.day_spend` (already `LIKE 'bedrock%'`).
- **Booking**: counts only, with a note; no "% of the step before" (window counts, not a cohort).
- **Kit**: `DataTable` gained `meta.align: "right"` (header + cells) and `paginate={false}`; `primitives` gained
  `ScrollX` (horizontal scroll inside its own box + a hint shown only while it really overflows, ResizeObserver).
  Both copied byte-identically into `portal/app/_kit/` (guard `test_aa885_portal_split.py`).
- **Audit log**: keyset "Load older" only (`paginate={false}`), "Showing N rows" count; page 50 desktop / 25 phone;
  phone uses two compact columns (card = action, who, when, chain # + request); chain badge says the chain covers
  every row while the list is filtered; short placeholders; internal codes (CIS 248, X1) replaced by plain words.

## Changed (vs the findings)
- Finding 8 (Bhutan S1 generated 33 vs raw active 32): not changed, noted in the PR as a data oddity.
- The overview smoke (`aa858-ops-overview.spec.ts` (c)) now allows a table wrapper to scroll sideways only when it
  shows the ScrollX hint (finding 9 asks for scrollable tables with every column).

## Tradeoffs
- `/admin/overview/ops` now also builds (or reads the 60 s cache of) the Apps cards: on a miss that adds the Booking
  HTTP probe (2 s timeout) and two small DB reads. Accepted for one status per app.
- Money on screen is summed from cent-rounded parts (`costWording.shownSum`), so every "A + B = Total" line adds up
  on screen; the backend keeps 4 decimals (CSV export too). A column of rounded rows can still differ from the
  totals row by a cent. (Review fix: live October showed $101.24 + $144.35 − $0.42 = $245.16.)
- On a phone the audit log offers no CSV (the kit exports the visible columns, which are the two summary cards there);
  CSV stays on desktop with the full column set. The page size is read when a page is fetched, not kept in the query
  key, so crossing the 768 px breakpoint keeps the loaded older pages.

## Should know
- Not deployed: the local UI check used a production build with every `/api/admin/*` answer mocked from fixtures
  generated by the branch's own backend functions (real sign-in against Dev only for the page gate).
- Local integration DB lacks migration 248 (local role is not SUPERUSER), so the `cost_by_app` half of
  `tests/integration/test_s243_admin_consistency.py` runs only in CI; the provider and readiness parts passed locally.
- `staff_authz` POLICY unchanged (no new route).

## Review fixes (S243, second commit)
- **Jev = `typesafe`** (major): Jev decisions are logged with provider `typesafe` (`shared/llm_client/decide.py`), so
  `provider_family("typesafe")` is now "jev" (live Dev: $15.56 MTD was landing on "Other AI providers" with Jev at
  $0.00). The tests that locked `typesafe` -> "other" are fixed. Today's Jev spend on the provider table is read in
  `admin_overview` from `llm_call_log` (`provider IN ('typesafe','jev')`), display only: `shared.cost_guard` still
  has no Jev day-spend query and does not enforce a Jev daily cap (AA-660) — unchanged on purpose, enforcement is
  out of scope here.
- **Unanswered health check**: Booking's probe timing out now reads "Needs a look" with "its health check did not
  answer", instead of "Healthy … it has no health check". Firing alarms stay "degraded" on purpose (module docstring).
- **KPI strip adds up**: see Tradeoffs; e2e smoke (d) checks the four values to the cent.
- **Audit CSV / page size on a phone**: see Tradeoffs.


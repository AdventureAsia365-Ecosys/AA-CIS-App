# AA-1096 — Platform › Infra limits (the Terraform layer, read-only)

S279 thresholds audit (root `docs/audits/2026-11-13-S279-session/thresholds-audit/`, section 4 and lane L6): the limits
that stay in Terraform on purpose (WAF rate rules and geo allow-list, API Gateway throttles, Lambda reserved
concurrency, AWS Budgets, alarm thresholds) are "shown read-only on admin Platform > Infra". L6 (AA-CIS-Infra #832)
published them as the root output `platform_limits`, rendered on the generated alarms page; the issue comment of
13/11/2026 left "the follow-up Platform › Infra read-only page (needs an API over the Terraform output)".

## Decisions

- **Source = the docs reader, not Terraform state or AWS.** The Infra docs generator already renders the output into
  the section "Edge, budget and alarm limits" of root `docs/generated/alarms.md` (OPS-07, staff tier), and Docs sync
  publishes it to the bundle the CIS API reads (`api/services/docs_reader`, cached 5 min). `api/services/platform_infra.py`
  reads that one page by id (`load_staff_page_by_id`, the same call as the AI gateway Guide tab) and parses the
  section's tables. No new IAM grant (the task role is not given the Terraform state bucket or `wafv2:Get*`), no new
  Terraform, no cost.
- **Freshness is shown, not hidden:** the provenance block names the source page (link to /admin/docs), the
  AA-CIS-Infra commit from the page's GENERATED marker and the bundle time. A value changes on the page after the next
  Infra docs run is merged into the root repo.
- **Catalog of explanations in the backend** (`CATALOG`, `GROUPS`): per value a plain label in English and Vietnamese,
  a unit code and the Terraform variable or resource that sets it; per group what it does, why it is not a setting,
  how to change it, and the admin-editable layer above it with a link (Platform › Settings for WAF / throttles, Spend
  for budgets). Same pattern as the settings guides (bilingual text from the API); the page chrome and unit names are in
  `messages.ts`. A unit parity test keeps the backend `UNITS` equal to the page's `InfraUnit`.
- **Never lose a value:** a group or key of the output the catalog does not know is still listed ("Not described
  yet", the key shown); a catalog key the page no longer has is listed as stale (renamed or removed in Terraform).
- **No account labels on the page (review fix).** The admin UI never prints acc1 / acc2 / acc3, but four budget
  output names of `platform_limits` carry one (`acc2_total_monthly_usd`, `acc2_total_alerts`,
  `acc2_cost_anomaly_alert_usd`, `acc2_per_app_monthly_usd.<app>`: 9 rows live). `display_key()` drops the label token
  (`total_monthly_usd`, `per_app_monthly_usd.cis`) and turns any label left inside a word into "account"; every name in
  the answer (item `key`, an unknown group's id and title, `missing_keys`) is a display name, while `CATALOG` matching
  keeps the raw output name. Two raw names that differ only by the label get a " (2)" suffix so row ids stay unique.
  Renaming the outputs in AA-CIS-Infra was the alternative; it changes a public Terraform output and the generated root
  page for a cosmetic reason, so the CIS side strips it instead. A test asserts no item key, group or missing key of
  the live fixture (and of synthetic acc-named rows) matches /acc[0-9]/.
- **Missing page = 200 with a reason** (`not_published`, `unavailable`, `owner_tier`), so the page explains instead of
  failing.
- **Route under `/admin/platform/*`** (`admin_platform_ops.py`, ops.read): the BFF's `platform` read rule already makes
  it admin only; PAGE_RULES `/admin/platform` covers the page. Nav: Platform › "Infra limits" (icon Server), after
  Settings.

## Changed

- Backend: `api/services/platform_infra.py` (new), `GET /admin/platform/infra` in `api/routers/admin_platform_ops.py`,
  POLICY row in `api/core/staff_authz.py`; snapshots `aa882_staff_route_policy.json`, `aa1035_role_route_matrix.json`
  and `docs/api/openapi.json` regenerated (additions only).
- Tests: `tests/unit/test_aa1096_platform_infra.py` with the live section of 14/11/2026 as fixture
  (`tests/unit/fixtures/aa1096_ops07_alarms.md`).
- Review fix: `display_key()` in `platform_infra.py` (names in the answer without account labels) plus three tests.
- Frontend: `app/admin/platform/infra/{page.tsx,InfraLimits.tsx,messages.ts}`, `lib/platformInfra.ts` + test,
  `lib/adminIa.ts` (nav entry), `app/_kit/adminNav.tsx` (icon; not a vendored file), `app/_kit/nav.messages.ts` (EN/VI
  label), visual page list and UI smoke list.
- `CONTEXT.md`: section "Infra limits", Platform nav list.

## Tradeoffs

- **Parsing Markdown** couples the page to the generator's table shape (`| \`key\` | \`value\` |` under `### group`).
  The alternative (a JSON file published next to the page) needs a docs-builder rule change in the root repo and an Infra
  generator change for no gain today; the fixture test pins the shape, and a parse that finds no section reads as
  "not published", never as wrong numbers.
- **Explanations live in CIS** while the values live in AA-CIS-Infra: a renamed output key shows as "Not described
  yet" plus a stale catalog key until `CATALOG` is updated. Accepted: visible, not silent.

## Should know

- Screenshots and the signed-in check are the orchestrator's (the page is admin only; the E2E reviewer account is
  refused, so the staff smoke run records the refusal).
- The page reads the docs bundle with the API's own read grant; on a Dev where Docs sync has not published OPS-07 the
  page shows the "not published" note.

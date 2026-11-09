# AA-1039 — R6 slice 1: CIS admin information architecture (lane l7-r6-admin-ia, S266)

Scope of this slice: the seven nav groups of root ADR 0019 d13 (TL-13 `regroup_lab`), moves and redirects of the pages
whose group changed, the AI & spend group, and a hidden slot for Tenants › Plans & add-ons. Not in this slice: Writing
Lab per stage, the AA-subject gate (d21), the tour retirement fan-out (d20), RBAC changes (R2). Plan: root
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §6.2.

## Decisions

- **Nav model as plain data.** `frontend/lib/adminIa.ts` holds the groups, items (href, label, icon key, `hidden`),
  the route moves and the frozen list of S265 nav URLs. `app/_kit/adminNav.tsx` only maps icon keys to lucide icons.
  Reason: `next.config.ts` (redirects), the node unit test, the Playwright smoke spec and two Python unit tests
  (`test_aa977`, `test_aa992`) all need the same table without React/JSX.
- **No sections.** The S241 two-section split (Platform / CIS Content) is gone; the seven groups are the top level and
  each folds through the S264 kit NavGroup. "Platform" is now a group name, so keeping a section of the same name
  would have been confusing. `useNavSections.ts` (localStorage `cis_nav_closed`) is deleted; the stale key is harmless.
- **Settings is an AA Catalogue item**, not a footer entry (IA: catalogue settings). Label kept "Settings" because the
  page still has tenant and UI kit tabs; the tab split is a later slice.
- **URLs move only where the IA names a new prefix**: Tenant content QA → `/admin/tenant-qa/*` (the IA gives
  `/admin/tenant-qa/review-log`), Tenants → `/admin/tenants/*`. AA Catalogue, AI & spend, Writing Lab, Platform and
  Home pages keep their URLs: Booking deep-links `/admin/supply-content/<id>`, e-mails and runbooks link the review
  pages, and the IA does not ask for new URLs there.
- **Moves:** `content-quality` → `tenant-qa/quality`, `tenant-activity` → `tenant-qa/trace`, `platform-stats` →
  `tenant-qa/review-log`, `atom-curation?section=slate` → `tenant-qa/slate` (new page, section extracted),
  `platform/agencies` → `tenants/agencies`, `data-erasure` → `tenants/data-erasure`.
- **307, not 308** (`permanent: false`): browsers cache 308s for good and later R6 slices may move pages again; same
  reason as AA-665's `/admin/llm-usage`.
- **Slate** is tenant content (it lists `acp_shared.subject` per tenant), so it left the AA atom store page. The section
  moved unchanged into `tenant-qa/slate/SlateSection.tsx`; its copy stays in `atom-curation/messages.ts` (AT) so the
  EN/VI text did not move twice. Its fetch is an inline react-query call (the page's `usePlatformFetch` stayed local).
- **Sub-nav split.** `SocialContentSubNav` (file name kept) renders either the Atoms & routes sections (01–04) or the
  Tenant content QA pages, picked by the `active` key; numbers dropped from the QA labels ("06 · Content Trace" →
  "Content Trace", "07 · A4 Quality" → "Review log").
- **Labels:** Social Content → Atoms & routes, A4 Quality → Review log, Content Quality → Content quality,
  Agencies (CRM) → CRM agencies, Supply (Booking) → Supply copy, AA content (CRM) → AA content, Staff → Staff &
  access, Apps → App health, Tenants item → Directory. "AI providers" and "Cost Operations" keep their names: guides,
  cross-links and copy in several pages use them; renaming belongs to the slice that merges Cost Operations and
  Cost per app into one Spend page.
- **Nested hrefs.** `/admin/tenants` and `/admin/tenants/agencies` both match `/admin/tenants/agencies`; the sidebar and
  breadcrumb take the longest match (`isActiveEntry`, `activeGroup`), so one item is active at a time.
- **Routing by data class.** `pii_class` does not exist (no column in `db-schema-reference.md`, no API field). The
  Routing tab gets a "Routing by data class" card that lists stages per class once `GET /api/admin/llm-config` sends
  `pii_class`, and until then shows a kit EmptyState saying routes are not checked against the rule yet. No backend.
- **Anthropic:** untouched (no key provided); the Connections view is as before.

## Changed

- Frontend: `lib/adminIa.ts` (new), `app/_kit/adminNav.tsx`, `Sidebar.tsx`, `nav.messages.ts`, `useNavSections.ts`
  (deleted), `lib/rbac.ts` (PAGE_RULES `/admin/tenant-qa`; old prefixes dropped, `/admin/tenants` covers the moved
  tenant pages), `next.config.ts` (spreads `adminIaRedirects()`), page moves (git mv) with relative imports one level
  deeper, `tenant-qa/page.tsx` (index → quality), `tenant-qa/slate/*` (new), breadcrumbs on AI providers, CRM agencies
  and Writing memory, the Routing tab card, `lib/aiGateway.ts` `stagesByDataClass`.
- Backend: `api/services/data_coverage/registry.py` drill links point at the new URLs (no redirect hop);
  `services/quality/lint.py` reads `frontend/app/admin/tenant-qa/quality/shared.ts` (it silently skipped a missing file,
  so the path had to follow the move).
- Tests: `frontend/lib/adminIa.test.mjs` (new, 9 checks), `frontend/lib/aiGatewayDataClass.test.mjs` (new), path fixes in
  `kitAiNotice`, `adminUiS263`, `crmConsole`, `tenantMembers`, `kitCore` tests; Python `test_aa977`, `test_aa992` read
  the nav from `lib/adminIa.ts`; smoke `aa752-shell` (e)/(f) for the seven groups, `ui-smoke` page list, new
  `aa1039-admin-ia-redirects.spec.ts` (smoke-public: every move is a 307 with the query kept, every S265 nav URL ends
  below 400 without a session); `admin-evidence` page list.
- `CONTEXT.md`: admin console layout, page list, role table, Content quality path.

## Tradeoffs

- Moving page folders touches many files (imports one level deeper) but gives the groups real URL prefixes; a
  nav-only regroup would have left `/admin/platform-stats` under Tenant QA and `/admin/platform/agencies` under Tenants.
- Content quality's Cards tab (place cards) belongs to AA Catalogue › Places & photos per the IA. It stays a tab of the
  QA page in this slice; splitting it out is a page change of its own (follow-up).
- The legacy `tests/e2e/aa5xx-*.spec.ts` live-verify specs (chromium project, not run in CI) still use the old URLs;
  they keep working through the 307s except where they assert the URL.

## Should know

- Old URLs: the table is `ADMIN_ROUTE_MOVES`; add a row there (never in `next.config.ts` by hand) when a page moves,
  and keep `PRE_R6_ADMIN_ROUTES` append-only.
- The Plans & add-ons slot: `lib/adminIa.test.mjs` test 6 fails once `/admin/tenants/plans/page.tsx` exists while the
  slot is still `hidden` — drop the flag in the same PR.
- Root docs that still name old URLs (all keep working through the 307s): `docs/guides/crm-runbook(.vi).md`,
  `docs/legal/dsar-erasure-runbook.md`, `docs/reports/catalog.yaml`, skills `aa-ecosys-repos`, `aa-ui-verify`.
- Vendored copies: none of the changed files is in the kit core or a vendored list, so no Partner/Booking sync PR.

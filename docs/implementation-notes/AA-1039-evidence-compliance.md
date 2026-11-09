# AA-1039 — admin evidence covers Tenant QA › Compliance (lane s270-evidence-specs, S270)

Test-only. Earlier notes: `AA-1039-r6-admin-ia.md`, `AA-1039-r6-gate-fanout.md`, `AA-1039-r6-writing-lab.md`,
`AA-1039-r6-rest.md`.

## Decisions

- `/admin/tenant-qa/compliance` joins `PAGES` of `tests/e2e/admin-evidence/admin-pages.spec.ts` (the Dev-only E2E CIS
  admin, A10b). `lib/rbac.ts` gives the `/admin/tenant-qa` prefix to TENANT_QA, which includes `admin`, so the session
  is not bounced. The visual job cannot shoot this page: its E2E reviewer is not in TENANT_QA.
- Ready marker: `[data-testid="admin-content-ready"]`. The page renders that marker in three states: error (the API
  error string, or "Compliance is not available yet." on a 503), empty ("Nothing here.") and loaded (the table).
- To keep an error page from passing as evidence, a new `LOADED` map asserts that the marker also holds a table row or
  the empty text. A positive check, not "the error text is absent": the error branch prints `String(q.error)`, whose
  text is not fixed, so only the loaded and empty shapes can be named.

## Changed

- Nothing in the app. The header comment of the spec now lists Tenant QA compliance among the covered pages.
- Screenshots: `tenant-qa-compliance-{desktop,mobile}-{light,dark}.png` under `tests/e2e/results/admin-evidence/`.

## Tradeoffs

- The `LOADED` check runs at 1440 wide, where the kit `DataTable` renders a `<table>`; the mobile shots come after.
- The empty text is matched in English (pages open in English; the run never switches the admin language).

## Should know

- `tests/**` is outside the paths of `deploy-dev.yml`: the merge does not redeploy ECS.
- Run: `cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 npx playwright test --project admin-evidence admin-pages`.

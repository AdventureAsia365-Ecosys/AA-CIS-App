# AA-816 S2 path: viewer-capped Preview sign-in, smoke split, API legacy flag off

Approved by Nghiệp in S247 (AA-816 S247 comment, AA-911). Three CIS branches, merged in order:

1. `feat/aa-816-preview-viewer-signin` (P6a): staff sign-in on the fixed Preview host through the Preview-only
   Cognito client, capped at viewer by the API. The CIS to Booking token bridge stays (P6b, end of the project).
2. `test/aa-816-smoke-split`: per-PR previews run only the unauthenticated smoke; the authenticated smoke signs in
   with the Cognito E2E user on the fixed Preview host and on Production.
3. `feat/aa-816-s2-api-legacy-off` (S2): the API refuses HS256 sessions on `/auth/verify-admin`.

## Branch 1: P6a

### Decisions

- **Ported from the AA-911 C1 branch (`refactor/aa-911-retire-booking-bridge`), bridge parts left out.** Taken:
  the D-1 API cap (`identity_config` preview client + `staff_identity.resolve_staff` viewer cap, commit `bf3650a9`),
  the client-id set test change (`24d1ac65`), the filled-in preview client id (`2290869c`), and from `1b5b9060`
  only the Preview-host pieces: `frontend/lib/cognito.ts` (`VERCEL_ENV` origins, `PREVIEW_ORIGIN`,
  `isPreviewSignInHost`), the login page and BFF `/api/auth/login` refusing the password form on the Preview host,
  `X-Robots-Tag` on preview builds, and the opt-in `preview-alias` job in `vercel-deploy.yml`. Left out:
  `api/core/booking_jwt.py` removal, `/auth/booking-token` and JWKS removal, `NO_BOOKING_ROLES` removal and the
  deleted bridge tests. Those stay on the C1 branch for P6b.
- **The preview client id is filled in** (`3d6u7r00ljf4m80gjil20fp2kg`, created by AA-CIS-Infra #260). It is a
  public value (it appears in every authorize URL), like the production client ids already in code.
- **New test: the bridge refuses a preview Cognito token** (`/auth/booking-token` → 401, no DB access). The bridge
  reads only HS256 password sessions, so keeping it does not give a Preview session a Booking token.
- **Comments that said "empty until the Infra apply"** now say what an empty value means.

### Changed

- Ship plan P6 (C1) was one PR (bridge retirement + preview sign-in). S247 splits it: P6a now, P6b (bridge
  retirement) at the end of the project.

### Tradeoffs

- Merging the Preview sign-in before the bridge retirement keeps one more auth path live for a while. It is safe for
  Preview because the bridge needs an HS256 password session and the Preview host never offers the password form.
- The viewer cap is per client id, not per host: a production-client token sent from preview code would keep its
  role, but preview code cannot get one (the production client has no Preview callback, and a preview build never
  reads the production client secret).

### Should know

- The viewer cap holds only while the Vercel `aa-cis-frontend` **Preview** `ADMIN_SECRET` is the C9 Preview
  credential (ship-plan check V2): the main secret with a forged `x-admin-role: admin` passes on a path without a
  Bearer. Check V2 before the Preview sign-in check.
- Vercel Preview env var to set (Nghiệp or the orchestrator, Preview target only, Sensitive):
  `COGNITO_CIS_ADMIN_PREVIEW_CLIENT_SECRET` = the `cis-admin-preview` client secret. Never a Preview target of
  `COGNITO_CIS_ADMIN_CLIENT_SECRET`. The alias must point at a preview built after the variable was set.
- Tests run locally: `test_aa911_preview_client_cap.py`, `test_identity_verifier.py`,
  `test_aa854_staff_bearer_middleware.py`, `test_aa854_verify_admin_cognito.py`, `test_aa878_viewer_role.py`,
  `test_aa656_staff_roles.py`, `test_s241_admin_routes_require_auth.py`, `test_aa680_booking_jwt.py` (604 passed,
  1 skipped); flake8 on touched files; `tsc --noEmit`; eslint on the touched frontend files.

## Branch 2: smoke split

### Decisions

- **Split by tag, not by folder.** Specs that load the smoke session carry `{ tag: '@staff' }` on their describes
  (`ui-smoke.spec.ts`, `aa752-shell.spec.ts`, `aa858-ops-overview.spec.ts`). Project `smoke` = `grep: /@staff/`
  (needs `smoke-setup`), project `smoke-public` = `grepInvert: /@staff/` (no setup). No file moves, so links in
  docs stay valid. A unit test (`tests/unit/test_aa816_smoke_split.py`) fails if a spec uses `ADMIN_STATE` without
  the tag, and pins the workflow shape.
- **Signed-in smoke = the Cognito E2E user**, the same mailbox flow as `e2e-cognito.yml`; the email-code steps moved
  to `tests/e2e/helpers/cognitoSignIn.ts` and both the E2E spec and the smoke setup use them.
- **Where it runs:** only from `main` (the mailbox role trusts `refs/heads/main` + environment `e2e`), only on the two
  callback hosts. Production after every production deploy (`smoke-production`), the fixed Preview host after an
  alias-only dispatch (`smoke-preview-host`). A PR labelled `preview-signin` runs on `refs/pull/...`, so its signed-in
  smoke is a separate dispatch of `ui-smoke.yml` from `main`.
- **Role of the session:** read from the `cis_role` display cookie, which the BFF writes from the
  `/auth/verify-admin` answer at the end of the Cognito callback (the browser never writes it on this path). On the
  Preview host the setup fails unless the role is `viewer`: that is the live evidence of the cap.
- **Reviewer mode (Production):** the E2E user is a reviewer, so a non-admin, non-viewer run takes the page list
  from `frontend/lib/rbac.ts` `PAGE_RULES` (`canSeePage`, dependency-free, imported by the test): a page it may not
  open must redirect to its landing page. No write test runs on Production. Viewer mode keeps its `viewerRefused`
  flags, and a public test checks they equal `PAGE_RULES`.
- **No code leak:** `smoke-setup` has no retries (a retry asks for another code), no automatic screenshots or
  traces, and the signed-in job uploads screenshots only, never the HTML report (a `fill` step names the code).
- **Concurrency:** the signed-in job shares the group `e2e-cognito` with `e2e-cognito.yml` (one mailbox, one user).
- The legacy password mode of the setup stays for local runs until S2; CI no longer passes `E2E_ADMIN_*`.

### Changed

- `aa816-admin-logout.spec.ts` and `aa816-cognito-auth-routes.spec.ts` treat the fixed Preview host like
  Production (it is a registered callback / sign-out URL of the preview client since P6a).
- `npm run test:e2e:smoke` runs both projects; `npm run test:e2e:smoke-public` only the public one.

### Tradeoffs

- PR previews lose every authenticated page check. Authenticated coverage moves to after merge (Production, as a
  reviewer) and to opt-in Preview-host runs (as the viewer). Admin-only pages are no longer page-checked by CI at
  all (no admin test identity: the E2E user is a reviewer and Preview is capped at viewer).
- One Cognito access token (15 min) and one refresh token are shared by every test context from the storage state.
  A run longer than the access token relies on `/api/auth/refresh`; with refresh-token rotation, parallel contexts
  refreshing with the same old token could fail after Cognito's grace period. Today's run is well under 15 min
  (workers: 1).
- The Preview-host run comments on the PR of the main commit it checks out (no PR of its own).

### Should know

- Preconditions for the Preview-host run: P6a deployed (API knows the preview client), Vercel Preview
  `COGNITO_CIS_ADMIN_PREVIEW_CLIENT_SECRET` set, the host aliased to a preview built after it, check V2 done.
- Ran locally: `npx playwright test --project=smoke-public` against the production host (44 passed, 1 skipped:
  the "no registered callback" test skips on callback hosts); `--list` for `smoke`, `smoke-public`, `cognito`;
  an ad hoc `tsc --noEmit --strict` over the changed e2e files and `playwright.config.ts`;
  `tests/unit/test_aa816_smoke_split.py`, `tests/unit/test_aa885_portal_split.py`.
- The signed-in job has not run yet (it needs `main`). First check after merge: dispatch
  `ui-smoke.yml` from main with `base_url=https://aa-cis.lumiguides.it.com staff_session=true`.

## Branch 3: S2, API legacy flag off

### Decisions

- **Where the flag was set:** nowhere for the API. `LEGACY_ADMIN_LOGIN_ENABLED` is not in the CIS api task
  definition (grep AA-CIS-Infra, 22/10/2026), so the API ran on the code default (on). Vercel `aa-cis-frontend`
  Production holds `false` since S1. So S2 is a code change: the default flips to **off** in
  `api/core/staff_identity.legacy_login_enabled` and, with the same values, in `frontend/lib/staffSession.ts`
  `legacyAdminLoginEnabled`. Only an explicit `1`/`true`/`yes`/`on` turns it back on (fail closed: unset, empty or
  a typo = off). The old rule was the reverse (only an explicit off value turned it off).
- **BFF default flipped too.** With the API refusing HS256, a password form on a per-deployment preview would
  "succeed" at `/auth/admin-login` and then bounce at the first verify. Off by default hides it. Production already
  had `false`.
- **Booking unaffected (checked in code):** `/auth/admin-login` and `/auth/booking-token` (`api/main.py`) do not read
  the flag and never call `/auth/verify-admin`; the booking-token handler decodes the HS256 token itself
  (`verify_jwt`) and re-reads the row. AA-Booking `frontend/app/api/auth/login/route.ts` + `lib/session.ts` call
  only those two routes. A test mints a Booking token from a password session with the flag unset.
- **Other HS256 readers stay as they were:** `core/staff_secret.py`, `v1_pipeline._require_admin` and the rate
  limiter only decode a Bearer to answer 401/403 or to key a bucket; none turns an HS256 token into a staff session.
- **Smoke setup:** the legacy password mode is removed (it can no longer work). Without `E2ECOGNITO=1` the setup
  fails with a message pointing to `--project=smoke-public`.
- **Tests:** the legacy-path tests in `test_aa656_staff_roles.py` and `test_aa878_viewer_role.py` now set the flag
  on explicitly (they cover the rollback path); `test_aa854_verify_admin_cognito.py` flips its flag cases; new
  `tests/unit/test_aa816_s2_legacy_off.py` (HS256 from admin-login refused, Cognito accepted, Booking bridge still
  mints, BFF and API parse the same "on" values).

### Changed

- The cutover plan (G) said "API flag off (Infra task-definition env, HAND EDIT in VSCode)". The env var is not set
  there at all, and setting `false` in Terraform would leave the code default on for every other environment
  (tests, a new task definition). Flipping the code default is one reviewed change in one repo and fails closed.
  Setting the env var to `true` in the task definition stays available as the rollback.

### Tradeoffs

- Rollback by env var now needs two places (API task definition and the Vercel target), or a revert of this
  commit (one Deploy Dev + one Vercel production deploy). A revert is the simpler rollback.
- Live HS256 sessions end at once (their next verify, at most the BFF verify cache of 5 min): by design.
- After S2 no agent can sign in to the CIS UI locally (no mailbox access); authenticated checks are CI-only
  (the signed-in smoke from main) or Nghiệp in a browser.

### Should know

- Before merge: branch 2 (smoke split) is merged and live, otherwise every PR smoke goes red after this deploys.
  Check Vercel `aa-cis-frontend` env: if the **Preview** target holds `LEGACY_ADMIN_LOGIN_ENABLED=true`, remove
  it (the password form would come back on previews and fail at verify).
- After deploy: disable `e2e-claude-code` on the Staff page (audited `staff.disabled`); delete the GitHub secrets
  `E2E_ADMIN_USERNAME` / `E2E_ADMIN_PASSWORD` once nothing reads them (the legacy `chromium` live-verify specs in
  `tests/e2e/*.spec.ts` still name them but no workflow runs them); update the `e2e-admin-login` memory and the root
  `aa-ui-verify` skill and `legacy-login-flag.md` runbook (rollback semantics changed: unset = off).
- Tests run locally: `test_aa816_s2_legacy_off.py`, `test_aa854_verify_admin_cognito.py`, `test_aa656_staff_roles.py`,
  `test_aa878_viewer_role.py`, `test_aa854_staff_bearer_middleware.py`, `test_aa911_preview_client_cap.py`,
  `test_s241_admin_routes_require_auth.py`, `test_aa680_booking_jwt.py`, `test_aa816_smoke_split.py`; flake8;
  frontend `tsc --noEmit` + eslint on the touched files; ad hoc `tsc --strict` on the changed e2e files.

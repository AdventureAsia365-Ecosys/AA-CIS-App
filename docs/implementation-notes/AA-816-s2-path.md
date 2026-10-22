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

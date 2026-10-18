# AA-816 — staff sign-in on our own domain (CIS frontend)

S242 follow-up. Chrome Safe Browsing flagged lumiguides.it.com as deceptive after staff sign-in went live:
`/api/auth/signin/cognito` redirected to the Cognito prefix domain
`aa365-staff-auth-dev.auth.us-west-1.amazoncognito.com`, an unbranded AWS page asking for email + code.
The Infra PR (same issue) puts WORKFORCE managed login on `staff-auth.lumiguides.it.com` and brands it.
This PR is the CIS frontend half. Separate file from `AA-816.md` so it does not collide with the other lanes.

## MERGE ORDER — read first

**Merge this PR only AFTER the Infra apply**, once `https://staff-auth.lumiguides.it.com/oauth2/authorize`
resolves and serves managed login (check: `curl -sI https://staff-auth.lumiguides.it.com/login` gives an
HTTP answer from CloudFront, not a DNS error). Merged earlier, every company-email sign-in, token
refresh and logout fails (`unreachable` → users bounce to /login). Rollback without a deploy: set
`COGNITO_WORKFORCE_DOMAIN=https://aa365-staff-auth-dev.auth.us-west-1.amazoncognito.com` in Vercel
(the prefix domain keeps working; a pool can have both domains).

## Decisions

- **Single constant with optional env override** in `frontend/lib/cognito.ts`:
  `DEFAULT_WORKFORCE_DOMAIN = "https://staff-auth.lumiguides.it.com"`, overridden by
  `COGNITO_WORKFORCE_DOMAIN`. The file already had the env name; only the default changed. Changed `??` to
  "trim, drop trailing slash, empty means default" so an empty Vercel value cannot produce relative URLs.
  Nghiệp does not need to set anything.
- **All four uses go through that one constant**: `/oauth2/authorize` (start), `/oauth2/token` (code
  exchange + refresh), `/oauth2/revoke`, `/logout`. Callback and logout-return URLs stay on
  `aa-cis.lumiguides.it.com` (unchanged; they are registered on the clients, not tied to the domain).
- **Issuer unchanged**: `WORKFORCE_ISSUER` (`cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS`) does not
  depend on the hosted domain, so the ID-token `iss` check and the backend JWT verification are untouched.
- **Backend `api/` does not use the hosted domain** — confirmed with grep (`amazoncognito`, `oauth2/`,
  `auth.us-west-1`): only the issuer (`api/core/identity_config.py`). The one hit in
  `tests/unit/test_identity_verifier.py` uses the prefix domain as a *wrong* issuer value; still correct.
- **No CSP to update**: `next.config.ts`, `vercel.json` and `middleware.ts` set no Content-Security-Policy
  (the sign-in is a top-level navigation, token calls are server-side).
- **/login copy**: subtitle "Staff Login" → "Staff sign-in"; button keeps "Sign in with company email"; one
  muted line under it (only when Cognito is available on this host): "No password needed: a one-time
  sign-in code arrives by email. Sign-in opens on staff-auth.lumiguides.it.com." The host is read from the
  same constant (`workforceSignInHost()`, passed from the server page), so the text cannot drift from the
  real redirect. Naming the host up front makes the hand-off expected rather than surprising.

## Changed

- `frontend/lib/cognito.ts`: new default domain, normalisation, `workforceSignInHost()` export.
- `frontend/app/login/page.tsx`: passes `signInHost` to the form.
- `frontend/app/login/LoginForm.tsx`: subtitle + the email-code note.
- Booking / TripPlanner: not touched (out of scope for now).

## Tradeoffs

- Kept the env override instead of hard-coding: it is the zero-deploy rollback path if the custom domain
  (CloudFront distribution owned by Cognito) misbehaves.
- Did not add a frontend unit test: the frontend has no test runner (`package.json` has no `test` script);
  the change is a constant. The Playwright smoke `tests/e2e/smoke/aa816-cognito-auth-routes.spec.ts` does not
  reference the Cognito host, so it is unaffected.

## Should know

- Sessions started on the prefix domain survive the switch: refresh tokens are pool-level, so
  `/oauth2/token` on the new domain accepts them. The managed-login session cookie, however, is per domain:
  a user who signed in on the old domain and logs out after the switch has the cookie cleared only on the new
  domain. Harmless (the old one expires; nobody is redirected there any more).
- If `COGNITO_WORKFORCE_DOMAIN` was ever set in Vercel to the prefix domain, it overrides this default and
  must be removed. Review round (S242) checked Vercel → aa-cis-frontend env list: the variable is **not
  set**, so the new default takes effect. Re-check only if someone adds it before merge.
- Merge order: only after the Infra apply (AA-CIS-Infra `feat/aa816-staff-auth-domain`) and once
  `curl -s -o /dev/null -w '%{http_code}' https://staff-auth.lumiguides.it.com/.well-known/openid-configuration`
  returns 200. Merged earlier, every company-email sign-in, token refresh and logout fails. Rollback without a
  deploy: set `COGNITO_WORKFORCE_DOMAIN=https://aa365-staff-auth-dev.auth.us-west-1.amazoncognito.com`.
- Verification after merge + Vercel deploy: on `https://aa-cis.lumiguides.it.com/login` click
  "Sign in with company email" → the browser goes to `https://staff-auth.lumiguides.it.com/oauth2/authorize?…`
  (branded page), the code email arrives, callback lands on `/admin/...`; Logout returns to /login through
  `staff-auth.lumiguides.it.com/logout`.
- Checks run: `eslint lib/cognito.ts app/login/` clean (full `npm run lint` has the pre-existing 54 errors /
  109 warnings, none in the changed files), `tsc --noEmit` clean, `next build` OK.

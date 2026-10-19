# AA-816 — staff sign-out ends the managed-login session (cutover prep, S244)

Scope line from the S243 backlog: "CIS logout also ends the Cognito session (`staff-auth` `/logout`, as Booking does)".
Cutover plan for the remaining Stage S items: in the private AA-Ecosys root repo,
`docs/audits/2026-10-19-S244-aa816-cutover/cutover-prep.md` (it describes staff accounts and break-glass state, so it
does not belong in this public repo).

## Decisions
- **The main change was already live.** C4 (#811) made `POST /api/auth/admin-logout` revoke the refresh token,
  clear every session cookie and answer the managed-login `/logout` URL (`client_id` + `logout_uri=<prod>/login`);
  the Topbar navigates there with `window.location`. Checked on 19/10 before writing code:
  - the live `cis-admin` and `breakglass` app clients already list `https://aa-cis.lumiguides.it.com/login` as a
    sign-out URL (`describe-user-pool-client`, read-only), matching Infra `identity_variables.tf`;
  - production answers a same-origin POST carrying a Cognito-shaped refresh cookie with
    `https://staff-auth.lumiguides.it.com/logout?client_id=…&logout_uri=https%3A%2F%2Faa-cis.lumiguides.it.com%2Flogin`
    (the new smoke test, run read-only against production, 2 passed);
  - `e2e-cognito.yml` run 39207450965 (S243) passed "sign-out does not silently re-login".
  So the S243 note "CIS logout should also call Cognito `/logout`" described something that already worked. This
  branch closes the remaining differences with Booking and makes the proof stronger instead.
- **No Infra branch.** Nothing to add: both CIS staff clients already have the production sign-out URL (and localhost
  for `cis-admin`). The portal is the tenant app (EXTERNAL pool, `cis-portal` client); staff never sign in there.
  Booking registers `aa-booking-admin.vercel.app` because that is a second *production* domain of its project; CIS has
  no second production host in its redirect allow-list (`lib/cognito.ts` origins), and Vercel previews can never be
  callbacks (ADR 0006a A6), so there is no preview logout URL to register.
- **Same-origin check on the logout route** (strict, as Booking `lib/sameOrigin.ts`): a cross-site POST gets 403 and
  nothing happens (no revoke, no cookie cleared). Strict means a POST with neither `Origin` nor `Sec-Fetch-Site` is
  refused too; every current browser sends one of them on a `fetch` POST, so the Topbar is unaffected.
- **The Topbar follows only two shapes** (`lib/staffLogout.ts logoutNavigationTarget`): `/login`, or an `https:` URL
  with path `/logout` and both `client_id` and `logout_uri`; anything else (including the 403 body) goes to `/login`.
  Booking accepts any `https://` value; this is one step tighter and keeps a bad or empty answer from navigating
  anywhere unexpected.
- **E2E proof is "a new code", not just "the address form again".** Step 7 of `cognito-login.spec.ts` submits the
  address after sign-out, reaches the code step and waits for a second code mail in the SES mailbox (60 s cap). The
  code is never typed or printed (boolean shape check only), and the browser must still be off the CIS session.

## Changed
- `frontend/lib/sameOrigin.ts` (new): `isSameOrigin(req, { strict })`, same rule as Booking; typed on the two fields
  it reads so it is testable without Next.
- `frontend/lib/staffLogout.ts` (new): `LOGIN_PATH`, `logoutNavigationTarget(redirect)`.
- `frontend/app/api/auth/admin-logout/route.ts`: strict same-origin check first (403 JSON, `no-store`).
- `frontend/app/_kit/Topbar.tsx`: `logout()` uses `logoutNavigationTarget`.
- `tests/e2e/smoke/aa816-admin-logout.spec.ts` (new, runs in the PR preview smoke): unit checks of both helpers;
  cross-site POST → 403 and nothing cleared; same-origin POST without a session → `{ ok, redirect: "/login" }` and the
  four session cookies cleared; same-origin POST with a Cognito-shaped refresh cookie → the managed-login `/logout`
  URL on the production host, `/login` on a preview.
- `tests/e2e/cognito/cognito-login.spec.ts`: step 7 (second code), `test.setTimeout(240000)` for two mailbox waits.
- `CONTEXT.md`: sign-out and Cognito E2E lines.

## Tradeoffs
- A refused cross-site sign-out is only an annoyance attack, but the check costs nothing and keeps the two staff
  apps identical.
- Step 7 asks Cognito for one more code mail per E2E run (SES inbound, cost negligible; the object expires in a day).
  It also leaves one unanswered managed-login challenge, which expires on its own after 3 minutes.
- Not done here (follow-up candidates, see the cutover prep notes): a refused sign-in (`/login?error=no_access`)
  still leaves the managed-login session alive for up to an hour, so a person cannot switch to another address at
  once. Booking behaves the same. Fixing it means sending the refusal through `/logout`, which drops the
  `error` query (a sign-out URL must match a registered one exactly) unless a second sign-out URL or a short notice
  cookie is added.

## Should know
- Side effect on AA-Booking (review, S244): CIS and Booking staff clients share one WORKFORCE managed-login domain,
  so the Cognito `/logout` visit ends that shared managed-login session for Booking too. Booking's own session
  (its cookies and refresh token) is not touched: a staff member stays signed in to Booking until that session ends,
  and the next Booking sign-in then asks for a new email code instead of being silent. The reverse holds for a
  Booking sign-out. Sign-out is therefore per app, not a global sign-out; ADR 0006 does not state this for staff yet.
- The cognito spec runs only through `e2e-cognito.yml` on `main` (mailbox role trust). Step 7 is proven only by a
  dispatch after this PR is merged and deployed to production.
- The new smoke spec needs no login: it runs in the `smoke` project of every preview (`ui-smoke.yml`), and with
  `--no-deps` locally.
- Checks run: `npx eslint` on the changed files (clean); `npm run lint` (45 errors / 99 warnings, the same count as
  `origin/main`, none in changed files); `npx tsc --noEmit`; `npm run build` (OK); Playwright `--project=smoke
  --no-deps` on the new spec + `aa816-cognito-auth-routes.spec.ts` against a local `next start` (9 passed);
  `--project=cognito` locally (sign-in test skipped without `E2ECOGNITO=1`, mailbox unit tests 6 passed).

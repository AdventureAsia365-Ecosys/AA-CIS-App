# AA-1035 — view-as follow-ups (S268, lane s268-followups)

The "Open minors" of CIS #1303 (R2 slice 2, `AA-1035-s2.md`). No migration, no route signature change (no openapi or
matrix snapshot change); root ADR 0006a gets the A10b-2 entry in a separate root PR.

## Decisions
- **`POST /auth/verify-tenant` refuses a view-as token: 403 "A view-as token is not a tenant session".** The token is
  read from the body, so the view-as gate (Authorization header) never saw it and the route answered `valid: true`.
  A view-as token is recognised by the `vas` claim or `amr = staff_view_as` (either one is enough). A tenant session
  token answers as before.
- **The BFF DELETE (`/api/view-as`) ends only the signed-in member's own session**, like GET: `lib/viewAs.ts
  ownViewAsClaims(cookie, auth.adminId)` (the token's `stf` must be the member). A cookie another member left in a
  shared browser is only cleared, never ended through this member (an admin's end would revoke it).
- **Offboarding a tenant ends every open view-as session on it in the offboarding transaction**
  (`view_as.close_open_sessions(tenant_id=...)`): `end_reason` revoked (expired when already past its expiry), one
  `tenant.view_as_ended` row per session (actor the offboarding admin, cause `tenant_offboarded`, `session_staff`); the
  offboard audit row lists `ended_view_as_sessions` and the answer counts them.
- **"Staff sign-out" covers three paths.** (1) An owner disabling a member (`POST /admin/platform/staff/{id}/disable`)
  and (2) the owner "sign out everywhere" reset (`.../sign-out`) close the member's open sessions in the same
  transaction (`close_open_sessions(staff_id=...)`, cause `staff_disabled` / `staff_signed_out`, revoked; the staff
  audit row notes `ended_view_as_sessions` only when one was closed, so the usual row is unchanged). (3) The member's
  own sign-out (`/api/auth/admin-logout`): the BFF lists the member's open sessions (`GET
  /admin/view-as/sessions?open_only=true`, filtered to the member's own: an admin's list holds everyone's) and ends
  each through the existing end route (cause `ended_by_staff`), BEFORE the refresh token is revoked (the revoke also
  invalidates the access token the calls use), and clears the `aa_view_as` cookie (Set-Cookie on its own path).
- **The Dev-only E2E admin lists, reads and ends only its own sessions** (`_sees_every_session(principal, request)`
  is False for the `e2e_read_only` identity, so `/admin/view-as/sessions` is scoped `own` and another member's session
  is a 404). Its start / end stay limited to synthetic tenants (A10b-2, unchanged).

## Changed
- `api/core/view_as.py`: `close_open_sessions`, `CLOSE_TENANT_SESSIONS_SQL`, `CLOSE_STAFF_SESSIONS_SQL`,
  `BULK_END_CAUSES`, `NOT_A_TENANT_SESSION`; module docstring steps 4 and 5.
- `api/main.py` (verify-tenant), `api/routers/admin.py` (offboard), `api/services/staff_directory.py` (disable,
  sign-out), `api/routers/admin_view_as.py` (E2E scope).
- Frontend: `lib/viewAs.ts` (`ownViewAsClaims`, `ownOpenSessionIds`), `app/api/view-as/route.ts` (GET and DELETE use
  the owner helper), `app/api/auth/admin-logout/route.ts` (ends the own sessions, clears the cookie). The sign-out BFF
  now reads `ADMIN_SECRET` (legacy sessions), so it joined `_SECRET_BFFS` in `tests/unit/test_aa882_staff_authz.py`.

## Tradeoffs
- The member's own sign-out reuses the list + end routes instead of a new "end mine" route: no new route, POLICY row,
  matrix snapshot or openapi change, and the end route's own checks (own session, E2E synthetic only) still apply. The
  cost is two to three small calls on sign-out, bounded by a 4-second timeout; a failure never blocks the sign-out (the
  session then ends at its next request or its expiry, at most 60 minutes).
- The end_reason column keeps its CIS 364 vocabulary (ended / expired / revoked); the precise cause is in the audit
  row's `cause`, as for the gate.
- `close_open_sessions` runs its UPDATE in a savepoint so a database before CIS 364 never aborts an offboarding or a
  staff write.

## Should know
- Live check after deploy (Dev): start a view-as session on a synthetic tenant as the E2E admin, sign out, then
  `GET /admin/view-as/sessions?open_only=true` shows none and the session has one `tenant.view_as_ended` row; a
  view-as token on `POST /auth/verify-tenant` answers 403.
- Tests: `tests/unit/test_aa1035_s2_view_as.py` (E2E scope, a real admin still sees all, verify-tenant),
  `tests/unit/test_aa797_tenant_admin.py` (offboard closes in the transaction),
  `tests/unit/test_aa816_staff_directory_writes.py` (disable / sign-out), `tests/integration/test_aa1035_s2_view_as_pg.py`
  (offboarding, disable / sign-out and verify-tenant on real Postgres: CIS 364 guard trigger and audit chain),
  `frontend/lib/viewAs.test.mjs` (owner helper, DELETE / sign-out wiring).

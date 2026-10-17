# S241 — admin GET authz hotfix (GĐ −1)

Refs AA-816 (staff authz umbrella). Source: AA-Ecosys
`docs/audits/2026-10-17-S241-admin-console/README.md` §2, §11 R1/R2/R16.

## Decisions
- **Router-level `dependencies=[Depends(verify_admin_secret)]`** on the five routers whose every route
  is staff-only and whose reads were open: `admin_job_runner`, `admin_llm_ops`, `admin_budgets`,
  `admin_photos` (`router` only — `public_router` `/content/photos/{id}` stays public, it is the image
  redirect TripPlanner and published pages use), `admin_segment_research`. A new route added to these
  routers can no longer ship open.
- The write routes in those routers keep their inline `verify_admin_secret(x_admin_secret)` call
  (same check, run twice). Removing them would also mean removing the now-unused header params:
  a larger diff for a hotfix, no behaviour gain.
- `POST /admin/upload-staging/{id}/decide` (admin_pipeline) — found by the scan, not in the audit list:
  it declared `x_admin_secret` but never checked it, so a public caller could apply a staging
  decision (insert/supersede raw tours). Fixed per-route with the inline call its siblings use, as the
  first statement.
- Constant-time compare via a new `api/core/secret_compare.secrets_equal()` (hmac.compare_digest on
  UTF-8 bytes; None/"" never match). Applied to every `==`/`!=` secret compare found:
  `admin.verify_admin_secret`, `admin_settings.verify_admin`, `auth.verify_tenant_api_key` (X-Admin-Secret
  branch), `v1_rules._get_admin`, `v1_social._get_admin`, `v1_tours` catalog-review detail,
  `middleware/rate_limit` admin tracking branch. `v1_pipeline` already used compare_digest.
  Status codes, the 503 "not configured" branch and `x-admin-role` handling are unchanged.
- CI guard `tests/unit/test_s241_admin_routes_require_auth.py` accepts EITHER an auth dependency in the
  dependant tree OR an inline auth call at the top level of the endpoint body before any `await` /
  `return`. Most /admin routes (≈150) use the inline pattern; requiring the dependency form only would
  have meant rewriting every admin router in a hotfix. The "before any await" rule means no DB access
  can happen before the check. `ALLOWED_PUBLIC` is empty.
- Accepted auth dependencies: `verify_admin_secret`, `admin_settings.verify_admin`,
  `admin_atoms._require_atoms_staff`, `admin_pipeline._resolve_brand_tenant_id` (tenant JWT for the
  tenant's own brand, else the admin secret — AA-424).

## Changed
- 27 routes now require the admin secret (25 GETs + POST segment-research/preview + POST
  upload-staging/{id}/decide). Full list in the commit/PR.
- `deploy-dev.yml` smoke: `/admin/job-runner/workers` curl now sends `x-admin-secret` from the step env
  (`secrets.ADMIN_SECRET`), inside the existing `if [ -n "${ADMIN_SECRET:-}" ]` block, so a missing
  secret still skips the admin checks with the existing warning.
- `tests/unit/test_dedup_flow.py`: decide_staging calls pass the secret; new 403 test.
- Docstring of `admin_llm_ops.py` no longer says reads are open behind the BFF.

## Tradeoffs
- With a router-level dependency, a request with no secret AND an invalid body now gets 403 instead
  of 422 on those routers (dependencies resolve before body validation errors are returned). Callers
  that send the secret see no change.
- Inline checks are still allowed by the guard; a future clean-up could move every admin router to
  router-level dependencies and drop the inline form (then the guard can require the dependency).

## Should know
- Callers checked: admin BFF `frontend/app/api/admin/[...path]/route.ts` sends `X-Admin-Secret` for
  every method incl. GET; `frontend/app/api/tenant/pipeline/brand-identity/route.ts` only calls
  `/admin/brand-identity` (unchanged); no browser code calls the API URL `/admin` directly
  (`lib/api.ts` has no admin path). In-process samplers/schedulers make no HTTP call to /admin.
  AA-CIS-Infra `lambda_src/dfs_balance_check/handler.py` only POSTs secret-gated paths with
  `X-Admin-Secret` (no GET). AA-TripPlanner-Web uses only `/content/photos` (public, unchanged).
  AA-Booking only links to the CIS admin UI (`ManagedInCisLink`, `CIS_ADMIN_URL`), no API calls.
- Live check before the fix (17/10, anonymous GET): `/admin/job-runner/workers`, `/admin/budgets`,
  `/admin/llm-config`, `/admin/segment-research/status` → 200. After deploy they must answer 403.
- Still open (not in this PR, per scope): role checks for staff writes (BFF `canWriteAdminPath` default
  allow, R3+), `x-admin-role` trust model.

## Review follow-up (S241 review findings, same branch)

### Decisions
- **/v1/rules + /v1/social are staff-only now** (finding 1, confirmed in code). Their `_get_admin`
  accepted the admin secret OR any valid JWT via plain `verify_jwt`; a tenant JWT from
  `/auth/tenant-login` (role='tenant') could list every tenant's social posts, approve/reject them
  (`batch-review`, audit rows with a client-chosen reviewer_id) and switch off global
  `acp_output_rules`. Replaced by `api/core/staff_secret.require_staff_secret`, the AA-799
  `v1_pipeline._require_admin` rule shared: secret (constant time) → master principal; wrong secret →
  403; unconfigured → 503; any JWT → verified (401 if bad) then 403; nothing → 401. Attached
  router-level, so a new route on these routers cannot ship open. No caller in any repo uses these
  routes (grep: CIS frontend, TripPlanner, Booking, Infra) — the only callers are the existing tests,
  which already send `X-Admin-Secret`.
- `reviewer_id` in `POST /v1/social/batch-review` still comes from the body: only the staff BFF
  (secret holder, trusted) can call now, and the secret principal is a shared master principal with
  no individual identity, so taking it from the principal would record a constant instead of the
  reviewer. Revisit with the staff-identity work (AA-816 R3+, x-admin-user-id trust model).
- **CI guard scans every route, not only /admin** (finding 2). Rule inverted: each route needs an
  auth dependency or a strict inline staff check, else an `ALLOWED_PUBLIC` entry with a reason
  (/health, /auth/{tenant-login,admin-login,verify-tenant,verify-admin,booking-token},
  /auth/.well-known/jwks.json, /v1/trip/{tour_id}, /content/photos/{photo_id}, /v1/angle-gate/goals).
  `INLINE_REVIEWED` holds the one route whose inline check branches (`GET /v1/tours/{tour_id}/full`:
  admin secret or verify_tenant_jwt, 401 otherwise). Stale-allowlist check covers all lists.
- **Non-APIRoute entries are no longer skipped** (finding 3): `Mount` is recursed into when the mounted
  app has `.routes` (paths prefixed); any other mounted ASGI app and any plain Starlette
  `Route`/`WebSocketRoute` fails unless in `NON_API_ALLOWED` (empty). `APIWebSocketRoute` is checked
  through its dependant like an APIRoute. Negative self-test builds a throwaway app with a mounted
  FastAPI sub-app, a mounted raw ASGI app, an open websocket, an open /internal route and a
  tenant-JWT dependency on an /admin path, and asserts each is reported.
- **Stronger evidence** (finding 4): (a) an inline check is resolved through the endpoint's
  `__globals__` and must BE `admin.verify_admin_secret` or `admin_settings.verify_admin` (identity),
  with exactly one positional argument that is an endpoint parameter declared as the
  `X-Admin-Secret` header and not reassigned before the call. `main.verify_admin` (the /auth
  endpoint) or a query-param argument no longer counts. (b) Dependencies split into
  STAFF (`verify_admin_secret`, `admin_settings.verify_admin`, `_require_atoms_staff`,
  `v1_pipeline._require_admin`, `require_staff_secret`, `require_admin_secret_for_docs`) and
  TENANT_OR_STAFF (`_resolve_brand_tenant_id`, `v1_tours.get_tenant`). Staff-only prefixes
  (/admin, /v1/pipeline, /v1/rules, /v1/social) accept TENANT_OR_STAFF only via
  `ADMIN_TENANT_OR_STAFF` = the 4 brand-identity routes (AA-424).

### Tradeoffs
- The ~148 inline-only /admin routes were not migrated to router-level dependencies (finding 4's
  "better still"): too wide for a hotfix; the strict inline rule closes the name-only loophole.
- /v1/rules + /v1/social: a staff JWT alone no longer works either (403). Nobody sends one today.
- The tenant-JWT tests for /v1/rules + /v1/social mount the two routers alone (no rate-limit
  middleware, which would look the tenant up in Redis/DB); the DB pool raises if touched, proving the
  guard refuses before any query.

### Should know
- Live (17/10, anonymous GET, no headers): `/v1/rules` and `/v1/social` → 401 before and after; the
  hole needs a tenant JWT (not exercised live — login is a POST). After deploy, a tenant JWT must get 403.
- Local pytest imports `api.main`, which calls Secrets Manager for the Sentry DSN; run with
  `AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null` and no AWS_PROFILE to keep it offline.

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

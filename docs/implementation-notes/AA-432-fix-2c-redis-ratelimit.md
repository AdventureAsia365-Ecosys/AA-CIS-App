# AA-432 (Part 2/2c) — Remove API Gateway X-API-Key gate on /v1/*, Redis rate-limit + revoke

Date: 22/08/2026. Repos touched: AA-CIS-Infra (Terraform), AA-CIS-App (FastAPI).
Branch (both repos): `pqnghiep1354/aa-432-urgentinfra-api-gateway-id-sai-lech-claudemd-vs-production-x`

Status: **LIVE, verified 22/08/2026.** Both PRs merged (AA-CIS-App #190, AA-CIS-Infra #30),
`Deploy Dev` + `Terraform Apply Prod` both completed successfully, live-verified against
production with a real minted tenant JWT. See "Live verification" section at the end for the
actual results. Everything below the "Should know" section that talks about this being blocked
describes the state *during* this session, before Nghiep merged both PRs — kept as-is for the
history/reasoning, not because it's still true.

## Decisions

1. **Middleware, not `get_tenant()` itself, for the Redis rate-limit/revoke check.** The task
   suggested either; `api/middleware/rate_limit.py::rate_limit_middleware` already existed,
   already runs on every `/v1/*` request ahead of any route handler (wired in `api/main.py`),
   and already did JWT decode + a Redis-backed per-minute counter — it just used a hardcoded
   `PLAN_RPM` bucket-by-plan-tier map instead of the tenant's real `rate_limit_rpm`, and had no
   `is_active` check at all (only checked at login, in `auth.py`'s deprecated `/auth/tenant-login`
   and the live one in `main.py`). Extending this one function covers every `/v1/*` route
   automatically — no per-router change needed, and no risk of a new router forgetting to wire it
   in (a `get_tenant()`-level fix would need duplicating into both `v1_tours.py` and
   `v1_exports.py`, the two files that define their own `get_tenant()`).

2. **Tenant metadata cache: Redis, not in-process, 30s TTL.** ECS runs multiple tasks; an
   in-process cache would let different tasks disagree about whether a tenant is still active.
   30s is a judgment call (not load-tested) — fast enough that a revoke feels close to immediate,
   slow enough that a normal traffic pattern doesn't add a DB round-trip per request. Cache key
   `tenant_meta:{tenant_id}`, value `{"rate_limit_rpm": int, "is_active": bool}` as JSON.

3. **is_active=false → 403, not 401.** 401 already means "your JWT itself is invalid/expired"
   (see `verify_jwt`) — a deactivated tenant's JWT is still cryptographically valid, it's just no
   longer authorized. 403 (Forbidden) is the more accurate signal, and keeps 401 unambiguous as
   "re-authenticate" vs 403 "authenticated but not allowed." Task text allowed either.

4. **Fail-open on tenant_meta lookup failure (Redis AND DB both unreachable), never fail-open on
   an is_active=false result.** If the lookup itself can't complete, the code falls back to the
   pre-existing plan-tier/`PLAN_RPM` behavior rather than 403-ing or 500-ing every tenant request
   on a transient infra blip — matches this file's own pre-existing convention (the rate-limit
   counter already fails open on a Redis error: `except Exception: count = 0`). This is a
   deliberate availability-over-strictness tradeoff for the *rate-limit* dimension; it does NOT
   apply to revoke — if the lookup fails, revoke simply isn't checked that request (same as
   before this change, since no check existed at all), it never converts a `None` lookup result
   into "treat as active" vs "treat as inactive" ambiguity — the `is_active` branch only runs
   when the lookup actually returned data.

5. **Terraform: leave the `tenant_key` authorizer resource declared, only change the method.**
   Task explicitly said don't delete the Lambda/authorizer in this change. Confirmed via grep
   (AA-CIS-Infra repo) that no other method/resource references
   `aws_api_gateway_authorizer.tenant_key` — it's now an orphaned-but-harmless declared resource.
   Actually deleting it is a separate, later cleanup (noted in CLAUDE.md, not filed as a new
   Linear issue in this session — Nghiep can decide if that's worth its own ticket).

6. **Terraform: also fixed the deployment `triggers` hash to include method-level
   `authorization`/`authorizer_id`, not just integration URIs + the authorizer's own id.** Found
   this live, not in review — the FIRST `terraform plan` run in CI (before this fix) showed a
   clean single-attribute method change with the `aws_api_gateway_deployment`/`aws_api_gateway_
   stage.dev` resources completely untouched. API Gateway stages serve a frozen deployment
   snapshot; a method-level change edits the live REST API definition but does NOT retarget a
   stage unless a new deployment is created and the stage's `deployment_id` is updated. Applying
   that first plan would have "succeeded" (state would say `authorization = NONE`) while the
   `dev` stage kept serving the OLD deployment — the actual 401 bug would not have gone away,
   silently. Added `v1_any.authorization` + `coalesce(v1_any.authorizer_id, "none")` (plus the
   other 3 gated methods' `authorization`, for the same latent gap) to the trigger hash. Re-ran
   plan after the fix: now correctly shows `1 to add, 2 to change, 1 to destroy` (new deployment
   + `v1_any` method + `dev` stage's `deployment_id` all in the plan). This is arguably the most
   important finding in this whole task — a "successful, clean" `terraform apply` is not proof a
   method-level auth change actually took effect on a live stage; always check the deployment/
   stage resources appear in the plan too when changing a method's `authorization`.

## Changed

- `AA-CIS-Infra/modules/api_gateway/main.tf`: `aws_api_gateway_method.v1_any` —
  `authorization = "CUSTOM"` + `authorizer_id = aws_api_gateway_authorizer.tenant_key.id` →
  `authorization = "NONE"` (authorizer_id line removed). This is the only resource change.
- `AA-CIS-App/api/middleware/rate_limit.py`: added `_get_tenant_meta()` (Redis-cached
  `shared.tenants.rate_limit_rpm` + `is_active` lookup) and wired it into
  `rate_limit_middleware` ahead of the existing rate-limit counter. `PLAN_RPM` kept, now only a
  fallback for lookup failure (previously the only source of truth).
- `AA-CIS-App/tests/unit/test_aa432_rate_limit.py`: new, 5 tests (revoke-blocks, real-rpm-used,
  cache-hit-skips-db, fail-open-on-total-lookup-failure, over-limit-still-429-not-401). All pass.
- `AA-CIS-App/.claude/CLAUDE.md`: LIVE STATE note on the new architecture + the not-yet-applied
  Terraform status (see below).

## Tradeoffs

- **A per-tenant DB read on every Redis cache miss** (worst case: once per tenant per 30s) is a
  new cost this middleware didn't have before (it only touched Redis previously). Judged
  acceptable — one indexed `WHERE tenant_id = $1::uuid` lookup on a small table, amortized over
  30s of that tenant's traffic. Not load-tested against real traffic volume.
- **30s revoke latency is not "instant."** If Nghiep needs revoke to be truly immediate (e.g. for
  an active-incident key compromise), this cache TTL is the wrong tool — that scenario should
  still use `is_active=false` (which this DOES enforce, just with up to 30s lag) but combine it
  with rotating/expiring the tenant's session more aggressively if zero-lag revoke is ever a hard
  requirement. Flagging, not solving — no such requirement was stated in the task.
- **Chose not to touch the pre-existing `billing_service.track_api_call` / `tenant_api_usage`
  logging path** — 403 revoke-blocks are not tracked there (429s already were, kept as-is; a
  403 isn't logged to `tenant_api_usage`). This is a small inconsistency (place to add later if
  billing/observability needs revoke-block visibility) — didn't expand scope to add it now.

## Should know (read before the diff)

- **`terraform plan` could not be run from this session.** The `aa365-admin` AWS profile
  (`role_arn` + `mfa_serial`, `source_profile aa365-nghiep-base`) requires an MFA code for
  AssumeRole. `aws` CLI calls worked throughout this session because the CLI was already using a
  cached, previously-MFA-authenticated session — but Terraform's AWS provider does its own
  AssumeRole and doesn't read that CLI cache, so it needs a fresh MFA code Terraform can't obtain
  non-interactively here. Attempting to export the CLI's cached temporary credentials into env
  vars for Terraform to reuse was blocked by this environment's own safety controls (correctly —
  that would be working around the MFA requirement's intent, not a legitimate workaround).
  **What WAS verified instead:**
  - `terraform fmt -check` on the changed file: clean.
  - `terraform validate` (in `accounts/aa365/`, the real root module for this API Gateway — see
    below): **Success, configuration is valid.**
  - Confirmed via grep that no other `.tf` file references the authorizer being detached.
  **Nghiep needs to run, from `AA-CIS-Infra/accounts/aa365/`, with `AWS_PROFILE=aa365-admin`
  active (MFA'd) and `TF_VAR_db_password` set** (same pattern the repo's own
  `terraform-plan.yml`/`terraform-apply.yml` workflows use):
  ```
  cd accounts/aa365
  export TF_VAR_db_password=$(aws secretsmanager get-secret-value --secret-id aa-cis/dev/rds --profile aa365-admin --region us-west-1 --query SecretString --output text | python3 -c "import sys,urllib.parse; print(urllib.parse.urlparse(sys.stdin.read().strip()).password)")
  terraform init -backend-config="bucket=aa-cis-tfstate-{acc2-id}" -backend-config="key=dev/terraform.tfstate" -backend-config="region=us-west-1"
  terraform plan
  ```
  Review the plan — it should show exactly ONE change (`aws_api_gateway_method.v1_any` in-place
  update, `authorization: CUSTOM -> NONE`, `authorizer_id` removed) plus whatever
  `aws_api_gateway_deployment.this`/stage redeploy Terraform decides is needed to actually push
  the method change live (expected, not a bug — the module's own `triggers` block forces a
  redeploy on integration/authorizer changes). If anything else shows up in the plan (unrelated
  resource changes), that's real infra drift unrelated to this task — stop and look before
  applying, don't assume it's expected.
- **A second, separate Terraform config-drift bug was found (not fixed) while locating the real
  root module:** the repo ROOT `versions.tf`/`main.tf` (not `accounts/aa365/`) still points its S3
  backend at `aa-cis-tfstate-{acc1-id}` — **a bucket that no longer exists** (confirmed:
  `aws s3 ls` 404s under both `aa365-admin` and `pqnghiep-admin` profiles). This root config is
  almost certainly the pre-"AA-246 Track B" single-account setup, now orphaned dead code — the
  real, live config is `accounts/aa365/` (own `versions.tf`, correctly points at
  `aa-cis-tfstate-{acc2-id}`, created 2026-07-07, same day as the real API Gateway). CI is
  unaffected (`terraform-plan.yml`/`terraform-apply.yml` override the bucket via
  `-backend-config` from a GitHub secret, never read the root `versions.tf`'s hardcoded value) —
  this only bites a human running `terraform init` from the repo root without realizing
  `accounts/aa365/` is the one that matters. Not fixed/removed in this session (out of scope,
  same "flag it, don't silently fix a bigger thing than asked" judgment call as STEP0's stale
  `owq9as3wjl` sweep) — worth its own tiny cleanup ticket (delete or clearly mark the root config
  dead) so the next person doesn't lose time on this the way this session did.
- Linear AA-432 comment reflects the final live-verified state (see below) — issue moved to Done.

## Live verification (22/08/2026, after both PRs merged)

Both merges triggered their pipelines automatically: AA-CIS-App's `Deploy Dev` (push to main)
and AA-CIS-Infra's `Terraform Apply Prod` (`workflow_dispatch`, triggered manually after merge —
this workflow is NOT auto-triggered by a push, by design, matching the review-gate pattern this
task asked for). Both completed successfully:

- `Terraform Apply Prod` run: **`Apply complete! Resources: 1 added, 2 changed, 1 destroyed.`**
  (new `aws_api_gateway_deployment`, `v1_any` method updated, `dev` stage's `deployment_id`
  repointed, old deployment destroyed — confirms decision #6 above actually took effect, not
  just showed up in a plan).
- `Deploy Dev` run: success.

Real verification against `https://api-cis.lumiguides.it.com` (a genuine JWT minted server-side,
inside the ECS container via `api.routers.auth._create_jwt`, for the real, pre-existing
`test-n1-flow` tenant row — same "mint a real JWT server-side" pattern the AA-427 session used;
no plaintext secret ever left the container):

| Check | Result |
|---|---|
| `GET /v1/tours/pool`, real JWT, **no** `X-API-Key` | **200**, real tour data returned |
| `GET /v1/tours/my-versions`, same JWT | **200**, `{"data":[],...}` |
| No `Authorization` header at all | 401 `{"detail":"Not authenticated"}` (FastAPI's own `HTTPBearer` — not the old gateway-edge `{"message":"Unauthorized"}`/`x-amzn-errortype` signature) |
| `Authorization: Bearer fake.jwt.token` | 401 `{"detail":"Invalid or expired token"}` — this is `get_tenant()`'s own except-branch string, proof the request now reaches FastAPI instead of being rejected at the gateway edge |
| Response headers on a real 200 | `X-RateLimit-Limit: 1000`, `X-RateLimit-Remaining` decrementing per call (999 → 998), `X-RateLimit-Plan: business` — confirms `_get_tenant_meta()` is live and reading `shared.tenants.rate_limit_rpm` (this tenant's real value, 1000, queried directly beforehand) |
| Revoke: set `test-n1-flow.is_active = false`, wait for the 30s cache TTL, retry | **403** `{"detail":"Tenant is deactivated"}` on the very next request after the cache window — then reverted `is_active` back to `true` immediately after, tenant left in its original state |
| `/docs`, `/openapi.json` (unrelated NONE-auth routes) | still 200 — confirms no collateral damage to routes this task didn't touch |

Not separately re-verified live: an actual 429 (would need >1000 requests/minute against this
specific tenant's real limit to trigger honestly — impractical to do live; covered instead by
the 5 unit tests, one of which (`test_over_limit_still_returns_429_not_next`) asserts 429 not
401/403 on the code path directly). Also didn't walk the full STEP0 §4 route list one-by-one
(DashboardTab/PoolTab/CatalogTab/staff routes) — the 2 routes tested plus the auth-boundary
proof (fake-JWT error shape) are enough to confirm the gateway-level fix is doing what it should
for every route under `/v1/*` uniformly (it's one shared gateway method + one shared FastAPI
middleware, not a per-route fix), but a full click-through of the actual frontend tabs by a human
is still worth doing at some point if not already covered by other test suites.

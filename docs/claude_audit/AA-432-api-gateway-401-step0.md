# AA-432 STEP0 — API Gateway 401 investigation (Browse Pool / My Catalog)

Status: **investigate-only, no auth-logic fix applied** (per task scope). One safe doc fix
applied (CLAUDE.md stale API Gateway ID) — see "CLAUDE.md fix" section at the bottom.

Investigated: 22/08/2026. All findings below are from live AWS CLI output and source reads on
`main`, re-confirmed with a live curl reproduction against production at the time of writing.

---

## 1. Real API Gateway ID vs. what CLAUDE.md said

Confirmed via AWS CLI, account `[acc2 id]`, region `us-west-1` (profile `aa365-admin`):

```
$ aws apigateway get-rest-apis --profile aa365-admin --region us-west-1
{
  "items": [{
    "id": "4ylo382khg",
    "name": "aa-cis-dev-api",
    "createdDate": "2026-07-07T17:49:46+07:00",
    ...
  }]
}

$ aws apigateway get-rest-api --rest-api-id owq9as3wjl --profile aa365-admin --region us-west-1
NotFoundException: Invalid API identifier specified [acc2 id]:owq9as3wjl
```

- **`owq9as3wjl` no longer exists** — not stale-but-still-there, actually deleted/gone.
- **The only REST API in this account/region is `4ylo382khg`** (`aa-cis-dev-api`), created
  2026-07-07 — 2 days before the AA-271 account-1 teardown (09/07/2026), almost certainly a
  Terraform recreate around that time that CLAUDE.md was never updated for.
- `apigatewayv2` (HTTP API) has zero APIs in this account/region — ruling out an HTTP-API-vs-
  REST-API confusion.
- This matches `4ylo382khg` observed via Playwright in AA-430's testing exactly — that was not
  a Playwright artifact, it's the real ID.

Stale `owq9as3wjl` was found in **6 places**, not just CLAUDE.md:

| File | What |
|---|---|
| `aa-cis` (root)/.claude/CLAUDE.md:23 | LIVE ENDPOINTS — **fixed** |
| `AA-CIS-App/.claude/CLAUDE.md:8,64` | LIVE STATE (2 lines) — **fixed** |
| `AA-ACP-App/.claude/CLAUDE.md:71` | API CONNECTION — **fixed** |
| `skill/aa-cis-schema.md:1004` | describes the TOKEN authorizer — **not fixed** (out of scope: not CLAUDE.md, task step 5 only authorizes CLAUDE.md edits) |
| `skill/aa-ecosys-repos_SKILL.md:106` | describes gateway/authorizer behavior — **not fixed**, same reason |
| `AA-CIS-App/api/routers/admin_produce.py:15` | a source-code comment about the 29s integration timeout — **not fixed**, same reason (also: not CLAUDE.md, and editing source under an investigate-only task felt wrong even for a comment) |

Recommend a tiny separate follow-up (or fold into whatever ticket fixes the header mismatch) to
sweep the remaining 3 stale references — flagging here so it isn't lost, not doing it now since
it's outside this task's one permitted edit.

---

## 2. Real request path for `/v1/tours/*` (confirmed, not assumed)

```
Browser (tenant portal, e.g. /portal/t1-rewrite)
  → fetch("/api/tenant/v1/tours/pool")                         [same-origin, Next.js on Vercel]
  → frontend/app/api/tenant/[...path]/route.ts (Next.js route handler, runs server-side)
       tenant branch: headers = { Authorization: `Bearer ${cis_tenant_token}` }   ← NO X-API-Key
  → fetch(`${API_URL}/v1/tours/pool`)  where API_URL = https://api-cis.lumiguides.it.com
  → DNS: api-cis.lumiguides.it.com
  → API Gateway custom domain "api-cis.lumiguides.it.com"
       (confirmed via `aws apigateway get-domain-names` + `get-base-path-mappings`)
       → basePath "(none)" → restApiId 4ylo382khg, stage "dev"
  → resource `/v1/{v1_proxy+}`, method ANY
       authorizationType: CUSTOM, authorizerId 22qdq8 ("tenant-key-authorizer", TOKEN type)
       identitySource: method.request.header.X-API-Key
  → *** request has no X-API-Key header → API Gateway rejects HERE, 401, Lambda never runs ***
  → (if it had passed) integration: HTTP_PROXY via VPC_LINK → internal NLB
       (aa-cis-dev-nlb-fa2bbe1ae4bac76d.elb.us-west-1.amazonaws.com) → ECS → FastAPI
```

This confirms and closes the one thing AA-430's notes flagged as "chưa xác nhận 100%" (domain→
stage mapping) — verified directly: `api-cis.lumiguides.it.com` really does route through
`4ylo382khg`/`dev`, the exact API + stage the `tenant-key-authorizer` is attached to. This is
real production traffic, not a side environment.

### Live reproduction (just run, production, right now)

```
$ curl -sS -i https://api-cis.lumiguides.it.com/v1/tours/pool
HTTP/2 401
x-amzn-errortype: UnauthorizedException
{"message":"Unauthorized"}

$ curl -sS -i https://api-cis.lumiguides.it.com/v1/tours/pool -H "Authorization: Bearer fake.jwt.token"
HTTP/2 401
x-amzn-errortype: UnauthorizedException
{"message":"Unauthorized"}                          ← exactly what the real FE proxy sends today

$ curl -sS -i https://api-cis.lumiguides.it.com/v1/tours/pool -H "X-API-Key: bogus-key-000"
HTTP/2 403
x-amzn-errortype: AccessDeniedException
{"Message":"User is not authorized to access this resource with an explicit deny in an
identity-based policy"}                              ← proves the header alone is what gates entry;
                                                         once present, the Lambda IS invoked (and
                                                         denies, as expected for a bogus key)
```

`x-amzn-errortype: UnauthorizedException` is API Gateway's own edge rejection (missing
identitySource header) — structurally different from `AccessDeniedException` (Lambda ran and
returned an explicit Deny policy) and different from any error shape FastAPI would produce. This
is the same signature AA-430 already captured via Playwright with a real tenant JWT; reproduced
here independently with curl, confirming it's not a Playwright config artifact.

ECS is currently running (`desiredCount=1, runningCount=1` — checked live), so this isn't an
"ECS is stopped" false negative either; the request never gets far enough to reach ECS.

---

## 3. Lambda authorizer source — confirmed by reading the code, not guessing

`AA-CIS-App/api/lambda_authorizer/handler.py` (last modified 14/05/2026, **before** the AA-427/
424/431 JWT work started this week):

- Reads `event["authorizationToken"]` — this is what API Gateway populates from whatever header
  `identitySource` points to (`X-API-Key`). There is **no code path that looks at
  `Authorization` or does any JWT decoding** — it SHA256-hashes whatever string it received and
  looks it up in `shared.tenants.api_key_hash`.
- `DATABASE_URL` env var is a real DSN (not the AA-303 placeholder bug) — that specific prior
  root cause is not in play here, this is a genuinely different/new issue.
- Confirms: **the authorizer has zero Bearer/JWT support**, deployed code matches source on
  `main` (no drift between what's live and what's in the repo for this file).

Important structural point often missed: this is a **TOKEN-type** authorizer with a single
`identitySource`. API Gateway does not invoke the Lambda at all when that one header is absent —
it 401s at the edge (confirmed above). So even if the Lambda code were changed to also accept a
JWT, **it would never see the request** unless the header the client sent matches
`method.request.header.X-API-Key` exactly. This matters for the recommendation in §5.

---

## 4. Blast radius — wider than the 2 routes Playwright caught

The gateway resource `/v1/{v1_proxy+}` (method ANY) is a single greedy proxy — **every path
under `/v1/*` shares the exact same authorizer and identitySource requirement**, confirmed via
`get-resources`/`get-method`:

| Gateway resource | authorizationType | Affected? |
|---|---|---|
| `/v1/{v1_proxy+}` (ANY) | **CUSTOM** (`tenant-key-authorizer`, `X-API-Key`) | **YES — everything under `/v1/*`** |
| `/admin/{admin_proxy+}` (ANY) | NONE | No (gateway-level; FastAPI does its own `X-Admin-Secret` check) |
| `/auth/{auth_proxy+}` (ANY) | NONE | No |
| `/content/{content_proxy+}` (ANY) | NONE | No |

And `frontend/app/api/tenant/[...path]/route.ts` — the one Next.js proxy that talks to
`https://api-cis.lumiguides.it.com` for **every** `/v1/*` call from the frontend — has **two
branches, neither sends `X-API-Key`**:

- tenant branch (role ≠ admin/content): `Authorization: Bearer <cis_tenant_token>` only.
- staff branch (role = admin/content): `x-admin-secret` + `x-admin-user-id` only.

So this is not limited to the 2 B2B tenant-portal routes AA-430's Playwright run happened to
exercise. Grepping every frontend caller of `/api/tenant/v1/...`:

**B2B tenant portal** (role = tenant, via `(tenant)/portal/*`):
- `DashboardTab.tsx`, `layout.tsx` → `/v1/tours/pool`, `/v1/tours/my-versions` (counts)
- `PoolTab.tsx` (T1 Browse Pool) → `/v1/tours/pool`, `/v1/tours/pool/{id}/rewrite`,
  `/v1/tours/my-versions`
- `CatalogTab.tsx` (T4 My Catalog) → `/v1/tours/my-versions`, `/v1/tours/versions/{id}`
  (GET+PATCH), `/v1/tours/pool/{id}`, `/v1/quota`

**Internal/staff pages** (role = content, via `(internal)/*` — same proxy, staff branch, also no
`X-API-Key`):
- `(internal)/catalog/page.tsx` → `/v1/tours`, `/v1/tours/{id}/approve`
- `(internal)/upload/page.tsx` → `/v1/pipeline/sources`

All of the above are broken in production today whenever reached through
`api-cis.lumiguides.it.com` — not just Browse Pool/My Catalog. `/admin/atoms/*` and
`/admin/brand-identity` calls (`AtomsTab.tsx`, `BrandTab.tsx`) go through the same proxy file but
hit a gateway resource with `authorizationType: NONE`, so they're unaffected by this specific
bug.

I did not find any other consumer that hits `/v1/*` on this domain with a working `X-API-Key`
today (no successful counter-example found in the frontend to suggest this route ever worked
post-gateway-rebuild) — worth double-checking with Nghiep if any external/API-Playground caller
is meant to still be using raw API keys directly (see `ApiPlayground.tsx` /
`api-playground/endpoints-config.ts`, not traced in depth here since it's a user-supplied-key
flow, different from the BFF proxy).

---

## 5. Recommendation: (a) FE proxy adds `X-API-Key`, not (b) Lambda authorizer adds Bearer branch

**Recommend (a).** Reasoning, grounded in what was actually read this session:

- **(b) is not just a Lambda code change — it's an infra change to the authorizer itself.**
  Because `tenant-key-authorizer` is `TOKEN` type with `identitySource:
  method.request.header.X-API-Key`, API Gateway rejects requests missing that exact header
  *before* invoking Lambda (proven live in §2). A TOKEN authorizer cannot be told "accept either
  X-API-Key or Authorization" — that requires changing the authorizer type to `REQUEST` (which
  can pull multiple headers) via Terraform, redeploying the API, and re-verifying **every** route
  under `/v1/*` (per §4, that's the whole `/v1/*` surface, both tenant and staff traffic) against
  the new authorizer — much larger blast radius on a live production auth boundary than the
  issue description implies.
- **(a) is genuinely low-risk because the gateway authorizer's tenant resolution is provably
  unused downstream.** Read `api/routers/v1_tours.py`: `get_tenant()` uses `HTTPBearer` and
  decodes the `Authorization: Bearer` JWT itself, taking `tenant_id = tenant["sub"]` directly —
  it does **not** consult whatever `tenantId`/`tenantSlug` context the Lambda authorizer would
  have attached. So the gateway's X-API-Key check is, in practice, only a coarse edge gate ("is
  this a legitimate caller"), not the source of truth for per-tenant scoping — that's 100%
  handled by the JWT already, both today and after any fix.
  - This means (a) doesn't need a *per-tenant* API key threaded through the proxy — a single
    static service-level key (one new `shared.tenants` row, e.g. an `is_active` "internal-bff"
    tenant, `api_key_hash` = SHA256 of a secret stored in Vercel env, e.g.
    `CIS_GATEWAY_API_KEY`) satisfies the gateway precondition for all `/v1/*` traffic from the
    BFF, while the real per-tenant authorization keeps flowing through the JWT exactly as it does
    now. One line added to `frontend/app/api/tenant/[...path]/route.ts` (`headers["X-API-Key"] =
    process.env.CIS_GATEWAY_API_KEY`), one DB insert, one Vercel env var — no Lambda redeploy, no
    Terraform change, no re-test of unrelated routes.
- (b) is the architecturally "correct" direction long-term (matches the JWT-first direction
  AA-427/424/431 already signaled), but should be its own deliberate infra-review task, not the
  urgent unblock — the REQUEST-authorizer migration deserves its own testing pass across all of
  `/v1/*`, not something to rush under active-incident pressure.

Not verified this session, worth confirming before implementing (a): whether `shared.tenants`
has any uniqueness/rate-limit assumptions that a synthetic service tenant would need to respect
(`rate_limit_rpm` in particular — a shared BFF key aggregates all tenants' traffic against one
row's rate limit unless that's special-cased), and where `CIS_GATEWAY_API_KEY` should live
(Vercel env — flagging per global CLAUDE.md rule: no underscore env var should be
auto-generated/edited by me; Nghiep sets this one manually in Vercel/VSCode).

---

## CLAUDE.md fix (the one edit this task was allowed to make)

Fixed the stale `owq9as3wjl` → `4ylo382khg` in all 3 CLAUDE.md files that had it, each with a
"corrected 22/08/2026, AA-432" note to prevent re-staling silently:

- `aa-cis/.claude/CLAUDE.md` (root workspace — not a git repo, edited directly, nothing to
  commit)
- `AA-CIS-App/.claude/CLAUDE.md` — committed on branch
  `pqnghiep1354/aa-432-urgentinfra-api-gateway-id-sai-lech-claudemd-vs-production-x`,
  pushed, PR opened: https://github.com/AdventureAsia365-CIS/AA-CIS-App/pull/188
- `AA-ACP-App/.claude/CLAUDE.md` — same branch name, own repo, pushed, PR opened:
  https://github.com/AdventureAsia365-CIS/AA-ACP-App/pull/6 (note: AA-ACP-App is documented as
  abandoned in AA-CIS-App's CLAUDE.md — fixed anyway since the file is still stale and could
  mislead, but this PR is low-priority to merge)

Neither PR touches auth logic — both are the single doc line(s) each, per task scope. **Not
merged** — left for human PR review per repo policy.

No fix applied to the actual header-mismatch bug. Awaiting Claude Chat + Nghiep to pick (a) or
(b) from §5 before any code touches `frontend/app/api/tenant/[...path]/route.ts` or the
authorizer.

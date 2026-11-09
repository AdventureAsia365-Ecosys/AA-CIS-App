# AA-1036 R3 slice 3 — remove tenant API keys (lane r3-key-removal, S267)

Root ADR 0019 d6: "All tenant API-key code and data go too." Slices 1-2 are live (the studio runs inside the agency
web with partner-pool sign-in only; `aa-crm-dev-portal` retired). The 30-day `acp_shared.audit_log` check
(`action = 'tenant.login'`, `details.login_method`) found 694 `api_key` sign-ins, all from synthetic E2E tenants, the
last on 2026-11-03 11:42Z with no `client` (a machine or agent script); the studio's `partner` sign-ins continue. Tech
lead rule: Dev does not wait out windows; switch every caller, then remove.

## Caller inventory (origin/main of every repo, 04/11/2026)

Callers of `POST /auth/tenant-login` and of tenant API keys, and what happened to each.

### AA-CIS-App (this PR)

| Where | What | Outcome |
| --- | --- | --- |
| `api/main.py:395` | live `POST /auth/tenant-login` (key -> 24 h tenant JWT, `tenant.login` audit row) | 410 Gone for any body, no DB read, no audit row |
| `api/routers/auth.py:163` | the unmounted router's `POST /auth/tenant-login` | same 410 (`tenant_login_gone()`) |
| `api/routers/auth.py:267` | `verify_tenant_api_key` (X-API-Key, no route depended on it) | deleted with `_hash_api_key`, the `TenantApiKey` scheme, `TenantLoginRequest/Response`, `AA_INTERNAL_ADMIN_SUB` |
| `api/lambda_authorizer/handler.py:51` | dead Lambda authorizer source (Terraform resource gone since AA-750) | deleted (+ CODEOWNERS line) |
| `api/routers/admin.py:229, 1373` | `POST /admin/tenants/{id}/generate-key` | deleted (+ `staff_authz` POLICY row, snapshots, role tests) |
| `api/routers/admin.py:281, 336` | `POST /admin/tenants` issued and returned a key | no key column written, no `api_key` in the answer |
| `api/routers/admin.py:1491, 1537` | offboard wrote `api_key_hash = 'REVOKED_' || id`, answered `api_key: REVOKED` | sets `account_status = 'offboarding'` (CIS 354 trigger keeps `is_active` / `cancelled_at`), no key |
| `api/routers/admin.py:1623, 1645` | restore issued a new key | sets `account_status = 'active'`, no key |
| `services/acp_shared/e2e_tenant_key_sync.py:28` | copied a re-issued key into `aa-cis/dev/e2e-tenant/<slug>` | deleted (+ its unit test) |
| `api/services/tenant_account.py:304`, `api/routers/v1_account.py:93`, `api/schemas/public_api.py:142` | `GET /v1/account/api-key` (masked key facts, `REVOKED_` read) | deleted; activity label `tenant.login` reads "Signed in" |
| `services/tenant_account/provision.py:217, 335` | saga inserted `api_key_hash NULL`, result `api_key: not_issued` | column and field left out |
| `frontend/app/admin/tenants/page.tsx:1151, 1306, 249` | admin Tenants: Key (rotate) button, one-time key dialog after create / restore | removed; create / restore end in a small result dialog |
| `tests/e2e/aa524…aa576*.spec.ts` (15 files), `tests/e2e/cis.spec.ts:176` | old live-verify specs that signed in with `WANDERLUX_API_KEY` / generate-key on `/tenant-login` (project `chromium`, never run in CI) | deleted; the `cis.spec.ts` tenant-login suite replaced by a note |
| `tests/load/k6_*.js` | send an `X-API-Key` header nothing reads | left (load scripts, not CI; the header is ignored) |
| `api/core/login_throttle.py`, `api/core/staff_secret.py`, `api/middleware/tenant_role.py`, `services/acp_shared/hitl_events.py` | docstrings naming the old flow | updated (history only) |

### AA-Partner (separate PR, ships first)

| Where | What | Outcome |
| --- | --- | --- |
| `web/app/(studio)/portal/_components/ApiTab.tsx:17, 92` | studio API page told agencies to `POST /auth/tenant-login` with their key | page, playground and `app/api/playground/proxy` deleted; `/portal/api`, `/portal/account/api` 308 to settings |
| `web/app/(studio)/portal/_components/ApiKeyCard.tsx:26` | Integrations card calling `GET /v1/account/api-key` | deleted |
| `web/app/(studio)/portal/_components/PlaceholderTabs.tsx:333` | settings "API access" card | deleted |
| `e2e/smoke-portal/auth.setup.ts:67` | local studio sign-in with `E2E_TENANT_API_KEY` on `/tenant-login` | removed: partner sign-in only |
| `e2e/phase2/phase2.spec.ts:186` | optional "E check" with `E2EP2TENANTKEY` | removed |
| `web/proxy.ts:20`, `web/lib/hosts.ts:133`, `.github/scripts/edge_verify.sh`, `ci.yml:554` | `/tenant-login*` 308 to the partner sign-in | kept (a redirect, not a key) |

### AA-CIS-Infra (separate PR)

| Where | What | Outcome |
| --- | --- | --- |
| `modules/ecs/main.tf:317-327` | CIS task role DescribeSecret + PutSecretValue on `aa-cis/dev/e2e-tenant/*` | removed (plan: 1 in-place policy update) |
| `modules/api_gateway/main.tf:140` | `/auth/tenant-login` own resource + tight throttle (AA-876) | kept on purpose: forwards to the 410 |
| Lambda authorizer | removed by AA-750 | nothing left in Terraform |
| `db/migrations/cis/*` | copies of CIS migrations | the CIS 368 copy is the bot's job |

### Root, AA-Booking, AA-TripPlanner-Web

- Root `docs/security/secrets-register.{yaml,md}`: `aa-cis/dev/e2e-tenant/{atlas-hearth,terra-family,trail-pulse,
  wildkind-travel}` -> orphan-delete (root PR). GitHub `E2E_TENANT_API_KEY` (AA-Partner) was already orphan-delete
  (S256). `skill/aa-ecosys-repos/SKILL.md:20, 85` and ADR 0019 d6 updated. `tools/uat/lib/dryrun-page.cjs:70` only
  treats `/tenant-login` as a sign-in path (harmless).
- AA-Booking, AA-TripPlanner-Web: no caller (grep `tenant-login`, `api_key_hash`, `e2e-tenant`, `X-API-Key` for a
  tenant: none; TripPlanner's `X-TripPlanner-Key` and Booking's M2M are unrelated). `aa-m2m` client credentials
  untouched.
- The live `api_key` sign-ins of the last 30 days came from agent-run scripts (`.tmp-session` live proofs, T2 reruns)
  reading the E2E key copies; no repo code calls the endpoint on a schedule. They stop with the 410.

## Decisions

- **410 Gone, not 404**, on both handlers, for ANY body (the handler takes no body, so even malformed JSON gets the
  410, never a 422). Body: `{"detail": "tenant_api_key_removed", "message": "...partner account...", "sign_in":
  "partner"}`. No throttle, no DB read, no audit row: there is nothing to protect any more.
- **`_create_jwt` stays as a test helper** (the legacy token shape without `amr` / `csr`). Many token-gate tests use it;
  the guard test keeps every production call out. Tokens minted before the deploy live at most 24 h; no extra
  revocation (Dev, synthetic tenants only).
- **Offboarding uses `account_status`.** The offboard UPDATE sets `account_status = 'offboarding'` explicitly next to
  the legacy columns; restore sets `'active'`. The CIS 354 trigger keeps `is_active` / `cancelled_at` consistent, and
  the partner session exchange already refuses an inactive or cancelled tenant (`services/tenant_membership/session.py`).
- **Data in two steps.** CIS 368 (contract, MIG-4, after this code PR is deployed) sets the column NULL with a
  before/after check that no account state changed. The DROP is CIS 369 (`-- migrate: destructive`) on its own branch
  with a read-only DRY RUN script (`scripts/aa1036/key_hash_dry_run.py`). DRY RUN on Dev 04/11 (S268): `verdict
  ready=true`; the tech lead approved the apply the same day (see "CIS 369" below).
- **No CHECK (api_key_hash IS NULL) in 368**: 13 integration-test fixtures still inserted the column; the guard test
  covers the code, and the DROP removes the column anyway. The 369 PR cleaned those fixtures (see "CIS 369" below).
- **openapi.json** regenerated with the pinned fastapi 0.161.1 / pydantic 2.14.1 (scratch venv): removing
  `/v1/account/api-key` and the tenant-login body is a breaking change on public paths, so the PR needs the
  `api-breaking` label for the API contract check.

## Changed

See the inventory. Tests: new `tests/unit/test_aa1036_no_tenant_api_keys.py` (410 on both handlers for 8 bodies,
no DB touch; no key route, scheme or answer field; an AST source guard over `api/`, `services/`, `shared/`,
`worker/` that refuses the retired names, string literals `api_key_hash` / `generate-key` / `e2e-tenant` /
`X-API-Key` / `/v1/account/api-key` and any production call of `_create_jwt`, with self-tests); updated unit tests
(aa797, aa761, aa760, aa876, aa559, aa1028, role matrices, snapshots) and integration tests (aa797, aa741 offboard,
aa761, aa1028 provision); deleted `test_auth_tenant_api_key.py`, `test_e2e_tenant_key_sync.py`.

## Tradeoffs

- Keeping the API Gateway `/auth/tenant-login` resource (Infra) costs nothing and keeps old callers on a clear 410.
- Deleting the 15 obsolete live-verify specs instead of porting them: each verified one past issue on a page and a
  sign-in that no longer exist; the studio smoke (AA-Partner) covers the same pages with the partner sign-in.

## Should know

- Order: Partner PR (studio stops calling `/v1/account/api-key`) -> this CIS PR (deploy) -> Infra PR (grant) ->
  CIS 368 migration-only PR (contract, applied by Migration Apply) -> root PR -> delete the four secrets.
- CIS 368 needs 366 and 367 (other S267 lanes) merged first, or it must be renumbered to main max + 1.
- After the deploy: `curl -X POST .../auth/tenant-login` -> 410 with the body above; `audit_log` shows no
  `login_method = 'api_key'` row after the deploy time; Tenants page has no Key button.

## CIS 369 — DROP COLUMN (lane aa1036-369, S268)

The 369 file was prepared in S267 on `feat/aa-1036-r3-key-hash-drop-369`, a branch on an old main that also carried the
pre-squash 368 commit. This PR starts again from main and brings over only `api/migrations/369_tenant_api_key_hash_drop.sql`
(byte-identical, header and `-- migrate: destructive` kept), plus the DRY RUN script and the test clean-up.

### Decisions

- **Destructive, not contract.** `-- migrate: destructive` stays: DB Migrate takes the pre-apply RDS snapshot
  (`db-migrate-369-<run>-<attempt>`) and Migration Apply never applies it on its own. No readiness raise
  (`REQUIRED_CIS_SCHEMA` unchanged): no code needs the column gone, and the guard test keeps code from naming it.
- **DRY RUN script committed verbatim** at `scripts/aa1036/key_hash_dry_run.py` (the path the 369 header names), so
  the evidence of the Linear comment can be re-run. It reads the catalog and one count, as `cis_app`.
- **Fixtures that insert or read the column**: the column is removed from the INSERT / UPDATE / SELECT (conftest seed,
  aa741 x2, aa761, aa764, aa840, aa978 276, aa1028 356 / 361 / 353-355 / provision). The offboard / restore UPDATEs in
  the 276 and 353-355 tests now mirror `api/routers/admin.py` (no key column). `test_007_rls_isolation.py` (excluded
  from CI, runs against a live DB) swaps its two "tenant has a SHA-256 key hash" tests for one "the column is gone".
  The stray tracked `tests/integration/conftest.py.bak` is deleted.
- **Migration tests that re-run an older file whose body names the column** get a stand-in column, not a skip
  (helpers in `tests/integration/_rls_fixture_helpers.py`): an applied file is never edited, and a skip would drop the
  re-run coverage of 354 / 355 / 368.
  - `KEY_HASH_STANDIN_SQL` (`ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(255)`, the 003 type) inside the test's own
    rolled-back transaction: the 353-355 re-run and grants tests (354 / 355 guard 3 probe INSERT names the column).
  - `key_hash_standin(cur)` around a file run with its own BEGIN / COMMIT (autocommit): the 320/321 teardown that
    re-applies 355, and the 368 module (seeds hashes, re-runs 368). It adds the column only when absent and drops it
    again after, so the shared test DB returns to the replay state.
  - Found beyond the list in the 369 header: `test_aa1017_migration_320_321.py` re-applies 355 in its teardown.
- **New `test_aa1036_migration_369.py`**, each test in a rolled-back transaction: the replay dropped the column and
  registered 369; with a stand-in column (368 registered, value NULL) the file drops it and registers 369; a value
  written after 368 stops it; a missing 368 stops it; a view on the column stops the DROP (RESTRICT) instead of
  cascading; on a DB without the column it only registers, twice in a row. The 368 module also checks 368 is a no-op
  without the column.

### Tradeoffs

- A stand-in column vs a skip when the column is absent: the stand-in keeps the re-run tests meaningful; its cost is a
  short ACCESS EXCLUSIVE lock on `shared.tenants` in the test DB, which tests run serially anyway.

### Should know

- Order after merge: the Infra copy of 369 into `AA-CIS-Infra/db/migrations/cis/` (bot Migration Sync opens it; a
  destructive copy does not auto-merge, merge it by hand), then DB Migrate `check` + `apply` of `cis/369_...` (snapshot
  first, the workflow takes it), then `/health/ready` shows 369, then the snapshot id goes into the root
  `docs/architecture/dead-table-registry.md`.
- `scripts/aa729/common.py` still lists `api_key_hash` in a PII scrub list (harmless; left as is).
- After 369 the DB Schema Dump bot PR drops the column from `db-schema-reference.md`.

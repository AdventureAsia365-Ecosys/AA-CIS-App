# AA-433 — Rotate wanderlux-travel API key (plaintext exposed in git)

Urgent security fix, independent of AA-432's architecture decision. 22/08/2026.

## Decisions

- **Rotated via the real `generate_api_key()` endpoint**, not a hand-rolled DB update — called
  `POST /admin/tenants/{tenant_id}/generate-key` on production
  (`https://api-cis.lumiguides.it.com`) with the real `X-Admin-Secret` (from Secrets Manager
  `aa-cis/dev/admin-secret`), exactly as the task required. This endpoint atomically generates
  the new plaintext, hashes it, and persists the hash — no separate "step 4" DB update was
  needed, generating the key *is* the persist step.
- **Did not rotate `exploreasia-co`** (the other migration-007 seed tenant) — confirmed
  `is_active=false` live, both before and unaffected by this change. Task scope was "rotate
  active tenants sharing this exposure"; an inactive tenant's key already can't authenticate
  (`WHERE api_key_hash = $1 AND is_active = true` in both `auth.py` and the Lambda authorizer),
  so there's no live credential to revoke here regardless of the plaintext being exposed.
- **Migration 007: replaced the plaintext with inert placeholders, not just a warning
  comment.** The task's own guidance was to check whether this migration could ever run again
  before deciding. It's designed to be idempotent (`ON CONFLICT (tenant_id) DO NOTHING`,
  matching the general "migrations self-register into `shared.schema_versions`, re-runs are
  safe no-ops" convention) — but idempotent-on-existing-rows doesn't mean *dead*: it's the
  literal script a from-scratch DB rebuild (disaster recovery, a fresh environment) would
  replay from row zero, at which point `ON CONFLICT DO NOTHING` doesn't block anything because
  there's nothing to conflict with yet. A comment-only fix would leave a rebuild free to
  silently reseed the exact leaked credential. Swapped both `encode(sha256('<real
  key>'::bytea), 'hex')` calls for `encode(sha256('ROTATED-SEE-AA433-DO-NOT-USE-<slug>'::bytea),
  'hex')` — syntactically identical, runs fine, just hashes something that matches no real
  key.
- **Also fixed 3 more exposure points found via the required grep sweep, not just the 1
  migration file the task named:**
  1. `AA-CIS-Infra/modules/rds/migrations/007_seed_test_tenants.sql` — a second copy of this
     exact migration in a **different repo**. Confirmed via grep that nothing in that repo's
     Terraform (`*.tf`) references `modules/rds/migrations/` at all — this copy is static,
     never applied by any automation (it was a one-time snapshot committed 21/04/2026 alongside
     migration 008, already stale — stops at 008 while the real repo is past 100). Fixed with
     the identical placeholder swap for consistency, even though it's dead weight either way.
  2. `frontend/app/(tenant)/portal/page.tsx.bak` — a committed backup of the pre-AA-430
     `page.tsx`, hardcoding `"<old seed key, already rejected>"` as a fallback default in an
     `ApiKeyTab` component. Confirmed unreferenced anywhere (`.bak` extension, no imports found,
     not part of the Next.js build) — genuinely dead code, not just dead-with-respect-to-this-
     bug. Deleted outright rather than editing, since it serves no purpose.
  3. `tests/integration/test_007_rls_isolation.py` — two tests (`test_tenant_a_api_key_hash`,
     `test_tenant_b_api_key_hash`) hardcoded the same plaintext keys and asserted the live DB's
     `api_key_hash` matched `sha256(<that exact plaintext>)`. Two problems, not one: (a) same
     cleartext-secret-in-git issue, and (b) **this assertion was already stale before AA-433
     touched anything** — see "Should know" below. Rewrote both to assert "a real 64-char SHA256
     hex digest is present" instead of pinning to one historical secret value, which is what
     actually stays true across any future rotation.

## Changed

- `api/migrations/007_seed_test_tenants.sql` — comment block + 2 literal key strings
  (placeholder swap, see Decisions).
- `AA-CIS-Infra/modules/rds/migrations/007_seed_test_tenants.sql` — identical fix, separate
  repo/commit (that repo has its own git workflow; can't be committed in the same PR).
- `tests/integration/test_007_rls_isolation.py` — `test_tenant_a_api_key_hash` /
  `test_tenant_b_api_key_hash` rewritten (see Decisions).
- Deleted `frontend/app/(tenant)/portal/page.tsx.bak`.
- **Not a file change, a live action:** `wanderlux-travel`'s `shared.tenants.api_key_hash` was
  updated in the production DB via `generate_api_key()`. New plaintext key was **not** committed
  anywhere — written once to a local, non-git scratchpad file for Nghiep to retrieve and hand to
  the real tenant, then delete. See "Should know" for the exact path.

## Tradeoffs

None significant — this is a narrowly-scoped credential rotation plus cleanup of every place the
same leaked string appeared. The one judgment call (placeholder vs. comment-only for migration
007) is covered in Decisions above with the reasoning for going further than the task's minimum
ask.

## Should know

**The old key was already dead before this task started — important context, not just a
technicality.** Baseline-tested the migration-007 plaintext key against production *before*
touching anything (see Verify below): it was already rejected, at both the backend
(`/auth/tenant-login` → `401 Invalid API key`) and the API Gateway Lambda authorizer layer
(`X-API-Key` header → `403 AccessDeniedException`, i.e. Lambda ran and explicitly denied). Cross-
checked against `shared.tenants.updated_at` for this row: `2026-05-05`, two weeks after the
migration's `2026-04-20` seed — someone rotated this key for real back in May, independent of
this task. **This changes the urgency framing, not the correctness of doing the work**: the
specific leaked string was not a live credential the moment this task started, but (a) the
exposure in git history was real and permanent regardless, (b) nobody had documented that the May
rotation happened or updated the migration file to match, so the next person reading migration
007 would reasonably have believed that key was still live, and (c) a from-scratch rebuild would
have resurrected it (see Decisions) — so rotating again + closing the file-level exposure was
still the right call, just not a "credential is actively being used maliciously right now"
emergency. Flagging this plainly rather than letting the "🚨 urgent, live exposure" framing stand
uncorrected.

**Where the new key is:** written to
`/tmp/claude-1000/-home-nghiep-projects-aa-cis/4157ee63-b11e-4055-9fad-c98fbfea963e/scratchpad/
AA-433-NEW-WANDERLUX-KEY-store-then-delete.txt` (session scratchpad, `chmod 600`, never
committed, never pasted into chat). Nghiep: retrieve it, get it to WanderLux Travel's real
contact (or your own secrets store, however you currently distribute tenant keys — I didn't find
an existing "resend key" flow in the codebase, worth a look if this comes up again), then delete
the file.

**`generate_api_key()` has no independent write-audit trail beyond `updated_at`.** Unlike
`create_tenant()` (which writes to `acp_shared.audit_log`), `generate_api_key()` doesn't log an
audit row — worth a small follow-up if key-rotation auditability matters, not fixed here (out of
scope, not security-critical for this task).

## Verify

**Baseline (before rotation)** — confirms the old key's actual state going in:
```
POST /auth/tenant-login {"api_key":"<old seed key, already rejected>"}
→ 401 {"detail":"Invalid API key"}

X-API-Key: <old seed key, already rejected>  →  GET /v1/tours/pool
→ 403 x-amzn-errortype: AccessDeniedException
  {"Message":"User is not authorized to access this resource with an explicit deny in an
  identity-based policy"}
```
(Both already-rejected pre-rotation — see "Should know".)

**Rotation call** (production, real endpoint):
```
POST /admin/tenants/a1b2c3d4-0001-4000-8000-000000000001/generate-key
X-Admin-Secret: <from Secrets Manager aa-cis/dev/admin-secret>
→ 200 {"tenant_id": "a1b2c3d4-...-001", "tenant_name": "WanderLux Travel",
       "api_key": "cis_<redacted, 47 chars>", "message": "Store this API key securely..."}
```

**Post-rotation — old key, re-tested (still/again rejected, as expected):**
```
POST /auth/tenant-login {"api_key":"<old seed key, already rejected>"}
→ 401 {"detail":"Invalid API key"}
```

**Post-rotation — new key works:**
```
POST /auth/tenant-login {"api_key":"<new key>"}
→ 200 {"tenant_id":"a1b2c3d4-0001-4000-8000-000000000001","tenant_name":"WanderLux Travel",
       "plan_tier":"growth","token":"<JWT, present>"}

X-API-Key: <new key>  →  GET /v1/tours/pool
→ 401 {"detail":"Not authenticated"}   (x-amzn-remapped-server: uvicorn — NOT x-amzn-errortype;
  this response came from FastAPI itself, not the gateway edge — meaning the Lambda authorizer
  ALLOWED the new key and the request reached the backend, which then correctly asked for the
  separate Authorization: Bearer JWT that this route requires (AA-432 territory, unrelated to
  key validity). Confirms the new key passes the gateway/Lambda layer cleanly — contrast with
  the old key's 403 AccessDeniedException above, which came from the gateway/Lambda layer
  itself denying.)
```

**Grep sweep** (after all fixes): `grep -rn "<old seed key, already rejected>\|
<second old seed key, already rejected>" ~/projects/aa-cis/` → zero hits across both repos.

**Live tenant state, confirmed structurally (no hash values logged)**, before and after:

| | is_active | hash changed? |
|---|---|---|
| wanderlux-travel | true | yes — new random hash from `generate_api_key()` |
| exploreasia-co | false | no — not rotated (inactive, out of scope) |

## After this

- Committed on `pqnghiep1354/aa-433-urgentsecurity-xoay-api-key-wanderlux-travel-plaintext-key`
  in both `AA-CIS-App` and `AA-CIS-Infra`, pushed, PRs opened against `main` in each — not
  merged (human review per repo policy).
- Linear AA-433 → move to **In Review**.

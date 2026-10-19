# AA-506 STEP0 — JWT_SECRET hardcoded fallback investigation

## 1. Exact location of the hardcoded fallback

`api/routers/auth.py:27`:

```python
JWT_SECRET   = os.environ.get("JWT_SECRET", "cis-dev-jwt-secret-change-in-prod")
```

Module-level constant, evaluated once at import time (not lazily per-request).

## 2. Where it's used — sign + verify

- `_create_jwt()` (line 61) — signs **tenant** JWTs (`/auth/tenant-login`, deprecated but still
  reachable for any non-ACP caller — see docstring at line 82).
- `verify_jwt()` (line 67) — verifies any JWT (tenant or admin) presented to
  `/auth/verify-tenant` and anywhere else that imports `verify_jwt` from this module.
- `_create_admin_jwt()` (line 283) — signs **admin** JWTs (`POST /auth/admin-login`, live route
  declared in `api/main.py`, re-using this module's helpers per the comment at line 235-244).

So the same one constant backs both the tenant-portal auth flow and the admin-panel auth flow —
one fallback value compromises both.

## 3. How many read sites for the env var

Exactly **one** `os.environ.get("JWT_SECRET", ...)` call (line 27). `JWT_SECRET` is then reused
as a plain module-level name at lines 61, 67, 283 — not re-read from the environment each time.
Confirmed via `grep -rn "JWT_SECRET" api/` — no other file reads this env var; no duplicate
fallback string exists elsewhere.

## 4. Severity — why this is real, not theoretical

The fallback string `"cis-dev-jwt-secret-change-in-prod"` is committed in this repo's own
history (git-trackable, and now also quoted in this very audit doc and in AA-501's memory entry
— see `docs/claude_audit/../../.claude/... memory` "AA-501 shipped + JWT_SECRET security gap").
Anyone who has ever cloned this repo, or read AA-501's session notes, can forge a valid tenant OR
admin JWT for `aa-cis-dev-api` **iff** the deployed task definition does not inject a real
`JWT_SECRET` — confirmed below that it currently does not.

## 5. ADMIN_SECRET pattern (the one to mirror — confirmed, not assumed)

`api/routers/admin.py`:
```python
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")   # empty default, NOT a real secret
...
if not ADMIN_SECRET:
    raise HTTPException(...)   # fails the specific request closed, doesn't crash startup
if x_admin_secret != ADMIN_SECRET:
    raise HTTPException(...)
```
Same shape in `admin_settings.py`, `v1_tours.py`, and the `verify_tenant_api_key()` dependency in
`auth.py` itself (line 195) — all default `ADMIN_SECRET` to `""`, never a guessable real string.
This is already "fail closed on a per-request basis", not "fail fast at startup" — AA-506's own
ask (fail-fast at startup) is a **stricter** bar than the existing ADMIN_SECRET convention, by
explicit instruction from Nghiệp for this ticket specifically. Not proposing to retrofit
ADMIN_SECRET's own fallback in this task — out of scope, flagged as a separate observation only.

### Terraform wiring for ADMIN_SECRET (the pattern to copy for JWT_SECRET)

- Secret created **manually** (`aa365/variables.tf:111`: `"... (aa365 — create manually first)"`)
  — Terraform in this account does not own `aws_secretsmanager_secret` for these three (openai/
  admin/anthropic); it only receives their ARNs as tfvars and wires them into the ECS task
  definition's `secrets` block. Same precedent as the OpenAI key rotation (S153).
- `accounts/aa365/terraform.tfvars:38`: `secret_admin_arn` = the ARN of secret `aa-cis/dev/admin-secret` — naming convention `aa-cis/dev/<name>`.
- `accounts/aa365/main.tf:146`: passed straight through into the `ecs` module call as
  `secret_admin_arn = var.secret_admin_arn`.
- `modules/ecs/main.tf:210`: `{ name = "ADMIN_SECRET", valueFrom = var.secret_admin_arn }` inside
  the container definition's `secrets` list — a real Secrets Manager reference, injected by ECS at
  container start, never plaintext in the task definition or Terraform state.
- Execution role already covers any `aa-cis/dev/*` secret: `accounts/aa365/main.tf:156` sets
  `execution_role_secrets_arn_pattern` = every Secrets Manager secret named `aa-cis/dev/*` in the account → `modules/ecs/main.tf`'s `aws_iam_role_policy.ecs_execution_secrets` grants
  `secretsmanager:GetSecretValue` on that whole prefix. **A new secret named
  `aa-cis/dev/jwt-secret` needs no new IAM policy** — it's already covered.

Plan: create `aa-cis/dev/jwt-secret` the same manual way, add `secret_jwt_arn` variable at both
`modules/ecs/variables.tf` and `accounts/aa365/variables.tf`, wire it through `main.tf` exactly
like `secret_admin_arn`, add the tfvars line, add the `secrets` list entry
`{ name = "JWT_SECRET", valueFrom = var.secret_jwt_arn }`.

## 6. Live task definition confirmation (aa-cis-dev-api:172)

`aws ecs describe-task-definition --task-definition aa-cis-dev-api:172` → container `api`
`secrets` list, confirmed real (live AWS call, not assumed from Terraform source):

```json
[
  {"name": "DATABASE_URL",       "valueFrom": ".../secret:aa-cis/dev/rds-XXXXXX"},
  {"name": "OPENAI_API_KEY",     "valueFrom": ".../secret:aa-cis/dev/openai-key-XXXXXX"},
  {"name": "ADMIN_SECRET",       "valueFrom": ".../secret:aa-cis/dev/admin-secret-XXXXXX"},
  {"name": "ANTHROPIC_API_KEY",  "valueFrom": ".../secret:aa-cis/dev/anthropic-key-XXXXXX"},
  {"name": "DATAFORSEO_LOGIN",   "valueFrom": ".../secret:aa-cis/dev/dataforseo-XXXXXX:login::"},
  {"name": "DATAFORSEO_PASSWORD","valueFrom": ".../secret:aa-cis/dev/dataforseo-XXXXXX:password::"}
]
```

**No `JWT_SECRET` entry.** Confirmed the gap: the live app is running on the hardcoded fallback
string right now. `aws ecs describe-services` confirms `:172` is the actual deployed+running
task definition (desiredCount=1, runningCount=1, status=ACTIVE) — not a stale/superseded revision.

## 7. Plan (per Nghiệp's build spec, unchanged from the prompt)

1. Create `aa-cis/dev/jwt-secret` in Secrets Manager, random ≥32-byte value, never written to
   code/tfvars/state in plaintext.
2. Terraform: new `secret_jwt_arn` var (ecs module + aa365 account) → task def `secrets` entry,
   mirroring `secret_admin_arn` exactly.
3. `auth.py`: remove the hardcoded fallback string entirely; if `JWT_SECRET` is unset at import
   time, raise immediately (fail-fast at startup, not first-request).
4. Deploy order: `terraform apply` first (secret exists + injected, old code still tolerates it
   fine since it just reads a real env var now) → force new deployment of the new code (which now
   requires it) → confirm no rollback to a stale task (secret-fetch failure would otherwise loop
   the service on the old revision).
5. Live-verify all 4 items Nghiệp listed before reporting done.

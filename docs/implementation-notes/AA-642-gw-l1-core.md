# AA-642 — gateway completion, lane L1 (CIS core): D1, A1, A9, A8

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` (sections 1.A, 1.D, 2, corrections in 4).
Lane order: D1 (migration 325) → A1 (migration 326) → A9 → A8. Migration numbers 325 and 326 are this lane's.

## D1 — prompt_version seed + CI gate (gap OPS-03)

### Decisions
- Migration 325 inserts the 32 manifest rows exactly as `scripts/prompt_registry_check.py --sql` prints them, with
  `created_by = 'migration-325'` (the `<migration>-NNN` label other seeds use), and a guard that every row exists with
  this exact hash and path. Without the guard, `ON CONFLICT DO NOTHING` would keep a row registered earlier with
  another hash and that stage would silently keep `prompt_version` NULL.
- Class **contract**, not expand: ADR 0017 d3 makes any INSERT into an existing table contract (config rows
  included). The lane brief said "expand header"; the ADR wins. MIG-4: migration-only PR, Infra copy (bot, merged by
  hand for a contract file), manual DB Migrate apply.
- The CI gate is static: `registration_problems()` reads the `INSERT INTO shared.prompt_version` tuples of every file
  in `api/migrations/` and fails for a manifest entry (stage, version, sha256) none of them inserts. It runs in the
  existing gate test and in the script's default mode. No database in CI needed.
- `--sql --unregistered --created-by migration-NNN` prints only the rows still missing, the body of the next bump
  migration.

### Changed
- Nothing at run time: `prompt_registry.version_for` already writes the version once a row exists.

### Tradeoffs
- The tuple regex reads text, not SQL. A later migration that DELETEs a row is not seen as unregistering it. Accepted:
  rows are never deleted (versions are immutable) and a rollback is a deliberate forward file.

### Should know
- The test of the migration (`tests/integration/test_aa642_migration_325.py`) and the gate test ship in the same PR
  as the file (lesson S258: a migration PR carries its own tests and imports no later code).
- After apply, a new ledger row of a manifest stage has `prompt_version = 'v1'` within 10 minutes (registry cache).

## A1 — stage kill switch + declared degrade mode (gaps CIS-2, CIS-4, OPS-11 stage part)

### Decisions
- **The kill switch is `llm_role_config.is_active`**, not a new column. TripPlanner already reads `is_active = false`
  as "AI paused" (its fail-closed route read, TP #198), while the CIS gateway fell back to its built-in SAFE_DEFAULTS
  route for an inactive row, i.e. ran the stage anyway (gap CIS-2). One meaning for both apps, no new grant (cis_app
  already updates `is_active`), and the TP contract view v1 already carries it, so switching `tp_compose` off in the
  CIS admin also pauses TripPlanner.
- Migration 326 stops on its first apply when any row is inactive under the old meaning (it would silently stop its
  stage after the code PR); a re-run after an admin used the switch passes (schema_versions has 326).
- `degrade_mode` (closed CHECK: `queue_retry`, `basic_narration`, `last_saved_text`, `manual_reply_needed`, from tech
  lead answer T-21) is migration-only: a product decision, not an admin toggle. `disabled_reason` is the admin's
  text (cis_app column UPDATE).
- Declared in 326: `t2_generate`, `t9_write` = `queue_retry`, `tp_compose` = `basic_narration` (the three the plan's
  Done-when names). The other T-series writer stages (`t2_flag_fix`, `t2_itinerary_nudge`, `t8_angle_gen`, `t9_repair`)
  have the same aws_only + Anthropic constraint and will show as breaches until a later migration declares theirs:
  left out on purpose, the plan lists only these three (follow-up).
- New ledger class `disabled` (error_class CHECK in 326) instead of reusing `other`: a switched-off stage must be
  countable apart from outages. The CHECK is replaced NOT VALID + VALIDATE (writers keep writing).
- Class: contract (constraints, column ACL, UPDATE, hot ledger table). MIG-4: 326 applied before the A1 code PR,
  because the code writes `error_class = 'disabled'`.

## A9 — ledger v2 for embeddings (gap OPS-02)

### Decisions
- `embed()` mirrors `LLMClient._attempt`: each provider attempt is timed and numbered; a success carries
  `connection_key`, `latency_ms`, `attempt_no`, `deployment_wire_id`, `pricing_mode` on `EmbedResponse`, so the two
  callers (`content_embedding.compute_embeddings`, `places.llm.embed_texts`) need no change: they already pass
  `**billed_fields(resp)`.
- A failed attempt writes the gateway's own 0 USD row (role `embed`; status / class from `client.attempt_outcome`, so
  an account refusal is `refused/access_denied`, an outage `error/other`, an open circuit `refused`). A route model
  not tried at all (not in the catalog, disabled, no usable deployment) writes a `refused` row with latency 0 and no
  attempt number, like the chat gateway's net row.
- `pricing_mode`: `catalog` for a catalog row, `code_table` when the built-in fallback row priced it (catalog
  unreadable).

### Changed
- Before A9 an embed failure left no ledger trace at all (lesson S239: a broken embed profile was silent).

### Should know
- The success row is still the caller's (one row per success); the gateway never writes it.
- Live check needs no paid call: switch the acc2 `cohere-embed-v4` deployment off in Providers, trigger one embed
  (any f10 / a3 embed job), then read the refused / error rows; switch it back on.

## A8 — tenant budget fails closed when the counter cannot be read (gap CIS-3)

### Decisions
- Fail closed only where something is known: the tenant is in the budgeted-tenant set (that read worked) and its
  spend read failed less than `FAILURE_TTL_SECONDS` ago. Then a **T-stage** call raises `TenantBudgetUnavailable`
  (message prefix `tenant_ai_budget_unreadable`); platform A-stages keep failing open (a tenant budget never stops
  them). If the last good read said the tenant's `hard_stop` is off (alert-only) or it has no amount, fail open: an
  alert-only budget would never have stopped the call anyway.
- `TenantBudgetUnavailable` is a plain RuntimeError, not a BudgetExceeded: a counter outage is transient, so the job
  retries with its normal backoff instead of ending `stopped_budget` and waiting for staff approval.
- Ledger: the gateway writes its 0 USD row before any attempt, `refused / budget` (`client.attempt_outcome` now treats
  `TenantBudgetUnavailable` like a budget refusal).
- "hard_stop on by default" (plan A8) done in code, not by migration: the PUT body default (`TenantBudgetBody`) and
  the admin form's empty draft are now `true`. The table has no rows and every write goes through that PUT, so the
  column default would change nothing (and would be a contract migration).

### Changed
- `test_an_unreadable_state_fails_open` became `..._fails_closed_for_t_stages_only` (the old behaviour is the gap).

### Tradeoffs
- When even the tenant set was never readable (no database at all), the check still fails open: nothing says the
  tenant has a budget, and the T-stage call itself needs the same database, so it fails on its own.

### Should know
- No Dev budget row is seeded (brief: do not). The live proof of the fail-closed path is unit-level; the refused row
  of an approval stop is already live-tested by AA-1016.
- `docs/api/openapi.json` regenerated with fastapi 0.161.1 / pydantic 2.14.1 (requirements.txt pins): one line
  (`hard_stop` default).

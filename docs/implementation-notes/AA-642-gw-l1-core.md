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

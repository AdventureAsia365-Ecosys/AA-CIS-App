# AA-1096 lane l5-settings-env (S281) — retire the one-release env overrides (CIS)

Last Done-when of AA-1096: "No env var remains for a migrated setting after one release" (tech lead S279: env vars stay
one release as an emergency override, then go). The release has passed: the S279 settings shipped, S280 deployed again,
and the live task definitions no longer set any settings env var (AA-CIS-Infra lane L6, Infra #832).

## Decisions

- **One PR, migration first in it (CIS 428, contract).** The reader and the admin API read the definitions from
  `shared/settings/compiled_defaults.json`, generated from the migration blocks (seed-parity test), never from the
  table's `env_override` column. So the code change IS the regenerated file: every test that drove a setting through
  its env var, the scripts that set an env var to force a mode, and the fingerprint entries all have to change in the
  same commit as the file, and the file cannot change without the migration (parity). A separate code PR would have
  nothing left that does not depend on the migration file, and branched from `origin/main` it would be red until the
  migration merged. No readiness raise (nothing reads the column), so the Migration preflight accepts it; deploying it
  before the Migration Apply changes nothing (the code never reads the column).
- **`settings-edit` block (generator).** CIS 428 changes some fields of existing keys: `-- settings-edit:begin` …
  `$edit$[{"key", <field>: <value>}]$edit$` … `-- settings-edit:end`, applied in file order after the seed and guide
  rows. A field outside the definition's fields (or `key` / `app`) or an unknown key fails the generator. The SQL
  applies exactly those fields (`env_override` when present, `description`, `guide`), skips equal rows (re-runnable),
  and a post-check raises when a row is not in force or any definition still names an env var.
- **Text follows the switch.** The 25 retired keys lose the "env var stays an emergency override for one release"
  sentence from their description and the matching sentence from the EN / VI guide `what`; the erasure key's
  description says the Dev drill sets 0 on the page. The 14 TripPlanner keys whose description and guide named the
  TripPlanner env fallback are edited too: AA-TripPlanner-Web drops those env reads in the same wave (its fallback is
  the code default, equal to the seed).
- **settings-lint for env overrides** (`tests/unit/test_aa1096_settings_env_lint.py`, runs in Unit Tests):
  1. a retired key (`tests/unit/snapshots/aa1096_retired_env_overrides.json`, 25 keys) never gets an override back,
     and no definition names a retired env var;
  2. any future override needs a window in `tests/unit/snapshots/aa1096_env_override_window.json` (`env`, `added`,
     `remove_by`, `issue`; at most 14 days); the test fails once `remove_by` has passed, until a `settings-edit`
     migration retires it, and fails on an entry whose definition lost the override;
  3. no runtime code (`api/`, `services/`, `shared/`, `worker/`, `scripts/`) holds a string literal equal to a
     retired env name or a retired class family prefix (`T10_SA_MODE_`, `T10_ADJ_MODE_`). Docstrings are skipped.
     The one exception is `T9_LENGTH_EXTEND` as the **trial flag** key of a `t9_write` job (`services/quality/
     trial_flags.py`, `length_extend.EXTEND_FLAG`), which is a payload key, never an env var.
  The env_override mechanism itself (reader step 1, the page badge, 409 `env_override_active`) stays.
- **The erasure window has no env path any more.** `erasure_job.tenant_retention_days()` takes no argument and reads
  the setting; the `env` parameter chain that only carried the override (`policy.windows`, `saga.schedule`,
  `store.create_request`, `store.on_offboard`) is gone. The Dev erasure drill proposes and approves 0 on Platform ›
  Settings for its run (the admin may self-approve in the dev phase) and restores 30 after it.
- **Scripts.** `scripts/aa1077/extend_trial.py` passes `switch=on` to `length_extend.plan_for` instead of setting the
  env var in its process. `scripts/aa788/promotion_replay.py` lays the promoted check modes over the quality-gate mode
  readers (`modes()`, `mock.patch.object`) instead of setting env vars. `scripts/aa1077/seed_floor_replay.py` reports
  `recommended_setting` (`cis.t7.blog_chars_per_seed_word`) instead of `recommended_env`.
  `services/quality/policy_harness.py` forces F12 block through `f12_scope_mode`, not the env.
- **Release fingerprint.** The retired names leave `config_fp.MODE_FLAGS`, and `MODE_FLAG_PREFIXES` is empty (the
  mechanism stays). Only names that are set enter the mode_flags hash, and none is set on Dev, so no stored
  fingerprint changes; the values are in the policy part already (`quality_settings.settings_fp`).

## Changed

- `api/migrations/428_aa1096_retire_settings_env_overrides.sql` (new, contract), `scripts/settings/gen_compiled_defaults.py`
  (`settings-edit`), `shared/settings/compiled_defaults.json` (regenerated: 0 env overrides).
- Code: the `*_ENV` constants of the retired switches removed (`brief_grounding.MODE_ENV`, `f9_policy.F9_POLICY_MODE_ENV`,
  `quality_gates.F12/F9/F14_*_MODE_ENV`, `adjudicate.MODE_ENV`, `modes.MODE_ENV` + `_env_for`, `blog_seed.RATIO_ENV`,
  `debate.DEBATE_MODE_ENV`), `length_extend.EXTEND_ENV` renamed `EXTEND_FLAG` (the trial flag); docstrings, comments,
  `CONTEXT.md` and two operator texts (`brand_agreement`, `measure_aa1079_brand_fit`) name the setting.
- Tests: `tests/unit/settings_serve.serve_retired_env` serves each retired key from its old env name (as an approved
  value), so the tests written for the env switches keep driving the setting with `setenv`; 16 modules use it
  (autouse fixture). Direct checks of the override now assert it is gone (`test_s279_quality_settings_parity`,
  `test_aa839_erasure`, `test_aa1093_erasure_saga`, the 418 integration test). The registry 409 test lays an invented
  override on the tenant key. New: the env lint, `tests/integration/test_aa1096_migration_428.py`.

## Tradeoffs

- The window rule fails CI on `main` once a date passes (a time-based red). That is the point (the override must go
  after one release); the message says what to do. Today the window file is empty, so nothing can go red by date.
- `writing_policy.yaml` keeps `env:` as the id of each `gate_promotions` row: renaming it changes the writing policy sha
  and every writer release would ask for a re-verify. Nothing reads it as an env var.
- `serve_retired_env` reproduces the old override in tests (source `platform`) rather than rewriting ~150 test lines
  to `serve({...})`: smaller diff, same behaviour, and the lint guarantees no runtime code reads those names.

## Should know

- Number 428: 421-427 are taken by other lanes of this wave (orchestrator hint). If main max + 1 differs at merge,
  renumber (`git grep '428_'` and `'428'` in the migration, the 428 integration test, the generator docstring,
  `compiled_defaults.json`'s `_generated` line, this file, the env lint docstring, `CONTEXT.md`).
- Other env-only thresholds were seen on the way and are NOT migrated settings (out of scope, follow-up material for
  the thresholds audit): `ERASURE_DRY_RUN_MAX_AGE_HOURS`, `T7_DEBATE_CONCURRENCY`.
- AA-CIS-Infra drops the `erasure_tenant_retention_days` variable (the drill's env) in the same wave.

# AA-1025 — slice A7 (lane l1-cis-gw-routes, S264): central LLM budgets

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` row A7 (gaps CIS-1, OPS-07). Tech lead decision
T-8 (S263): cap = p95 of the daily spend of the last 30 days × 1.5; Dev caps are alert-only, never a hard stop on
normal traffic; hard stops stay on tenant budgets. Two PRs: migration 340 (this file's first part), then the code PR.

## Decisions

- **One provider value `llm` in `shared.spend_budget`** (every LLM provider together), not a stage cap per provider:
  a stage cap must also cover the route's OpenAI / Anthropic fallbacks. The existing provider rows (`bedrock` /
  `dfs` / ..., `global` and `job:<kind>`, loaded by the jobs' own `RunBudget`) are untouched.
- **Scopes of an `llm` row**: `app:<app>` (llm_call_log.app; NULL counts as `cis`, the rows before ledger v2),
  `stage:<stage>`, `job:<kind>` (the spend of jobs of that kind; `per_run_usd` = the spend of the running job), `eval`
  (the eval runs: an eval label bound, or the stage `writing_memory_eval`), `adhoc` (stages `adhoc_*`). No `global`
  for `llm` (`app:cis` is the whole app). The CHECK keeps provider rows on `global | job:%` as before.
- **A month axis** (`per_month_usd`): T-8 asks monthly caps for eval and ad-hoc; one nullable column, read only by the
  central check. Any `llm` row may use day and / or month.
- **The T-8 seed is computed by the migration from the database's own ledger**, alert-only: `('llm', 'app:cis')`
  per_day = round(p95 × 1.5, 2) over the last 30 full UTC days (days without spend count as 0; today excluded; other
  apps' rows excluded). No row when there is no CIS spend (CI, replay). Never overwrites an admin row. No stage / job /
  eval / ad-hoc row is seeded (T-8: loose caps; admins add them).

## Changed

- `api/migrations/340_spend_budget_central_llm_scopes.sql` (contract: CHECK changes, a column, a config row).
- `tests/integration/test_aa1025_migration_340.py`.

## Tradeoffs

- The seed amount differs per database (it is data-driven); the guard checks its shape (alert-only, day only), not its
  value. On Dev the value is printed in the apply log (NOTICE).

## Should know

- Contract class: applied on Dev by Infra Migration Apply with every job table idle (root ADR 0017a), queued behind
  mig 336 while `DB_AUTO_APPLY_CONTRACT=false`.
- The code PR reads the rows tolerantly (`to_jsonb` of the row): before 340 there is no `llm` row, so nothing is
  checked and nothing changes.

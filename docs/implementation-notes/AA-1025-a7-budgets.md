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
- **Code PR — where the check sits**: `LLMClient._generate_with`, after the stage switch (A1) and the tenant budget
  (AA-1016), before any attempt; same shape as the tenant gate: a refusal writes the 0 USD `refused` / `budget` row
  (`log_gateway_failed_attempt`, classified by `attempt_outcome` since it is a `BudgetExceeded`) and is raised. Not
  checked: shadow calls (`_start_shadow`), `probe` (the admin's Test connection and the contract smoke) and embeddings —
  the same paths the tenant budget leaves out.
- **Refuse on the worst case, not on the past only**: hard cap refuses when spent + `estimate_call_usd` (route's first
  model, prompt chars / 4 input tokens, the whole `max_tokens` output, `pricing.calc_cost`) would pass it, like
  `RunBudget.check(estimate)`. That is what makes the live check deterministic: `stage:t9_write` = 0.01 USD refuses a T9
  write (Sonnet 5.5, 4096 output tokens ≈ 0.04 USD) before any spend. The estimate is computed only when a central cap
  exists (`central_budget.active()`), so a database without `llm` rows pays nothing for it.
- **Fail open** (like the connection budget): rows or spend unreadable = not enforced, logged once per failure window.
  The tenant budget fails closed for T-stages because a tenant can owe money; a central cap is an internal guard rail,
  and failing closed would stop every LLM call on a DB blip.
- **`CentralBudgetExceeded(BudgetExceeded, RuntimeError)`**, the `ConnectionBudgetExceeded` pattern: the job runner
  ends the job `stopped_budget`; callers that catch the gateway's RuntimeError still do (they degrade as for any
  gateway failure). No pipeline re-raise was added (unlike the tenant approval stop): a central hard cap is a manual
  emergency brake, not a wait-for-approval flow.
- **Caching**: rows 60 s, spend 30 s, job kind 10 min per process; the admin PUT / DELETE drop the API process cache at
  once (`central_budget.invalidate()`), the worker sees a change within a minute.
- **Alerts**: `cost_guard.maybe_alert_central` (same bell type `spend_budget_alert`, entity `llm:<scope>`, one unread
  per 24 h; the ops e-mail through `enqueue_spend_budget_email`, whose summary keys it reuses) at `alert_pct` of any cap
  (read when the spend is loaded) and on a refusal.
- **Admin**: `PUT /admin/budgets/llm/{scope}` (same route as the provider rows; `per_month_usd` accepted only for
  `llm`, `per_run_usd` only for an `llm` `job:` scope; 409 with "needs CIS migration 340" before 340), new
  `DELETE /admin/budgets/llm/{scope}` (spend.write; provider rows are never deleted there). Reads use `to_jsonb(row)`
  so they work before and after 340.
- **Budgets tab**: a "Central LLM caps" section (Drawer editor, alert-only by default, Remove with confirm) and
  "Coverage per call site" (every CIS stage from the routes plus the stages seen this month / last 7 days, with the
  central caps it is under, the job kinds that called it, today / month spend, calls in 7 days). TripPlanner rows are
  left out (this gateway does not check them; slice H1).

## Changed

- Migration PR: `api/migrations/340_spend_budget_central_llm_scopes.sql` (contract: CHECK changes, a column, a config
  row); `tests/integration/test_aa1025_migration_340.py`.
- Code PR: `shared/llm_client/central_budget.py` (new), `shared/llm_client/client.py` (`_central_budget_gate`,
  `estimate_call_usd`), `shared/cost_guard.py` (`CENTRAL_PROVIDER`, `maybe_alert_central`),
  `api/routers/admin_budgets.py` (llm scopes, month cap, DELETE, 409), `api/core/staff_authz.py` (DELETE policy),
  `api/services/ai_gateway.py` (central tier, `call_site_coverage`), `docs/api/openapi.json` (regenerated with the
  pinned fastapi 0.161.1 / pydantic 2.14.1), frontend `lib/aiGateway.ts`, `BudgetsTab.tsx`, `budgets.messages.ts`,
  `guide.ts` / `guide.vi.ts`; CONTEXT.md. Tests: `tests/unit/test_aa1025_central_budget.py`,
  `tests/unit/test_aa1025_admin_central_budgets.py`, `tests/unit/test_aa804_ai_gateway_budgets.py` (central tier),
  `tests/unit/conftest.py` (no central cap in unit tests), `tests/unit/test_aa1002_no_behaviour_change.py` (module
  classified), `tests/unit/snapshots/aa882_staff_route_policy.json`, `tests/integration/test_aa1025_central_budget_sql.py`,
  `frontend/lib/aiGateway.test.mjs`.

## Tradeoffs

- The worst-case estimate can refuse a call that would have fit under a tight cap (it assumes every `max_tokens` is
  used). Accepted: caps are meant loose (T-8) and a hard cap is an emergency brake; the refusal message names the
  estimate.
- Spend is cached 30 s: a burst of parallel calls can pass a cap by up to that window's spend. Same as the connection
  budget; the caps are loose.

- The seed amount differs per database (it is data-driven); the guard checks its shape (alert-only, day only), not its
  value. On Dev the value is printed in the apply log (NOTICE).

## Should know

- Live check (after 340 is applied and the code deployed): PUT `llm` / `stage:t9_write` per_day 0.01, hard_stop true;
  one T9 write call (an ECS exec / Infra run-script call of `LLMClient().generate(LLMRequest(stage="t9_write", ...))`,
  or a T9 job) → `CentralBudgetExceeded`, one `llm_call_log` row `stage=t9_write status=refused error_class=budget
  cost_usd=0`, a `spend_budget_alert` bell with entity `llm:stage:t9_write`; then DELETE the cap.
- `LLM_CENTRAL_BUDGETS=off` (ECS env) switches the check off without a deploy of code.

- Contract class: applied on Dev by Infra Migration Apply with every job table idle (root ADR 0017a), queued behind
  mig 336 while `DB_AUTO_APPLY_CONTRACT=false`.
- The code PR reads the rows tolerantly (`to_jsonb` of the row): before 340 there is no `llm` row, so nothing is
  checked and nothing changes.

# AA-1024 — lane l13 (S264): A10 CIS runs on the shared core `aa_llm`, A5 check

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` rows A10 and A5. Branch
`feat/aa-1024-a10-a5-core-served-roles`, one CIS PR, no migration.

## A5 — served_roles (already on main)

- The code shipped in #1245 (lane gw-core, S263, notes `AA-1024-gw-core.md` § A5): `vendor_route.serves_role`,
  `deployment_plan(..., role=)` with `role_skips`, `role_refusal` → `RouteRoleNotServed`, `embed()` with role `embed`,
  `invariant.check_stage` counting only connections that serve the role and listing `role_skipped` in the
  `/admin/llm-availability` answer (`invariant.stages[].role_skipped`), and the unit tests in
  `tests/unit/test_aa1024_gw_core.py`. This lane found nothing missing, so it adds no A5 code; only the live check is
  left (live steps of the PR). Dropping `account_route` stays for later, as the plan says.

## A10 — CIS on the core

### Decisions
- **Step 0 in the core (CORE_VERSION 1.1.0).** `shared/aa_llm/errors.py` now owns the outcome classes that were
  CIS-only: `disabled` (the stage kill switch text, read first), `truncated` read from a provider's refusal to return
  a cut reply (`_TRUNCATED_ERROR`, before the patterns), `role_not_served`, `TRUNCATED_STOP_REASONS`, `is_truncated`,
  `reply_class`, `is_truncated_error`, `disabled_stage_of`, and `account_level_class` with the role-refusal rule.
  `LEDGER_ERROR_CLASSES` has all ten ledger classes (319 + 326 + 338), written as literals because the admin unit test
  parses the tuple. `ledger.py` gets `OUTCOME_CLASSES`, `outcome_of(class)`, `failed_outcome` = `outcome_of(classify)`
  (disabled / role_not_served / budget refused, deadline error, truncated truncated), and the module-level
  `v2_values(...)` that `LedgerRow.v2_values` now calls.
- **CIS imports, not copies.** `shared/llm_client/errors.py` re-exports the core names (one `LLMAccessDenied` class:
  `LLMStageDisabled` subclasses the core's) and keeps only the CIS gateway exceptions (`RouteDeadlineExceeded`,
  `RouteAttemptCapReached`, `RouteRoleNotServed`, `LLMStageDisabled`) and the job-runner helpers.
  `pricing.py` takes the tables, `Usage`, `Quote` from the core; `quote` = core `quote` with the catalog row CIS reads
  (`_catalog_rates`, still the patch point of the tests) and a structlog warning as `on_estimate`. `call_log.py` takes
  `V2_COLUMNS`, `STATUSES`, `ERROR_CLASSES` and the v2 normalisation (`_v2_values` = core `v2_values` + the CIS app
  default and the eval-label ContextVar). `client.attempt_outcome` asks `core failed_outcome` / `outcome_of` and keeps
  only the CIS-typed checks (kill-switch type, budget exceptions, deadline type, breaker credential / circuit / limiter,
  OpenAI platform block) in the order it had.
- **The statement text stays in `call_log.py`** (`_INSERT_V2_SQL`): the AA-729 eval harness relabels that string. A
  test pins its columns and casts to `core_ledger.COLUMNS` / `insert_sql()`.
- **Parity test → identity test.** `tests/unit/test_aa642_s8_aa_llm_parity.py` is now
  `tests/unit/test_aa1024_a10_core_identity.py`: the CIS names are the core's objects (`is`), the CIS quote equals the
  core quote with the same catalog row, `attempt_outcome` agrees with the core on provider text, a `record_call` row
  equals the core `LedgerRow` column by column (35 columns), and `_v2_values` equals the pre-A10 CIS writer (its code
  kept in the test as the reference) on 18 cases, with and without an eval label.

### Changed (vs. the plan)
- The plan says resync the TripPlanner / Partner copies in separate PRs or leave the sync bot. Left to the **Llm Core
  Sync bot** (`bot/llm-core-sync` in each consumer, opened on merge). Checked locally first: the core overlaid with
  `llm-core-sync.sh --from-local` on TripPlanner main (backend suite 884 passed; the 2 lock tests that fail only
  because a local lock says `ref: local`) and on AA-Partner main (`ruff check` + `ruff format --check` clean, unit
  suite 3215 passed).

### Tradeoffs
- Two small behaviour changes, both toward the right class, accepted rather than kept bug-for-bug:
  1. A text-only kill-switch refusal (no `LLMStageDisabled` in the chain, e.g. a re-raised job error) is now
     `refused / disabled` in the CIS ledger and in TripPlanner / CRM rows, not `error / other`.
  2. An error whose `llm_error_class` is `deadline` / `budget` without the CIS type is now `error / deadline` /
     `refused / budget`, not `error / other`. TripPlanner and the CRM already set these classes on their own rows;
     their `failed_outcome` callers see them only from such tagged errors.
- `truncated` read from text is new for TripPlanner and the CRM: an OpenAI "max_tokens or model output limit was
  reached" 400 is now a `truncated` attempt there too (it was `error / other`). The CRM's `crm.llm_call` CHECKs accept
  any `[a-z_]` class and the ledger statuses (`ledger.STATUSES`), TripPlanner writes `shared.llm_call_log` (CHECK 338).

### Should know
- No migration. The core change reaches TripPlanner and AA-Partner only when their bot sync PRs merge (their copy is
  pinned by `aa_llm.lock.json`, so nothing turns red on CIS merge).
- The frontend unit test `frontend/lib/aiGatewayPolish.test.mjs` now parses `shared/aa_llm/errors.py`
  `LEDGER_ERROR_CLASSES`.
- Integration tests could not run locally (the local Postgres has no pgvector, so the conftest replay stops); instead a
  scratch script wrote two rows with the new writer into the local test DB (at 319) and compared all 35 stored columns
  with the core row: equal. CI runs `tests/integration/test_aa804_ledger_v2_db.py` and the other ledger tests.

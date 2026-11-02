# AA-1026 — lane l1-cis-gw-routes (S264): a3_place_dedupe gets its acc2 rung (migration 339)

Open item of `AA-1026-gw-333-dedupe.md`: migration 333 left `a3_place_dedupe` on `gpt-6-luna → gpt-6-luna-openai`
because no enabled acc2 model of another maker than Amazon was known to answer its structured verdict. Since then:
migration 337 + CIS #1245 ask Llama 4 Maverick / Scout for the object in the system prompt (`structured_output:
prompt`, no `toolConfig`), and the live contract smoke of `a3_place_dedupe` on `llama4-maverick` (acc2, prompt mode)
PASSED on 02/11 (0.00021 USD).

## Decisions

- **Same shape as 333** (insert the pick right before the first OpenAI API hop; only that key moves), restricted to
  the one stage. Guards copied from 333 (registry, acc2 connection, pick catalog / acc2 deployment, writer rule,
  failure domains, re-apply = no-op, order gate on the latest `adhoc_aa1026` smoke row of the stage on the pick).
- **The 333 "refuses a forced tool choice" guard is replaced by a structured-capability guard**: the pick passes when
  its catalog row does not refuse a forced tool choice, or when it states a non-forced `structured_output` mode the code
  knows (`any` / `auto` / `prompt`). Maverick has `forced_tool_choice: false` + `structured_output: prompt` (337), so
  the file also requires 337 to be registered.
- **Place pair keeps two makers** (the 331 pair rule, which 333 dropped together with the stage): no family of the
  dedupe route is a family of the check stage's route. After 339: dedupe {OpenAI, Meta}, check {Anthropic, Amazon}.
- **Maverick, not Scout**: Scout is cheaper but was never smoked in prompt mode for this stage; the order gate needs a
  passing smoke of the pick. Re-picking Scout later = smoke it with the `models` override, then a new migration.
- **Contract smoke plan**: the code PR of the lane adds `"a3_place_dedupe": "llama4-maverick"` to
  `services/jobs/llm_contract_smoke_job.py` `PLANNED`, so a plain smoke run covers the stage (no override needed).

## Changed

- `api/migrations/339_llm_place_dedupe_acc2_maverick.sql` (contract).
- `tests/unit/snapshots/aa1026_routes_after_339.json` (the 333 routes with the one insert).
- `tests/integration/test_aa1026_migration_339.py` (expected routes, re-apply, admin-appended pick, route not ending on
  the OpenAI API, 337 guard, capability guard, acc2 deployment guard, pair guard, order gate, empty ledger).
- `tests/unit/test_aa1026_dedupe_acc2_route.py` (walks every route through `LLMClient.generate` with providers failing:
  dedupe = acc3 → Maverick on acc2 Converse → OpenAI API; every other stage unchanged; failure-domain report).

- Code PR of the lane: `services/jobs/llm_contract_smoke_job.py` `PLANNED["a3_place_dedupe"] = "llama4-maverick"`
  (+ comment), `tests/unit/test_aa1026_contract_smoke.py` (Maverick fixture with the 337 capabilities; every case is
  planned; the plan runs the stage on Maverick in prompt mode), CONTEXT.md (339, the plan's structured-mode rule).

## Tradeoffs

- No A/B or shadow rung for Maverick (as 331 / 333): the verdict is checked by `a3_place_dedupe_check` (two makers must
  agree, else the pair is held), so an acc2 verdict alone merges nothing.

## Should know

- Live order: the migration-only PR merges, the Infra bot copies it, Infra Migration Apply applies it (class contract)
  once every job table is idle (root ADR 0017a). Dev still has `DB_AUTO_APPLY_CONTRACT=false` until mig 336 is
  handled; 339 / 340 queue behind 336 (strict prefix).
- The order gate reads the 02/11 smoke row (`adhoc_aa1026`, `contract_smoke = a3_place_dedupe`, model
  `llama4-maverick`, acc2, `parser_ok = true`). A later failed smoke of the same pair refuses the apply.

# AA-1026 — lane gw-333-dedupe: a3_place_dedupe out of 333; the smoke counts a refused call as failed

S263 (01/11/2026). The live contract smoke on Dev after #1238 passed 19 of 20 stages on their acc2 pick.
`a3_place_dedupe` on `llama4-scout` got:

> ValidationException ... Converse operation: This model doesn't support the toolConfig.toolChoice.tool field.

`a3_place_dedupe` asks a structured verdict (`services/places/dedupe.py` `VERDICT_SCHEMA`), which the gateway sends on
Converse as a FORCED tool choice (`toolChoice.tool`). Yet the job summary said `failed: []`, `passed: 19`: the stage
was in neither list. Two changes, two PRs:

- **PR A (CIS #1239, migration 333):** `a3_place_dedupe` is no longer moved; 333 records the Scout fact in the catalog.
- **PR B (this file's PR, code, from main):** the smoke counts a refused / failed provider call as FAILED and writes a
  failing smoke row; the plan refuses a structured stage on a model whose catalog row says it takes no forced tool
  choice.

## Decisions

- **Why the stage was absent from the summary.** `run_case` set `called = bool(spent)`: a call that raised before any
  reply left `spent` empty, so the stage counted as "not called", and `summarize` lists only called stages under
  `passed` / `failed` (it went to `skipped`). A refused call did send its request; `called` is now always True in
  `run_case`, and a new `billed` says whether a reply came back.
- **A failing smoke row for a stage with no reply.** The migrations' order gate reads the LATEST `adhoc_aa1026` row
  of the stage on the model. A refused call wrote no smoke row (the gateway's attempt row carries
  `connection_test`, not `contract_smoke`), so an older passing row would still open the gate. Now such a stage writes
  one row: 0 USD, 0 tokens, `status` error, `error_class` (classify's class when it is a ledger class, else other),
  `quality_signal` = `{contract_smoke, parser_ok: false, error_class, error (ids redacted), no_reply: true}`. The same
  for a billed unusable reply: the gateway's `billed_unusable` row keeps the cost, the smoke row says the stage failed.
  No `billed_fields(resp)` on that row: there is no reply (the AST guard only checks rows logged from a reply).
- **Tool-choice support is data the gateway already keeps**: catalog `capabilities.forced_tool_choice` (AA-1017,
  migration 320; `client._forced_tool_choice_ok` reads it first). Every acc2 candidate row had `{}` (324). 333 now
  records `llama4-scout` → `false` (only when the row does not state it: an admin or later migration value wins). With
  it the gateway asks Scout with `auto` + an instruction from the first structured call, and the smoke plan refuses
  Scout for a structured stage.
- **What a stage "requires" is read from its request**: a JSON schema = Converse forced tool choice
  (`requires(request) -> ("forced_tool_choice",)`). The plan shows `requires`; a model whose catalog row states the
  capability false makes the stage `callable: false` with a `why_not` naming it. Unstated = allowed: the live call is
  the proof (and with PR B it fails visibly).
- **No re-pick for a3_place_dedupe; it is removed from 333 (seven stages).** The pick must (1) take
  `toolChoice.tool` on Bedrock Converse and (2) be another maker than Amazon (its check stage `a3_place_dedupe_check`
  falls back to Nova Lite, 331). Enabled acc2 chat models: Nova Lite / Nova 2 Lite (Amazon: fail 2), Llama 4 Scout
  (refused live), Llama 4 Maverick (Meta, same family as Scout, untested), Kimi K3 (Moonshot, untested, a reasoning
  model at 3.30 / 16.50 USD per 1M tokens; the 331 notes already say "never kimi-k3 for a format contract"). None is
  KNOWN to meet both, so per the lane brief the stage leaves 333. `PLANNED` has no entry for it (the case stays, so an
  override run can qualify a candidate).
- **333's guard replaced:** the "check pair shares no maker" guard only served `a3_place_dedupe`; it is gone. A new
  guard refuses a pick whose catalog row says `forced_tool_choice` false (unknown = allowed; the order gate's smoke
  proves the stage's own request).
- **Not changed: `_tool_choice_rejected` in `shared/llm_client/client.py`.** Its wording list ("not supported",
  "unsupported", ...) does not match Bedrock's "doesn't support", so the gateway's runtime downgrade to `auto` did
  not fire. Adding the wording is a gateway behaviour change outside this lane (the S261 readiness audit lists it), and
  it would also let a smoke of an untested model pass on `auto`, hiding whether it takes a forced tool. Follow-up.

## Changed

- PR A (`feat/aa-1026-acc2-before-openai`, #1239): `api/migrations/333_llm_judge_validate_acc2_before_openai.sql`
  (seven stages; step 0b catalog capability; forced-tool guard; header); `tests/unit/snapshots/
  aa1026_routes_after_333.json` (a3_place_dedupe as after 332); `tests/unit/test_aa1026_acc2_before_openai_routes.py`;
  `tests/integration/test_aa1026_migration_333.py` (dedupe kept, Scout capability recorded / an admin value kept, the
  forced-tool guard, Scout as dedupe pick refused); `test_aa1017_migration_324.py` (Scout may carry the 333
  capability); `test_aa1002_migration_304.py`, `test_aa1017_migration_320_321.py` (333 re-signs the Scout catalog row:
  the "set 324 aside" steps match `migration-333-aa1026` too).
- PR B (`fix/aa-1026-smoke-errors-fail`): `services/jobs/llm_contract_smoke_job.py` (`run_case` `called` / `billed`,
  `_log_replies` failing row, `requires` / `refused_capabilities`, plan `requires` + capability check, `PLANNED`
  without a3_place_dedupe); `tests/unit/test_aa1026_contract_smoke.py`; CONTEXT.md (333 stages, smoke semantics);
  `AA-1026-gw-acc2-fallback.md` (the dedupe line).

## Tradeoffs

- `a3_place_dedupe` keeps leaving AWS for the OpenAI API while acc3 refuses (its route is gpt-6-luna →
  gpt-6-luna-openai). Accepted over (a) a Nova pick, which breaks the two-maker pair with its check stage, or (b) an
  untested Maverick / Kimi pick, which the order gate would refuse anyway without a passing smoke.
- The failing smoke row is an extra 0 USD row per refused stage in `shared.llm_call_log` (stage `adhoc_aa1026`).
- `requires` treats every JSON-schema request as needing a forced tool choice, although the gateway could downgrade
  to `auto` for such a model in production. The smoke's job is to prove the stage's contract as the stage sends it.

## Should know

- **Order:** PR B (code) merges and deploys (api + worker) BEFORE 333 applies. Then re-run the smoke live for the
  seven 333 stages (their latest rows must be passing; the S263 run already passed them, a re-run is cheap evidence
  under the new semantics), then 332 → 333 (Infra byte copy, DB Migrate check / apply with idle job tables).
- After 333 applies, a DRY RUN with `"models": {"a3_place_dedupe": "llama4-scout"}` shows `callable: false` and the
  `why_not` (the catalog row now says false): the plan catches it, not Bedrock.
- To close the open item: smoke `a3_place_dedupe` live with `"models": {"a3_place_dedupe": "llama4-maverick"}` (about
  0.0002 USD). A pass (forced tool taken) → a new migration adds Maverick before the OpenAI API hop (Meta vs the
  check's Amazon: two makers). A ValidationException → record `forced_tool_choice: false` on Maverick too and leave
  the stage on its route.

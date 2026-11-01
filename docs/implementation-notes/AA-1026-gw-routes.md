# AA-1026 — lane gw-routes (S263): route changes the tech lead approved

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.I (I1b, I2, I3, I5) and §3 (T-2, T-3, T-4).
This lane: migration 335, the admin route PATCH exception for the writers' last resort, tests, CONTEXT.md. The root
runbook / orchestrator script / AI Use Register update is the root PR of the same lane.

## Decisions

- **One contract migration (335)** carries T-4, T-3 and I3 (all config, all "data, not code"). Contract, not expand:
  it UPDATEs existing routing rows (root ADR 0017 d3), so DB Migrate apply by hand with the job tables idle.
- **T-4 mapping follows the legacy chain, not just the key name.** A CIS `sonnet` key ran Sonnet 4.6 → Haiku 4.5 (the
  hard-coded chain, migration 307's alias rows), so it becomes `sonnet-5-5` → `haiku-5-5`; `haiku` becomes `haiku-5-5`.
  Duplicates are dropped, first occurrence wins. Same family first (Anthropic stays Anthropic), same tier.
- **TripPlanner stages (`tp_*`) map one to one** (`sonnet` → `sonnet-5-5`, no Haiku added): TripPlanner's own gateway
  called only the route's models, it never had the chain's degrade. No TripPlanner code change: its gateway already
  calls Converse rows through `bedrock_profile_ids['acc3']` (`_text_call`), and the builtin route (used only when the
  view is unreadable) can stay on `sonnet`.
- **Inactive rows move too** (`s1_atom_writer`, retired): no route names a legacy key after 335 (guard).
- **T-3 writers = the five named in the plan.** `s1_atom_writer` is the only other A-series Claude writer in the route
  list; it has no caller (AA-611), so it is not opened.
- **"AWS first, vendor after" holds for the whole route (review fix).** The gateway tries every deployment of a model
  (`vendor_route.deployment_plan`: acc3 priority 10, anthropic-direct priority 100) before the route's next model
  (`LLMClient._call_plan`). The first cut kept `supply_content_writer` on [sonnet-5-5, haiku-5-5, gpt-6-luna-openai],
  so with the Anthropic key on, a sonnet-only acc3 failure (throttle, timeout, model-level refusal: anything that does
  not open the whole acc3 connection circuit) would have sent the content to the Anthropic API before haiku-5-5 on
  acc3, against T-3 ("go outside only if the AWS paths are blocked"). Chosen fix: config, not gateway code. Step 2 of
  335 drops every writer fallback reached inside AWS that follows a model with a deployment outside AWS (counted over
  every deployment, enabled or not, like the admin's reach), so today it drops only that `haiku-5-5`, and all five
  writers are [one Anthropic model, gpt-6-luna-openai]: acc3 → Anthropic API → OpenAI API. A guard in 335 and
  `_check_aws_before_vendor` on both admin PATCH endpoints keep that order. Rejected: (b) a gateway walk "all AWS
  deployments of every model first, then the vendor ones" — a change to the core call path (unusable replies,
  deadlines, budget / circuit skips, ledger positions) for one stage, not asked; (c) accepting the per-model order —
  not what T-3 says.
- **I3 in the same migration**, not an admin PATCH: reviewed and reproducible. Only when `monthly_budget_usd IS NULL`
  and the policy row is missing (`ON CONFLICT DO NOTHING`), so an admin value is kept. `credential_env` stays
  `OPENAI_API_KEY` (the key keeps coming from the ECS task env; the tech lead said do not change its delivery).
- **SAFE_DEFAULTS stay on the legacy keys** (lesson S222): they serve a process that cannot read the database, where the
  catalog is unreadable too and only the hard-coded legacy chain works.
- **Admin route PATCH: one narrow exception** (`_writer_last_resort`): the LAST fallback of a writer stage whose
  `allowed_scopes` include vendor_direct may be an enabled, gateway-callable model of another maker reached only outside
  AWS. Without it, every route PATCH of the five writers (e.g. a shadow change) would answer 422, because the PATCH
  re-validates the fallbacks it keeps and the writer/judge vendor rule marks GPT "not selectable" for a writer.

## Changed

- `api/migrations/335_llm_routes_converse_keys_writer_scopes.sql` (new, contract).
- `api/routers/admin_llm_ops.py`: `_writer_last_resort` + its use in `patch_llm_route`; `_check_aws_before_vendor`
  (review fix) in `patch_llm_route` and `patch_llm_config`, after the vendor-rule checks (their message stays first).
- Tests: `tests/integration/test_aa1026_migration_335.py`, `tests/unit/test_aa1026_route_moves.py`,
  `tests/unit/test_aa1026_admin_writer_last_resort.py`, snapshot `tests/unit/snapshots/aa1026_routes_after_335.json`.
- Replay-state tests that read the route table after the full replay now expect the post-335 rows:
  `test_aa784_migration_286.py` (a3_place_card_check), `test_aa803_migration_259.py` (which stages route the 5.5 keys;
  its teardown also puts back the haiku-5-5 / sonnet-5-5 deployments its `_undo` cascades away),
  `test_aa835_migration_278.py`, `test_aa835_migration_282.py` (its restore now ends with 335; both expect
  `supply_content_writer` = sonnet-5-5 → gpt-6-luna-openai after the review fix).
- `CONTEXT.md`: writers' last resort, the T-4 move.

## Tradeoffs

- **A GPT draft judged by a GPT judge.** When every AWS path is blocked, an A-series writer's text comes from GPT-6 Luna
  and is scored by the GPT judges (same maker). Accepted by T-3 ("có thể dùng gpt"); the ledger row says
  `provider='openai'`, `fallback_used=true` (`llm_openai_fallback_used` event), and A2 human review stays. When the A4
  family rule (writer ≠ judge family, checked at call time) lands, it must allow or flag this case explicitly.
- **Temperature**: the 5.5 rows have `supports_temperature = false`, so call sites that send a temperature lose it
  (the gateway drops it). No prompt change was asked; quality is checked later by eval when LLM access is back.
- **Cost goes down** at catalog prices (haiku 1/5 → 0.1/0.5, sonnet 3/15 → 2/10 USD per 1M tokens), but a moved
  `sonnet` stage now has an explicit `haiku-5-5` fallback that the old chain had implicitly: same number of attempts.
- **`supply_content_writer` loses the Haiku degrade** (it had acc3 Sonnet → acc3 Haiku inside the legacy chain). In
  exchange, when only Sonnet fails on acc3 it goes to Sonnet on the Anthropic API (once the key is in), then GPT-6
  Luna: a stronger model than Haiku for customer copy, and staff review every draft. With the Anthropic key off (today)
  a Sonnet-only acc3 failure goes straight to GPT-6 Luna on the OpenAI API (inside its 10 USD budget).

## Should know

- **Order (MIG-4):** this migration-only content can merge with the code (the admin exception works with or without
  335; the migration needs no code). Then the Infra byte copy → DB Migrate check/apply (job tables idle) →
  bot/db-schema-reference PR. The number 335 is above main's max (324): the preflight wants main max + 1, so either
  the lanes holding 325–334 merge first or the PR gets label `migration:out-of-order` (or renumber: `git grep '335_'`).
- **Do not re-run the Infra run-script `aa1002_dev_anthropic_road.py` in apply mode after 335:** it derives the stages
  to open from the live routes, and once the T-stages route on `sonnet-5-5` / `haiku-5-5` it would open them to
  vendor_direct, against T-3 (T-stages stay aws_only).
- **Writing-memory releases**: 335 changes the routes of `s1_generate`, `t2_generate`, `t9_write` outside the admin
  PATCH, so the reverify scan the PATCH triggers does not run. After apply, run
  `POST /admin/writing-memory/releases/reverify-scan` once.
- Dev before 335 (from AA-1002's S261 record): `s1_generate` and `supply_content_writer` already had both scopes
  (road script), `anthropic-direct` already has `monthly_budget_usd = 150`.
- Root page `docs/generated/llm-routes.md` is generated: after apply, recapture (`tools/docs/gen_llm_routes.py dump`
  in the container, `import` locally). Expected diff: the 22 rows of `aa1026_routes_after_335.json` that differ from
  the 29/10 snapshot (21 legacy stages + `s1_generate`), "Used by" of `haiku` / `sonnet` empty, `haiku-5-5` /
  `sonnet-5-5` / `gpt-6-luna-openai` used by the moved stages.

# AA-900 / AA-741 — S271 lane l12: W0 routes (remove uncalibrated rungs)

Tech lead decisions D7 and D16 (06/11/2026, writing-quality restructure, root
`docs/audits/2026-11-06-S271-writing-quality-audit/`): the judge that may hold a piece is calibrated and pinned; GPT-6
leaves every writer route. Roadmap W0 item "Routes (one migration with a '-- break-glass: remove uncalibrated rungs'
header)" and critique F4 / M6 / M7 widened it to every writer route and to the writing-memory judges.

One PR: CIS migration 383 (migration-only, contract) with its integration test, the route snapshot after 383 and the
CI unit lint. No code PR: the lint is a test over the snapshots.

## Decisions

- **Rule-based removal, not a stage table.** 383 removes `gpt-6-luna-openai` from every `role = writer` row and
  `nova-lite` from every `role = judge` row plus `t10_claims`, keeping the order of the rest. A table of exact
  before/after chains (the 366 / 376 pattern) would skip an admin's chain and leave the rung on it; D7 is "every
  writer route". Rows whose chain differs from the post-376 one are reported in a NOTICE, the rule still applies.
- **The two brand audits are in.** The lane brief listed the judge stages without `s1_brand_audit` / `t2_brand_audit`;
  they are judge rows with the same chain, a brand-voice verdict is a judge verdict, and the lint "no judge route
  names nova-lite" would otherwise fail on them.
- **Validate rows kept on purpose**, recorded in the migration header and in the lint's `NOVA_LITE_KEPT` allowlist
  with the reason: `t9_faq_filter` / `t9_faq_answer_fit` (drop-only, flag off by default, primary is Claude, so Nova
  Lite is the only other-maker rung and makes no same-family pair: no verifier-family hazard), and the A3 / S1
  evidence producers (`a3_atomize`, `a3_place_card`, `a3_place_days`, `a3_place_dedupe_check`, `a3_place_locate`,
  `s1_source_facts`), which F10 measures in W1 / W2 before any change. A NEW validate stage with Nova Lite fails
  the lint until someone adds it to the allowlist with a reason.
- **`t10_claims` is the only validate instrument** (`INSTRUMENT_VALIDATE`): the F9 v2 claim extractor W2 scores as
  an instrument.
- **No new rung added.** The large writers end on `[sonnet-5-5, sonnet-4-6]` and the small ones on
  `[haiku-5-5, haiku-4-5]` (4 possible attempts: acc3 + Anthropic API each). Haiku 4.5 could come back on the large
  writers within the 6-attempt cap, but that is a quality choice (Haiku writing tour prose) nobody decided.
- **Sibling fill for a writer left with no fallback.** On Dev no writer is in that state, but the replayed
  migrations leave `s1_generate` / `t2_generate` on `[haiku-5-5, gpt-6-luna-openai]` (the Dev rows were moved by an
  admin, not a migration), and an admin could save such a chain. Such a row gets its primary's older sibling (the 366
  rule: Sonnet 5.5 -> 4.6, Haiku 5.5 -> 4.5); any other primary stops the file. Without it the file would RAISE on
  the CI replay and never register there.
- **`degrade_mode` queue_retry** on the changed writers whose mode was NULL (9 rows; `t2_generate`, `t9_write`,
  `segment_research*` already had it). It is declarative (the failure-domain report): these rows keep two failure
  domains (acc3 and anthropic-direct), so no report line changes today.
- **Number 383** = CIS main max (382) + 1 on 06/11. Renumber recipe in the header.
- **Snapshot as the lint's source.** The repo already proves each route migration against a snapshot JSON on real
  Postgres (366, 376); the lint reads the snapshot of the highest migration number, so a later route migration that
  adds its snapshot is linted automatically. `SAFE_DEFAULTS` (the no-database routes) are linted too.

## Changed

- `api/migrations/383_llm_w0_remove_uncalibrated_rungs.sql` (new, contract, break-glass header line first).
- `tests/unit/snapshots/aa900_routes_after_383.json` (new): `aa1042_routes_after_376.json` minus the rungs.
- `tests/integration/test_aa900_migration_383.py` (new, 14 tests): the routes equal the snapshot, re-apply is a no-op
  (no second version), degrade modes, `llm_route_chains.violations` and the admin `llm_route_control.chain_problems`
  on every changed row from the rows the database holds, a control case the same check refuses, admin chains,
  refusals (primary to remove, no fallback and no sibling, another-family writer rung), the external route version.
- `tests/unit/test_aa900_w0_route_lint.py` (new, 9 tests): W0-1 / W0-2 on the latest snapshot and `SAFE_DEFAULTS`,
  allowlist freshness, not vacuous (the 376 table breaks both rules on exactly 383's stages), chain rules, and the
  route control preview of 383's chains from the 376 table (valid, every stage serving, needs evidence = break-glass).
- Replay-state tests updated for the new final routes: `test_aa835_migration_278.py` (supply writer chain),
  `test_aa642_migration_326.py` (degrade modes),
  `test_aa835_migration_282.py` (supply writer chain), `test_aa803_migration_259.py` (actor list),
  `test_aa917_migration_280.py` (writing_memory_mine chain).
- `CONTEXT.md`: one bullet "No uncalibrated rung" in the route section.

## Tradeoffs

- **Availability for honesty.** A writer whose Claude rungs all fail (acc3 refusing, the Anthropic API key capped
  or refused) now fails and re-queues instead of answering on GPT-6 Luna; a judge whose GPT rungs all fail (acc3
  refusing, the OpenAI API at its 10 USD cap) returns no verdict instead of a Nova Lite one. That is D7's intent
  ("miss = unverified"). Today the Anthropic API serves every Claude primary (route version 4), so the writers lose
  nothing in practice; the judges lose their acc2 domain and depend on the OpenAI API while acc3 refuses GPT.
- **Rule over table** means an admin chain changes too. Accepted: the decision is about the model, not the chain.

## Should know

- Apply order: migration-only PR -> bot copy to AA-CIS-Infra `db/migrations/cis/` -> Infra Migration Apply (contract,
  every job table idle) -> bot/db-schema-reference PR. No readiness raise, no code depends on 383.
- The admin route PATCH still accepts the OpenAI API as a writer's last resort (`_writer_last_resort`,
  `llm_route_chains.violations` `writer_family` exception). An admin can re-add the rung through the UI; the lint
  only guards migrations. Follow-up: a code PR that drops that exception so the route control refuses it.
- The root `docs/generated/llm-routes.snapshot.json` / `llm-routes.md` show the old chains until their generator runs
  after the apply.
- The `llm_route_chains` module docstring still describes the OpenAI API at the end of a writer route (code, left for
  that follow-up PR).
- Writing-memory release fingerprints read writer / judge routes: run the reverify scan after the apply.
- The 383 route config version records, like 376's, any deployment drift not yet versioned when the file applies (a
  `provider_order` entry 383 did not cause). The integration test compares those entries with the drift found
  before the body runs, so it holds whatever earlier test files of a CI shard leave behind (shard 3 runs
  `test_aa862_migration_240.py` first, which leaves `cohere-embed-v4-acc3` out of step with the last version).
- Review fix pass: `test_aa835_migration_278.py` asserted the replayed `supply_content_writer` chain with the OpenAI
  rung; it now expects `["sonnet-4-6"]` (383 in its migration-history comment). The earlier note that its failures
  were a local test-order artefact was wrong. Checked by running the four CI integration shards
  (`ci_shards.py --contiguous`, 4 shards) each on a fresh postgres:15.

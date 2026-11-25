# AA-1050 (S294 D6) — retire the `t10_claims` claim-extraction stage

Inventory and replay: `AA-1050.md` ("S294 carry-over inventory", `services/quality/claim_layer_replay.py`). This is the
prepared removal of the one layer whose removal touches no live path: the orchestrator ships it only after the replay
shows 0 diffs for `t10_claims` (no ledger call tied to a piece, request or tour).

## Decisions

- **Retire, not delete.** Two PRs, MIG-4: a migration-only PR (CIS 495: the route row `is_active = false` with a
  `disabled_reason`, the 336 pattern; a guard refuses the apply when a call of the stage in the last 30 days was tied
  to an object) and this code PR (the contract-smoke case goes: nothing else sends the stage). The extraction prompt
  (`claim_rules.EXTRACTION_SYSTEM`, `extraction_schema`, `build_extraction_prompt`, `parse_rows`) stays: the prompt
  manifest and `shared.prompt_version` keep v1-v3 as history, `prompt_registry_check` hashes the symbol, and deleting
  it would move the T9 writers' `prompts` fingerprint component. `SAFE_DEFAULTS["t10_claims"]` and the admin label
  stay too (the row exists; `s1_atom_writer` is the precedent).
- **Code PR first or together**: the smoke is an on-demand job kind; with the row off and the case still there, a
  smoke run would report the stage as failed.

## Changed

- `services/jobs/llm_contract_smoke_job.py`: `t10_claims` out of `PLANNED` and `CASES`; `_claims_inputs`,
  `_claims_request`, `_t10_claims` removed.
- `tests/unit/test_aa1026_contract_smoke.py`: the fixture reply and the prompt assertion go; the billed-unusable test
  runs on `t9_faq_filter`; a line pins that the stage is out of the smoke.
- `tests/unit/test_aa900_s271_effort_floor_lint.py`: the `NO_CALL_SITE` reason says retired.
- `shared/llm_client/role_config.py`: comment only (the `route_defaults` change-control sha reads the data, unchanged).
- `docs/llm/prompt-register.md`: "Not in the register" names the retirement.

## Tradeoffs

- Keeping the prompt text means `grep EXTRACTION_SYSTEM` still finds it; the live-path grep that counts is
  `stage="t10_claims"` / `Case("t10_claims"` in `services/` and `api/`: 0 after this PR.

## Should know

- **The migration moves the T9 release fingerprint.** `config_fp` hashes every route row of the `t10_` stages (on /
  off included), so after CIS 495 the active T9 releases carry a `reverify` flag (`model_route`). They keep serving;
  a re-run needs approval. Apply CIS 495 after AA-1169's human read, not during it.
- Live check before the apply (read only): `SELECT count(*) FROM shared.llm_call_log WHERE stage = 't10_claims' AND
  created_at > now() - interval '30 days' AND (content_piece_id IS NOT NULL OR angle_gate_request_id IS NOT NULL OR
  tour_id IS NOT NULL)` = 0 (the replay's `layers.t10_claims.ledger.object_calls`).

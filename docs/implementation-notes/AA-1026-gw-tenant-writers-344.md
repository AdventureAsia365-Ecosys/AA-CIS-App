# AA-1026 — tenant writers: the OpenAI API as the last fallback (migration 344, S264 lane L11)

Tech lead decision S264 (02/11, 12:30 +07): tenants cannot write at all while acc3 refuses every model (since 29/10)
and the Anthropic API has no key; add the OpenAI API as the LAST fallback of the six tenant writer stages, as
migration 327 did for the A-series writers (decision T-3).

## Decisions

- **One contract migration, 344, the 327 pattern.** For `t2_flag_fix`, `t2_generate`, `t2_itinerary_nudge`,
  `t8_angle_gen`, `t9_repair`, `t9_write`: `allowed_scopes` `{aws_only, vendor_direct}` and `gpt-6-luna-openai` as the
  last fallback. Written as a PL/pgSQL block with an explicit stage list (the 333 / 339 style), so every listed stage
  is changed, skipped with a NOTICE, or the file rolls back.
- **The Claude fallback behind the primary is dropped** (Sonnet -> Haiku on `t2_generate`, `t8_angle_gen`, `t9_write`;
  Haiku -> Sonnet on `t9_repair`). With `vendor_direct` allowed, the gateway would call the primary on the Anthropic
  API (outside AWS) before the second Claude tier on acc3 (inside): T-3 ("outside only when every AWS path is
  blocked") forbids it, 327 dropped `supply_content_writer`'s Haiku for the same reason, and the admin route PATCH
  refuses that interleaving (`_check_aws_before_vendor`), so keeping it would also make these routes uneditable.
  Every route becomes `[<Anthropic primary>, gpt-6-luna-openai]`: acc3 -> Anthropic API (when switched on) ->
  GPT-6 Luna on the OpenAI API.
- **Scope widened for these six stages only**, and the header says so (ADR 0009 decision 5 kept tenant writers
  `aws_only` until AA-820). The same `vendor_direct` scope also opens the Anthropic API warm path for these stages once
  its key is stored (ADR 0009 decision 5 approved that path for Claude writers); the tenant terms already name both
  direct APIs as a capped fallback, and the AA-Partner PR of this lane names the writing fallback explicitly.
- **No contract-smoke order gate.** The contract smoke (`llm_contract_smoke`) probes judge / validate parsers on acc2
  only; a writer stage on the OpenAI hop is proven live instead, after the apply, by one `t9_write` call through the
  gateway (script in the session folder, `l11_t9_route_live.py`): the answered row is `adhoc_aa1026` with
  `quality_signal.route_smoke = t9_write`, `connection_key openai-direct`, `status ok`. 327's writers were checked
  the same way (connection test + route read).
- **Guards**: 327 registered; `gpt-6-luna-openai` an enabled OpenAI row callable by `llm_client`, deployed ONLY outside
  AWS; `openai-direct` vendor_direct and serving `writer` (`served_roles`, 322 / 338); after the update: listed writer
  rows only, primary / shadow / role unchanged, the hop once and last, other fallbacks only dropped (never added or
  reordered), no AWS call after a vendor call, every other row byte-for-byte unchanged (JSON snapshot of the row minus
  `updated_at`), 311's vendor_direct rule with 327's and 344's writers as the only additions, no writer on an
  acc2-only chat model.

## Changed

- `api/migrations/344_llm_tenant_writers_openai_last.sql` (new, contract).
- `tests/unit/snapshots/aa1026_routes_after_344.json`: the 37 Dev routes after 344 (from `after_339`).
- `tests/integration/test_aa1026_migration_344.py`: expected routes, re-apply no-op, mid-route hop moved last, an
  outside-only fallback kept, skips (non-writer primary, missing row), other rows unchanged, and mutation tests for
  each guard.
- `tests/unit/test_aa1026_tenant_writer_openai_routes.py`: the six routes walked through `LLMClient.generate` with every
  provider failing, Anthropic API off and on: acc3 first, then the Anthropic API (when on), then openai-direct last;
  before 344 they never left AWS; every other stage makes the calls it made after 339.
- Not changed: `SAFE_DEFAULTS` (they serve a process that cannot read the DB; legacy chain, aws_only, as 327 left them);
  `test_aa1002_migration_304.py` LATER (344 only updates `llm_role_config` rows, which that test does not reset).

## Tradeoffs

- **Losing the in-AWS degrade** (a throttled Sonnet on acc3 used to fall to Haiku on acc3) against keeping "AWS first,
  outside last" exact and the routes editable in the admin. With acc3 refusing everything the degrade does nothing
  today; when acc3 is back, a refused primary now goes to the Anthropic API / OpenAI API instead.
- **Writer family**: the last rung is GPT (OpenAI), skipping the ADR 0009 decision 12 ladder for this rung (as 327).
  While acc3 refuses, the T-judges answer on Nova Lite (acc2, 333) before their own OpenAI hop, so a GPT-written piece
  is normally scored by another maker; the call-time same-family check runs only in eval runners.
- **Cost**: GPT-6 Luna on the OpenAI API is priced 0.10 / 0.50 USD per 1M tokens in the catalog (migration 170),
  below Sonnet 5.5 (2 / 10): well under a cent per piece, inside the openai-direct 10 USD monthly hard stop (327) and
  the tenant AI budget (321). Quality is the open question (no A/B of GPT against Claude on T-stages yet); the tenant
  reviews every piece before publishing.

## Should know

- Number 344 = CIS main max + 1; Dev is at 338 with 336 pending, so the apply is out of order (DB Migrate
  `--allow-out-of-order`, as 339-343).
- After apply: `POST /admin/writing-memory/releases/reverify-scan` (routes of `t2_generate` / `t9_write` changed outside
  the admin PATCH) and the root route page (`tools/docs/gen_llm_routes.py`).
- Rollback: a new forward migration setting the six rows back to `{aws_only}` and their old fallbacks (listed in the
  migration header), or the admin route PATCH per stage with a break-glass reason.

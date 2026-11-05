# AA-1042 — Every Claude model of the Anthropic API key in the gateway catalog (S269 follow-up, lane l7)

Tech lead request (S269, 05/11, after the sponsored Anthropic API key went live on connection `anthropic-direct`):
"Fable 5.1, Fable 5, Opus 5, Opus 4.8, 4.7, 4.6, 4.5, Sonnet 4.6, Haiku 4.5: why are they not in the catalog as
fallbacks?". The key can use 13 models (checked with `count_tokens`, $0); only Haiku 5.5, Sonnet 5, Sonnet 5.5 and
Opus 5.5 had an `anthropic-direct` deployment (CIS 306).

One PR: CIS migration 376 (contract, migration-only; no code needs it). Tests: `tests/integration/test_aa1042_migration_376.py`
(real Postgres) and `tests/unit/test_aa1042_anthropic_catalog_376.py` (chain rules + route control preview), plus the
existing replay tests taught about 376's rows.

## Decisions

- **Five new catalog rows reached only by the Anthropic API** (`fable-5-1`, `fable-5`, `opus-5`, `opus-4-8`,
  `opus-4-7`): `provider` `anthropic`, `api_style` `anthropic_messages`, no Bedrock profile, `wire_model` = the API
  model id. The other columns follow the existing Anthropic rows (label "Claude …", family / vendor `anthropic`,
  `callable_via` `{llm_client}`, `supports_temperature` false, `max_output_tokens` 128000, `capabilities` `{}`).
  `provider` / `api_style` differ on purpose: `bedrock` / `converse` would send the no-registry path (DB down) to a
  Bedrock call with no profile; `anthropic_messages` makes it refuse the model by name, and `_callable` accepts the
  style on an Anthropic connection.
- **Prices only from the claude-api skill model table** (cached 2026-10-06): Fable 5.1 / 5 10 / 50, Opus 5 / 4.8 / 4.7
  5 / 25 USD per 1M tokens; cache read 0.1x input (Fable 5.1 0.025x = 0.25), cache write 1.25x (5-minute TTL; the
  gateway prices 1-hour writes at 2x itself). Opus 4.5 is not priced in the skill: its catalog row keeps the 367 price
  (5 / 25, the AWS price list, equal to the Opus 4.x list price), no override.
- **Opus 4.6 / 4.5: the 367 candidate rows switched on** (only while still the untouched 367 rows), acc3 deployments
  left disabled (no evidence acc3 serves them; the invoker role needs a Terraform change first). The API is their only
  enabled path.
- **Nine deployments at priority 100**, behind every AWS deployment of the model (S267 "AWS preferred, not only", the
  306 convention), `access_state` unknown (the probe fills it). Wire ids named, not derived: 306's derivation would give
  Opus 4.6 `claude-opus-4-6-v1` (its acc3 profile ends in a bare `-v1`). Opus 4.5's deployment states
  `request_defaults` `{"effort": "high"}`: the adapter's family table does not know it (an unknown family sends no
  effort and logs a warning once); high is its default. Sonnet 4.6 / Haiku 4.5 get cache read / write overrides
  (0.30 / 3.75, 0.10 / 1.25): their 366 catalog rows (copied from the legacy rows) have no cache price.
- **The chains: fit, do not extend.** The gateway makes at most six provider attempts per request
  (`errors.MAX_CHAIN_ATTEMPTS`, ADR 0009 d5) and the chain rule counts every deployment of every chain model in the
  stage's scopes, on or off. A second deployment for Sonnet 4.6 and Haiku 4.5 puts 20 of the 366 chains at 7 (proved:
  the 366 routes on the 376 registry break exactly `attempt_cap` on those 20 and nothing else). Each keeps its order
  and drops its lowest Claude rung; the other-maker last rung stays (the OpenAI API at the end of a writer, ADR 0009
  S267; acc2 at the end of a validate stage). Exception: `tp_compose` keeps both 4.x rows and drops the OpenAI API
  rung, because TripPlanner's own gateway calls Bedrock only and never runs that rung.
- **No new model on a chain.** Sonnet 5: acc3 refuses it like every 5.x and its only working path is the same key as
  Sonnet 5.5, so its two attempts would push out Sonnet 4.6 (the only Sonnet that answers on AWS today) or the
  other-maker rung, for no failure it covers that Sonnet 4.6 does not. Opus: no stage has an Opus primary; the degrade
  order (Opus 5.5 > 5 > 4.8 > 4.7 > 4.6 > 4.5) is written in the catalog notes for the stage that adopts Opus. Fable:
  Anthropic's top tier for long-horizon reasoning, 10 / 50 USD, above every tier a stage uses, API only: no family
  tier to degrade into. All of them are selectable in the admin route control.
- **One `external` route config version written by the migration** (snapshot + diff, same shape and order as
  `route_versions.diff`; the integration test checks the app finds nothing left to record), so the ledger rows after
  the apply carry the version that routes them instead of the S269 preset's version 4.

## Changed

- `api/migrations/376_llm_anthropic_api_catalog_fallbacks.sql` (new).
- `tests/unit/snapshots/aa1042_routes_after_376.json` (new): the 366 snapshot with the 20 new chains.
- Replay tests that re-run older migrations on the post-376 state: `test_aa1002_migration_304.py` (376's rows leave
  with `_undo`, 376 re-applied in `LATER`, 376's Opus rows set aside before 367's re-run, the "today's path" of an
  API-only row), `test_aa1002_migration_306.py` (376's rows are not 306's), `test_aa1026_migration_367.py` (sets 376's
  Opus switch aside, re-applies 376 at teardown), `test_aa1042_migration_370.py` (the baseline predates 376's models),
  `test_aa1002_deployment_wire_drift.py` (the Anthropic rule takes a bare `-v1` suffix and an API-only row's
  `wire_model`; a new check that every API-only row names a Claude API id), and the tests that pin the replay's
  end state of one route (`test_aa784_migration_286.py`, `test_aa803_migration_259.py`,
  `test_aa835_migration_278.py`, `test_aa835_migration_282.py`): post-376 chains.
- Number: the lane hint was 380; the migration preflight takes main max + 1 only, and CIS #1315 (AA-1035) took 375
  while this lane ran, so 376.

## Tradeoffs

- Fewer distinct models per chain (Haiku 4.5 leaves the large writers, Sonnet 4.6 the small writers and two validate
  stages, Haiku 4.5 the atomize / source-facts stages) against the same number of Claude attempts on more independent
  paths: a 4.x model is now tried on acc3 AND on the API, so a route survives acc3 being blocked as an account (the
  Error 002 risk). The alternative (raise the six-attempt cap) changes an ADR 0009 decision and the per-request
  latency bound: left to the tech lead (followup).
- Priority 100 behind acc3 follows the brief (AWS preferred) while route version 4 put the four 5.x Claude rows
  API-first. Re-applying the `prefer_anthropic_api` preset moves the four new two-deployment rows (Sonnet 4.6, Haiku
  4.5, Opus 4.6 / 4.5) API-first too (unit test), and it still validates.
- A revert to version 3 or 4 (the 366 chains) is refused after 376 by the attempt cap (unit test). The way back to
  AWS-first is the "Prefer AWS" preset, which validates.

## Should know

- The daily probe calls each new enabled deployment (60 tokens in, 32 out): about 0.011 USD a day for the nine
  (Fable 5.1 / 5 about 0.002 each, each Opus about 0.001), about 0.02 USD a day for the whole post-376 plan.
- **Probe cap (review fix, S269).** 376 brings the enabled deployments on enabled connections to 33 (test
  registry: acc2 6 incl. 2 embedding, acc3 12, anthropic-direct 13, openai-direct 2), over the probe's old
  `MAX_PROBES` of 30: walked in connection-name order, the last anthropic-direct row and both openai-direct rows (the
  GPT-6 Luna last-resort writer rung) would have been skipped as `over_limit` with a stale `access_state`. The
  companion code PR (`fix/aa-1042-probe-cap`, notes `AA-1042-probe-cap.md`) raises it to 50 and merges first;
  this branch is stacked on it. `test_the_daily_probe_plans_every_enabled_deployment_after_376` builds the probe
  plan on this registry and asserts nothing is skipped. Rejected: seeding the five API-only Opus / Fable rows
  disabled (the tech lead asked for them as usable fallbacks, selectable in the route control).
- Guards RAISE (rolling the file back) when an admin route that 376 does not change would be pushed over the six
  attempts by the new deployments: change that route in the admin route control first.
- The admin "via" label (`admin_llm_ops.model_via`) shows "Bedrock" for a provider `anthropic` row (no account): a
  one-line follow-up ("Anthropic API").
- Live checks after the apply: connection test of each new deployment (`max_tokens` 64), the route control preview
  (every stage served), `POST /admin/writing-memory/releases/reverify-scan`, the bot/db-schema-reference PR.

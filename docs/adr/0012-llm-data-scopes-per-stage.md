# LLM data scopes per stage: which ways in a stage's content may use is data, set by migration (AA-1002 S5)

- **Status:** Accepted (30/10/2026, S260).
- **Implements:** the S235 decision "Bedrock first; the OpenAI platform API only as the last resort of a stage
  route" (AA-Ecosys `docs/implementation-notes/S235-bedrock-only.md`), as data. Builds on ADR 0005 (catalog), ADR 0006
  (stage routes) and the S258 gateway design (AA-Ecosys `docs/audits/2026-10-29-S258-aws-org-blueprint/`, slice S5).
- **Ecosystem ADR:** root ADR 0009 "AI platform and governance" is reserved for AA-804 and not written yet; its
  data-class egress policy must cite this ADR (or supersede it). It is an app ADR today because only CIS reads the
  scopes: TripPlanner keeps its own gateway until slice S8.

## Context

Since slice S1 every way into a model is a connection row with a `data_scope`: `aws_only` (a Bedrock account: the
request stays in AWS) or `vendor_direct` (a vendor API: the request leaves AWS). Which stage may use which scope was
still code: the admin's `_OPENAI_LAST_RESORT_ONLY` (an OpenAI platform API model only as a route's last fallback) and
`vendor_route.deployment_plan`'s S3 rule (a `vendor_direct` deployment only when it is a model's first enabled row,
never a fallback behind acc3, "until S5"). With the Anthropic API deployments migration 306 seeded behind acc3 for
every Anthropic model (tenant writers included), the code rule was the only thing keeping tenant content inside AWS
if the Anthropic connection were switched on. Seven Dev stages (the judge routes and `a3_place_dedupe`) already end on
`gpt-6-luna-openai` since S235, three of them tenant-facing (T2, T7, T10 judges).

## Decision

1. `shared.llm_role_config.allowed_scopes text[]` (migration 311): the data scopes a stage's content may be sent
   through, `{aws_only}` or `{aws_only, vendor_direct}`; CHECKed, default `{aws_only}` for a new stage.
2. Backfilled from the routes as they are, so no call changes: both scopes for a stage whose route names a model
   whose every deployment is outside AWS (today the S235 last resort `gpt-6-luna-openai`), `{aws_only}` otherwise.
3. The gateway calls a deployment only when its connection's scope is in the stage's scopes (the deployment plan, the
   S2 vendor path, the OpenAI adapter gate). An `aws_only` stage never leaves AWS whatever the priorities; a stage with
   `vendor_direct` may fall back to a vendor API behind acc3.
4. NULL (column not migrated, a stage with no row, an `adhoc_*` stage or a connection test) is "no stage policy": the
   pre-S5 rules stay (the S3 first-row rule, the S235 catalog gate). The gateway reads the column with a fallback, so
   code and migration ship in either order.
5. The scope is migration-only: `cis_app` has INSERT / UPDATE on every column except `allowed_scopes`, and the admin
   shows it read-only. Opening a stage to `vendor_direct` is a reviewed migration (tech lead decision; for tenant
   writers and TripPlanner a DPA / terms review first, S258 table 5 and 11).
6. The admin keeps the S235 position rule in scope terms: a model reached only outside AWS may be only the last
   fallback of a route, never the primary or the shadow; and a route model the stage's scopes never let the gateway
   call is refused (422), so the UI never claims a route that does not run.

## Consequences

- (+) Policy is visible per stage in the admin (Route editor › Data) and in `db-schema-reference.md`; a change has a
  migration, a review and an audit trail; the code constants are gone.
- (+) Switching `anthropic-direct` on is now safe for tenant writers: their scope is `aws_only`.
- (−) A new stage seeded with an OpenAI-API last resort must also set `allowed_scopes` to both scopes in its seed
  migration, or the default `{aws_only}` silently skips that fallback (`llm_deployment_skip` reason "data scope").
- (−) With both scopes, a judge / validate stage whose route names an Anthropic model would use the Anthropic API as
  a fallback once that connection is on. No such route exists today (judges are never Anthropic).

## Alternatives considered

- **Scopes on the catalog model** (per model, not per stage): the same model serves tenant and admin stages; the
  data class belongs to the content, i.e. the stage.
- **Keep the S3 positional rule even for `vendor_direct` stages**: then the scope would add nothing over the code
  rule, and an acc3 outage could never fall back to the Anthropic API for any stage.
- **An admin toggle for the scope**: makes the egress decision a click without review; rejected (DM-11.5 style:
  data-leaving-AWS changes are reviewed).

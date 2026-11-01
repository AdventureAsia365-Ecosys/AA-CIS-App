# AA-1026 — lane gw-governance (S263): slice D7, a route change needs evidence

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.D (D7). Tech lead T-19 (accepted S263: the rule,
with break glass: reason required, admin only, alerted). Root ADR 0009 decision 12. Same PR as AA-1024 A2 / A4 / A6
(`AA-1024-gw-governance.md`).

## Decisions

- **Scope**: writer and judge stages only (`route_evidence.EVIDENCE_ROLES`). Validate stages may use any enabled model
  (CIS ADR 0008 decision 2) and embed stages have no judge; the plan's Done-when names writer / judge.
- **What counts as a change**: the primary model (model PATCH, `model_id` differs) or the fallback chain (route PATCH,
  `fallback_model_ids` differs, order included). A shadow-only change needs nothing (the shadow never serves a reply;
  it is how evidence is gathered), nor does an account-route-only save or the kill switch.
- **Eval run validity** (`eval_run_problem`, pure): the `shared.prompt_eval_runs` row exists and is not INVALID
  (`details.invalid`, read as `(details -> 'invalid') = 'true'::jsonb` so a non-boolean value never breaks the read).
  A writing-memory run: finished, `verdict = 'pass'` and `stop_reason = 'completed'` (`baseline` / `informative`, or a
  run stopped by `error` / `budget_cap` / `futility` / `hard_stop`, shows nothing about the change; shadow runs that
  pass are `pass` too). A legacy regression row (`s1_old` / `s1_from_atom`, written once at the end, verdict NULL):
  verdict not `fail` / `invalid`, `regression_detected` false. Review fix: `_detect_regression` forces
  `regression_detected = false` on an INVALID run (degraded hard detector, or the A4 `same_family_tours`), so the flag
  alone let an INVALID legacy run, even one the family rule declared INVALID, pass as evidence. No freshness window and no stage match: the run id is stored in the audit
  row for the reviewer. An id that is given must be valid even next to a break-glass reason.
- **Break glass**: `break_glass_reason`, at least 20 characters (whitespace collapsed), at most 500. The bell
  (`shared.notifications`, event `platform.llm_route.break_glass`, roles `admin`, platform tenant) is written in the
  same transaction as the UPDATE and its audit row (`role_config.set_stage_config` / `set_stage_route` take
  `evidence`). "Admin only" is the existing route policy (`ai.admin` on both PATCH routes).
- **Errors**: 422 with the reason (no evidence, short reason, not a UUID, unknown / unfinished / failed run); 503 when
  the run table cannot be read (never waved through).
- **UI**: `ChangeEvidenceFields` (kit `Field` / `Input` / `Textarea`) inside both confirm dialogs, shown only when the
  change needs evidence; the confirm stays open with an inline error until an id or a long enough reason is given.
  The save's `warnings[]` (AA-1024 A6) show as an info toast.

## Changed

- `acp_shared.audit_log.details` gains an optional `evidence` object next to `before` / `after` (no schema change).
- Existing route tests that save a new chain now pass a break-glass reason.

## Tradeoffs

- The eval run is not tied to the stage or the new model (the table has no such column): checking it would need a
  migration; the audit row keeps the id so a reviewer can follow it.
- A break glass alerts by bell only (no e-mail): the plan's Done-when asks for a bell; the ops e-mail templates are a
  separate allowlist.

## Should know

- Live check needs no LLM: PATCH a judge stage's fallbacks without evidence → 422; with a break-glass reason → 200,
  a bell, and an audit row with `details.evidence` (then PATCH the route back with another reason).
- Until an eval can run (acc3 Error 002), every real writer / judge route change on Dev goes through break glass.

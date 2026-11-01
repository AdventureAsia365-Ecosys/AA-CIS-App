# AA-642 lane L2 (S263) — non-LLM paid providers: Batch kill switch, provider switches, alerts, probe

Scope: slices D4, B1, B4 and B2 of the S263 gateway gap plan (root
`docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.B, §1.D, §2). No migration, no paid call; every provider
is mocked in the tests.

## Decisions

### D4 — Bedrock Batch kill switch

- **Env switch, default closed.** `BATCH_SUBMIT_SWITCH` (on / true / 1 / yes opens it; unset or anything else keeps it
  closed). No Terraform change is needed for the switch to be closed, and opening it is a deliberate task-definition
  edit once the T-BATCH trigger is met (batch spend above 100 USD a month, or a whole-catalogue rerun) and the
  pre-submit guard is built. A DB row would have been editable from the admin; the plan (T-7, TL-13) wants this path
  shut until that guard exists, so an env switch is the better fit.
- **Two places check it.** `POST /admin/s1-batch/submit` answers 503 with the reason before anything is materialized,
  uploaded or submitted (one hunk in `admin_pipeline.py`). `bedrock_batch.submit_batch_job` refuses too
  (`BatchUnavailable`), after the account check, so a future caller other than the API cannot reach
  `CreateModelInvocationJob` either. Polling / ingesting a job submitted before stays open (no new spend).

## Changed

- `shared/llm_client/bedrock_batch.py`: `BATCH_SWITCH_ENV`, `BATCH_CLOSED_REASON`, `batch_submit_closed_reason()`,
  the check in `submit_batch_job`.
- `api/routers/admin_pipeline.py`: the 503 at the top of `submit_s1_batch_endpoint` (nothing else in that file; lane
  L1's A2 edits it next).

## Tradeoffs

- 503 (not 403 / 409): the service is deliberately unavailable, and the caller should not retry until an operator
  opens it; the reason says how.

## Should know

- The T-BATCH trigger row for the root backlog / `plan.json` goes with lane L7's root PR (plan §2), not here.
- ⚠️ `BATCH_SUBMIT_SWITCH` is an underscore env name: set it by hand in the task definition only when opening Batch.

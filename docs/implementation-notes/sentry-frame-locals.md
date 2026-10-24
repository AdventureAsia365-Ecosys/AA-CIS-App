# Sentry: no frame locals, credentials and e-mails redacted (AA-941 security review, S249)

Finding (reproduced in S249): any unhandled 500 on a staff route for a Cognito-verified request sent Sentry an event
whose frame locals held the full staff access token (`StaffBearerMiddleware.__call__` `scope` / `headers` /
`state`; Starlette, request-id and sentry-context frames `scope` / `request`) and request body PII (FastAPI `body` /
`json_body` / `values`, route payloads such as an owner e-mail). Cause: `include_local_variables` defaults to True and
`scrub_frame_locals` only acted on erasure jobs (AA-839).

## Decisions
- `include_local_variables=False` in `init_sentry` (api and worker), and `before_send` drops `vars` from every frame
  of every event (top-level `stacktrace`, `exception`, `threads`). Belt and braces: an integration or a later init that
  turns locals back on still sends none. File / line / function / context lines stay.
- Value-based redaction in the free text that remains: exception `value`, `message`, `logentry`, `extra` (log-line
  fields), breadcrumbs (`message`, `data`, both in `before_breadcrumb` and `before_send`), request `url` /
  `query_string`. Patterns: JWT-shaped strings (`eyJ….….…`), `Bearer <x>`, e-mail addresses, plus the AA-712
  key-like query parameters.
- Redaction runs after the AA-871 log-line shaping, so fields copied into `extra` are covered. If redaction raises,
  the event is dropped (fail closed) rather than sent unredacted.
- `send_default_pii=False` and `max_request_body_size="never"` were already set (AA-870); asserted by a test now.

## Changed
- AA-839 chose a targeted scrub ("other kinds keep their debugging value"). That is superseded: no event keeps
  locals. `SCRUB_LOCALS_KINDS` / `SCRUB_LOCALS_MODULES` / `_tag` removed; `scrub_frame_locals` is now unconditional.
  The AA-839 unit test now asserts that a non-erasure event loses its locals too. CONTEXT.md and the
  `services/jobs/erasure_job.py` docstring updated.

## Tradeoffs
- Losing locals costs some debugging depth on Sentry. Weighed against: tokens and PII in a third-party store, and a
  denylist scrubber can never know every local name that may hold a credential or a body. Structured log lines
  (`extra`, with `request_id`) still give the context a responder needs.
- E-mail redaction also hits harmless addresses (e.g. a staff address in a log message). Accepted: an e-mail in a
  Sentry event is never needed to triage.
- Redacting the exception `value` can change the text shown in Sentry for messages that embed an e-mail/token;
  grouping uses the stack trace, so issues do not split.

## Should know
- Tests: `tests/unit/test_sentry.py` (init options, every-frame drop, text redaction, breadcrumb redaction, and the
  S249 repro end to end: real `init_sentry` with the Starlette/FastAPI integrations + real `StaffBearerMiddleware`,
  Cognito-verified POST whose route raises; asserts neither the token, its signature/payload nor the body e-mail is
  in the captured event, and the stack trace is still there). The e2e test fails on the old `sentry_init.py`.
- The misleading docstrings ("never logged" / "Sentry drops request bodies") are in `api/services/crm_console.py`,
  which is only on the AA-941 branch, not on main — fix them on that branch.
- AA-Booking and AA-Partner have their own Sentry init; see their own `fix/sentry-frame-locals` branches.

# AA-816 (S242) — staff invitation e-mail

Ask (Nghiệp, S242): when an owner adds a staff account, the person should be told. Cognito's own invite
(`MessageAction` not SUPPRESS) mails a temporary password, against the passwordless design (C6 always
sends SUPPRESS), so CIS sends its own branded invitation through SES.

## Decisions

- **Sender = the AA-828 helper** (`shared/aws_client/ses_mailer.py`): SES v2 `send_email`, From
  `SES_FROM_ADDRESS` (= Terraform `var.ses_from_address`, `no-reply@lumiguides.it.com`), configuration set
  `SES_CONFIGURATION_SET` (`aa-email-dev-transactional`). Added `sender_config()` (sender + config set +
  region, no `ALERT_EMAIL_TO`): the operator mailbox is irrelevant for an invite.
- **Async job `staff_invite_email`** (unprefixed: CIS kinds in `shared.job` keep plain names, ADR 0003a
  decision 3). Enqueued in the SAME transaction as the directory change (create: with the INSERT; resend:
  with the rate-limit check), so a rolled-back create never mails and a committed one always has its job.
  The HTTP request never talks to SES.
- **Idempotency key** `staff_invite_email:<staff_id>:<seq>`, `seq` = 1 + the highest seq already used for
  that staff id (parsed from the keys, not a count, so the 30-day retention deleting old jobs can never make
  a new key collide with a surviving one). Two concurrent resends serialize on the target row lock
  (`SELECT … FOR UPDATE`) and, failing that, collide on the key: one job.
- **Payload holds ids only** (`staff_id`, `requested_by`, `reason`, `seq`); the handler re-reads the row at
  send time (email, display name, current roles, active flag) and the actor's display name. No address in
  the payload or result, so the job-runner API (open to every staff role) needs no redactor.
- **Audit rows (acp_shared.audit_log, resource_type admin_user)**: `staff.invite_requested` (request txn),
  `staff.invite_sent` (worker, after SES accepted; details: job id, SES message id), `staff.invite_not_sent`
  (worker skip — row disabled / gone / no address — or the job's terminal failure via `on_terminal`).
- **"Last invited" without a migration**: the staff list/detail select the latest of each of the three audit
  actions per row (correlated `max(created_at)` subqueries; audit_log is ~1.3k rows, index on
  `(action, created_at DESC)`), and derive `invite_status` = sent / queued / not_sent. The audit log is never
  pruned (unlike `shared.job`, 30-day retention), so the date survives.
- **Rate limit per target** from the same `staff.invite_requested` rows (the create's invite counts): at
  most 1 per 10 minutes and 5 per rolling 24 h; 429 with a sentence saying when, plus `Retry-After`.
  Times come from the DB (`now()` in the same query), so app clock skew does not matter.
- **Resend preconditions**: staff.write (owner on a Cognito session, route policy) + the service's owner
  re-read; target active (409 otherwise), has an email (409), not break-glass (403); SES sender configured
  (503 otherwise, nothing queued).
- **Create**: `send_invite` (body, default true). Invite only when a NEW row is inserted; a re-POST of the
  same create (idempotent retry) does not queue another (use Resend). Not configured -> the account is
  still created, the response says `invite: "not_configured"`.
- **Handler send-once guard**: before sending, the handler looks for a `staff.invite_sent` row with this job
  id; found -> done without sending (covers a worker that wrote the audit row but died before marking the
  job succeeded).
- **Recipient guard**: the handler skips (no send, `staff.invite_not_sent` reason `not_a_staff_address`) any
  address not `@adventure.asia`, even if a row somehow holds one (defence in depth; the DB CHECK already
  enforces it).
- **Create invite waits for Cognito** (review round): the create's job gets `run_after = now + 120 s`
  (`CREATE_DELAY_S`) so the Cognito steps that run after the commit have finished, and the handler raises a
  retryable `CognitoNotReady` while the account's latest Cognito audit row (`staff.cognito_pending` vs
  `staff.cognito_synced`, `SQL_COGNITO_STATE`) is pending. DB-only, no AdminGetUser. With the runner backoff
  (30/60/120/240 s) the 5 attempts span about 10 minutes from the create; then the job fails, `on_terminal`
  writes `staff.invite_not_sent`, and the owner resends after "Retry Cognito". A resend is not delayed.
- **Request row only for a new job** (review round): `enqueue_invite` writes `staff.invite_requested` only
  when `queue.enqueue` created the job. A duplicate key (stale seq) returns the existing job id and writes
  nothing, so it neither counts against the rate limit nor leaves `invite_status` stuck on "queued".

## Changed

- `shared/aws_client/ses_mailer.py`: `SenderConfig` + `sender_config()`.
- New `api/services/staff_invite.py` (render, enqueue, rate limit, status), new
  `services/jobs/staff_invite_email_job.py` (handler + on_terminal), registered in `services/jobs/__init__.py`.
- `api/services/staff_directory.py`: `create_staff(..., send_invite=False)` (route passes the body flag),
  `resend_invite(...)`; `WriteResult.invite`; `StaffDirectoryError.headers`.
- `api/routers/admin_platform.py`: `send_invite` on the create body, `invite` in write responses,
  `POST /admin/platform/staff/{staff_id}/invite`, invite columns on staff rows.
- `api/core/staff_authz.py` POLICY + the route-policy snapshot.
- Frontend: "Send invitation email" checkbox (default on) in Add staff, banner says whether an invite was
  queued, "Send invite" / "Resend invite" button in the drawer (active accounts with an email), "Last invited"
  field in the drawer + list column (table min width 860 -> 960).
- Tests: new `tests/unit/test_aa816_staff_invite.py` (37 after the review round) and
  `tests/integration/test_aa816_staff_invite.py` (2,
  run against the local Postgres 16 cluster); `test_aa883_admin_platform.py` expects the four new row keys.

## Tradeoffs

- **Invite vs a pending Cognito half**: first version accepted that the e-mail could arrive before sign-in
  works. Review round: delay + audit-log check (Decisions). Residual gaps: a later `change_roles` whose
  Cognito half failed also writes `staff.cognito_pending`, so a resend for an account that can in fact sign in
  waits until "Retry Cognito" (safe direction); and `_audit_after` is best effort, so a lost pending row means
  the check cannot see the failure (the 120 s delay still covers the normal case).
- **At-least-once**: a worker that dies after SES accepted the message but before the audit row can resend
  on the reaper's retry. Accepted (same as AA-828); the send-once guard closes the later window.
- **Audit-derived status** costs three correlated subqueries per staff row instead of a column. Fine at this
  size; a column would need the migration copy/apply flow.

## Should know

- SES sandbox: if the account is still in the SES sandbox, only verified recipients receive mail
  (MessageRejected for others -> job fails as `not_sent`). Check `aws sesv2 get-account`
  (`ProductionAccessEnabled`) before announcing the feature.
- The template links only to `https://aa-cis.lumiguides.it.com/login` (`ADMIN_BASE_URL` override) and the
  public logo `/assets/adventure-asia-logo.png`; no code, token or deep link with a secret.
- No AWS, SES or Cognito call was made while building this; tests stub the SES client. (Regenerating the
  route-policy snapshot with `python -m tests.unit.test_aa882_route_policy` imports the app, whose Sentry init
  tries `GetSecretValue`; with the dummy `AWS_ACCESS_KEY_ID=x` it was refused, nothing was read.)
- The BFF (`app/api/admin/[...path]`) forwards status + body but not `Retry-After`; the 429 detail sentence
  already says how long to wait, which is what the UI shows.
- Template wording (review round): the code step and a line "Only enter the code on
  staff-auth.lumiguides.it.com. Never share it: Adventure Asia will never ask you for it." name the Cognito
  custom domain (`SIGN_IN_HOST`, same value as `frontend/lib/cognito.ts` DEFAULT_WORKFORCE_DOMAIN; text, not a
  link). Unknown actor -> "contact the Adventure Asia platform owner" (no "contact An … or the …").
  `clean_name` also drops zero-width and bidi control characters (U+200B-U+200F, U+202A-U+202E,
  U+2060-U+2069, U+FEFF); `require_display_name` (C6 input validation) is unchanged.
- SES errors are re-raised `from None`: `worker.py` logs failures with `exc_info=True`, and a chained
  `MailError` would put SES's message (which can quote the address) in CloudWatch / Sentry.
- **Deploy order for the first real invite (Thu)**: the e-mail's only link is on aa-cis.lumiguides.it.com,
  which Chrome Safe Browsing flagged (S242). Create Thu with the checkbox unticked, or wait until Search
  Console shows the review cleared, then use "Send invite"; also confirm `ProductionAccessEnabled` first.
- Role wording in the e-mail lives in `staff_invite.CIS_ROLE_TEXT` / `BOOKING_ROLE_TEXT`; the Booking role
  scopes are named, not described, because Booking owns what they mean.

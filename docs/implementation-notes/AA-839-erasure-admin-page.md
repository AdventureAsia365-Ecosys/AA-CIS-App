# AA-839 — Admin page: Jobs › Data erasure

Scope of this PR: the operator UI for the `data_erasure` job kind shipped in the erasure-job slice
(`AA-839-erasure-job.md`). No backend change.

## Decisions
- New route `/admin/data-erasure` under the **Jobs** nav group (next to `/admin/jobs`), admin only:
  `PAGE_RULES`, `API_READ_RULES` and `API_WRITE_RULES` (`job-runner/erasure`) in `frontend/lib/rbac.ts`.
  The backend re-checks (`staff_policy`, `tenant.read` / `tenant.write`).
- Traveller subject: the page computes `sha256(trim(lower(address)))` in the browser (Web Crypto) and
  clears the input at once. Only the hash is sent. The address is never put in the URL, a query key or
  storage. The hash is kept in memory only so the verification DRY RUN after an apply can be started
  from the same page session; after a reload the operator types the address again.
- Tenant subject: picker of offboarded tenants from `GET /admin/tenants?include_inactive=true`
  (`status == "offboarded"`, `cancelled_at`), with a pasted-uuid fallback.
- The apply body is built only from the DRY RUN job (id, fingerprint, subject, tenant id or hash held by
  the job row) plus the operator's snapshot id and approver. The typed fingerprint prefix (8 chars) is a
  confirmation only, never sent.
- The selected DRY RUN and apply ids live in the URL (`?job=…&apply=…`), so a reload or a deploy between
  the DRY RUN and the apply keeps the page.
- Polling: one job every 3 s while queued/running; history every 10 s only while a row is active.

## Changed
- Nothing outside the frontend except the UI smoke list (`tests/e2e/smoke/ui-smoke.spec.ts`, a
  `@staff` row with `viewerRefused`).

## Tradeoffs
- The page cannot tell a Cognito session from another one (the identity cookies hold role and name only).
  Since AA-816 S2 every staff session is Cognito; if not, the API's 403 reason is shown as-is.
- Client-side gate (`applyGate`) mirrors the job's refusals (succeeded, unblocked, unused, fingerprint,
  age ≤ max, tenant offboarded, apply flag) so the operator sees why before clicking; the job re-checks
  everything inside its transaction, so the client gate is a convenience, not a control.
- No FE unit-test runner exists in this repo (no vitest/jest); the pure helpers sit in
  `frontend/lib/dataErasure.ts` so a later test can import them.

## Should know
- The apply panel hides as soon as an apply id is in view, so a double click cannot enqueue a second
  apply (the job would refuse it anyway: single-use DRY RUN).
- An apply job that failed committed nothing; its DRY RUN is used up, the page says to start again.
- The audit row of an apply is `tenant.erased` or `dsar.cis_erased` (counts only), visible in
  Platform › Audit log.

## Review round (S248)
- `ApplyPanel` is mounted with `key={dryJob.id}`: every DRY RUN starts with an empty form. The backend
  fingerprint hashes the subject kind and the (table, action, rows) list only, so two tenants with the same
  counts share a fingerprint; a form typed for one must never carry over to the other.
- The danger notice and the confirmation sentence name the subject (`subjectLabel`): tenant name, slug and
  id, or `traveller hash abcd1234…`.
- `applyGate` fails closed: a job of another kind, a job without a 64-hex fingerprint (also when `result`
  is null) or without a subject keeps the panel locked. A job of another kind opened with `?job=` shows
  "This job is not a data erasure" instead of the plan.
- `applyGate` takes the recent erasure jobs: a queued, running or succeeded apply whose
  `payload.dry_run_job_id` is this DRY RUN locks the panel (`used_by_apply` is written only when the apply
  starts). Limit: the history holds the 20 newest erasure jobs; the job's atomic claim stays the control.
- A rejected `fetch` no longer says "nothing was sent": the request may have reached the BFF or API. The
  page says so and refreshes the history.
- Backend follow-up (separate PR): put the tenant id or the hash in the fingerprint, or require
  `approved_tenant_id`, so an approval names the subject and not only the counts.

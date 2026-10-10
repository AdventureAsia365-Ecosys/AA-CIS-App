# AA-756 — Jev credit top-ups in Settings + one exhausted alert under concurrency (S224)

Sub-task of AA-756 (Nghiệp approved, 10/10/2026). Decision: Jev (TypeSafe) has no balance API and
Nghiệp does not top up Jev himself, so the only automatic alert fires when credit is **exhausted**
(402 billing_error, AA-720) — there is **no "running low" alert**. Settings records the manual
top-ups so admins can see a rough (estimated) balance.

## What shipped

### 1. Migration `209_jev_credit_topup.sql`
`shared.jev_credit_topup` (id bigserial PK, topped_up_on date NOT NULL, amount_usd numeric(10,2)
NOT NULL CHECK > 0, note text, created_by text, created_at timestamptz default now()), plus a
`(topped_up_on DESC)` index. Self-registers in `shared.schema_versions`. **Claude applies it** — the
worker never touches the DB. With the table missing the GET returns a clean empty state (the FE
treats a 404/501 as "no data yet").

### 2. API — `api/routers/admin_decisions.py`
- `GET /admin/decisions/jev-credit` → `{topups[], total_topped_up_usd, first_topup_on,
  spent_since_first_topup_usd, estimated_left_usd, last_exhausted_alert_at}`.
  - `total = sum(amount_usd)`; `first_topup_on` = the earliest top-up date (rows come back newest
    first, so it is the last row).
  - `spent` = `sum(cost_usd)` of `shared.llm_call_log` where `provider = 'typesafe'` and
    `created_at >= first_topup_on` (includes the `reconcile_s224` backfill rows).
  - `estimated_left = total − spent`.
  - Empty table → every figure is `null`, never an error.
- `POST /admin/decisions/jev-credit/topups` `{topped_up_on, amount_usd, note?}` — `amount_usd > 0`
  (Pydantic `gt=0`), writes an `acp_shared.audit_log` row (`action = jev_credit.topup_added`) in the
  same transaction as the insert, like the AA-686 route/shadow edits.
- `DELETE /admin/decisions/jev-credit/topups/{id}` — 404 on an unknown id; writes an audit_log row
  (`jev_credit.topup_deleted`).
- Admin auth: the same `verify_admin_secret(x_admin_secret)` + `x-admin-user-id` as the other admin
  writes in this router.

### 3. Settings tab — `frontend/app/admin/settings/JevCreditTab.tsx` + `page.tsx`
New **"Jev Credit"** tab between "LLM Models" and "Tenant Info":
- A summary card: total topped up / spent (Jev) / estimated left (labelled **estimate**), and the
  last exhausted-alert time when present.
- A small add form (date, USD, note) and a table of top-ups with a per-row delete (ConfirmModal).
- Kit components only (`Card` from adminUi, `Button`/`Spinner`/`EmptyState`/`ErrorState`/
  `ConfirmModal`/`ToastProvider`/`useToast`/`apiGet`/`apiSend`), inline style + brand tokens, dark-
  theme safe (no hex/white literals, `alpha()` for fills). react-query, no setState in useEffect.
- **404 handling**: the Dev preview calls the Dev backend, which has neither the endpoint nor the
  table until Claude deploys. The tab treats an `ApiError` with status 404/501 as a clean empty
  state (nulls + "endpoint not deployed yet" note), so the UI smoke sees a rendered page, no crash
  and no console error.

### 4. Duplicate exhausted alert fix — `shared/llm_client/decide.py`
`maybe_alert_jev_credit` previously did a check-then-insert with no lock, so parallel `decide()`
coroutines on 10/10 04:09 UTC each read "no unread alert in 24h" and all inserted — three identical
`platform.jev_credit.exhausted` rows in the same millisecond. Now the check **and** the insert run
inside one transaction holding `pg_advisory_xact_lock(_JEV_ALERT_LOCK_KEY)`, so at most one alert is
written per 24h window even under heavy concurrency. Same best-effort contract (a notification
failure never fails the calling stage).

## Tests
- `tests/unit/test_aa756_jev_credit_topup.py` (new): GET math (populated + empty-table nulls), POST
  amount > 0 validation, POST writes audit, DELETE 404, DELETE writes audit.
- `tests/unit/test_aa720_jev_credit.py`: updated the throttle tests for the new transaction path and
  added `test_concurrent_alert_inserts_exactly_once` — 8 concurrent callers sharing one notifications
  store + one lock insert exactly one row.

## Out of scope / notes
- Claude applies migration 209, deploys, enters the two 10/10 + 04/10 top-ups, and live-verifies the
  GET against the LLM Usage page.
- The raw `<table>` in the tab is a lint *warning* (not an error), matching the existing
  `JevStagesCard` table in `ModelsTab.tsx` for the same kind of small read-only list.

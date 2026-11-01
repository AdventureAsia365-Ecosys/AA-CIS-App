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

### B1 — a real `enabled` switch for typesafe / dataforseo / geoapify, access_state, Jev's connection key

- **One rule, three clients.** Switched off = nothing is sent to the provider, and the refusal is written at 0 USD
  in the ledger that provider already uses. No row / a table before 322 / an unreadable row = on (the 322 seed), so a
  settings read never stops a provider.
  - Jev: `decide.py` reads `enabled` with its existing provider row (`_PROVIDER_SQL`, 20 s TTL). Off = every asked
    verdict is zone `error` (fail-open, the stage keeps its own rule), one `llm_call_log` row `status=refused`,
    `cost_usd=0`, `connection_key=account='typesafe'`, error class `other` (the CHECK has no `switched_off`). The daily
    canary (`POST /admin/jev-canary/check`, the other caller of `_call_jev`) answers `skipped` and calls nothing.
  - DataForSEO: `DataForSEOClient._request` is the only place that sends; it asks
    `connections.provider_enabled('dataforseo')` (own connection, 30 s TTL per process) before each call. Off = a
    `dfs_call_log` row `status=refused`, `cost_usd=0`, `meta={"connection_key": "dataforseo", "refused":
    "switched_off"}` (the table has no account column) and a fatal `DFSCallError`, so a run stops as on an auth
    failure and nothing is cached.
  - Geoapify: the row is already read when each Geoapify job starts (`refresh_daily_credit_limit`); the switch is read
    there too. Off = `GeocoderUnavailable` from `_get_key` (geocoder and place-details client), before any request:
    the run stops, names stay pending with no try counted (the existing "operator must fix it" path).
- **Every Jev ledger row now names its connection** (`connection_key='typesafe'`, stored as `account`): answered,
  refused, failed, and the canary row. Before, Jev rows had `account` NULL.
- **access_state.** Written on change (per process memo; a failed write backs off 60 s): Jev ok / 402 →
  `quota_exhausted` / 401-403 → `access_denied` / key unreadable → `unconfigured`; DataForSEO ok / 402 or DFS 402xx →
  `quota_exhausted` / 401, 403, DFS 401xx → `access_denied`; Geoapify 200 → `ok` / 401-403 → `access_denied` / key
  missing → `unconfigured`. A probe (the Jev canary, later the daily probe) always writes, with `last_probe_at`.
  The switch itself is not an access state (an operator choice, shown by `enabled`).
- **PATCH allowlist.** `PROVIDER_EDITABLE` gains `enabled`, for typesafe / dataforseo / geoapify only
  (`connections.PROVIDER_SWITCH_KINDS`); bedrock_guardrails stays migration-only (the CRM's client does not read it).
  No secret is read when a provider is enabled: the provider keys are read by the worker's getters
  (`shared/secrets.py`), which the api task role may not hold, and a denied read raises the "Denied secret access"
  alarm. A key that cannot be read shows on the first call (`unconfigured`).

### B4 — e-mail for the DataForSEO low balance and the reconcile mismatch, digest balances section

- **Same gate as every ops e-mail.** The caller queues the e-mail only when it just wrote a new bell row (the bell's
  own 24 h unread throttle), and the job key allows one e-mail per template per UTC day
  (`ops_alert_email:<template>:<day>`). Templates `dfs_balance_low` (from `POST /admin/dfs-balance/check`) and
  `llm_cost_mismatch` (from the nightly `llm_cost_reconcile`, settled days only, at most 10 rows in the mail with the
  full count).
- **The DataForSEO balance check is DataForSEO's probe**: it stamps `last_probe_at` with the state the read proves, and
  answers `skipped` (200, no call) while the connection is switched off. The Jev canary does the same for Jev.
- **Digest section "Balances and paid providers"** (OPS-12): DataForSEO balance (latest stored snapshot) and Jev credit
  estimate through `money_sources.balances()` (the money reports' reader), Geoapify credits used today vs the daily
  limit, the hand-set credits, each provider row's switch / access_state / last probe, and the settled reconcile
  mismatches of the last 3 days. Stored rows only: no provider is called by the digest. Attention lines: a balance
  under its threshold, a provider switched off or refusing, the Geoapify limit reached, a mismatch.

## Changed

- `shared/llm_client/bedrock_batch.py`: `BATCH_SWITCH_ENV`, `BATCH_CLOSED_REASON`, `batch_submit_closed_reason()`,
  the check in `submit_batch_job`.
- `api/routers/admin_pipeline.py`: the 503 at the top of `submit_s1_batch_endpoint` (nothing else in that file; lane
  L1's A2 edits it next).

- `shared/llm_client/connections.py`: `provider_enabled`, `note_provider_access`, `switched_off_reason`,
  `invalidate_providers`, `reset_provider_state` (test hooks `provider_row_loader`, `provider_state_writer`).
- `shared/llm_client/decide.py`: the switch, `jev_connection_enabled`, `note_jev_access` (own SQL: decide.py stays off
  the registry import guard), `connection_key` on both ledger rows.
- `services/seo_intelligence/dataforseo_client.py`, `services/places/geocoder.py`, `services/places/shape_sources.py`.
- `api/services/llm_connections.py` (allowlist), `api/routers/admin_llm_ops.py` (canary).
- `tests/unit/test_aa1017_registry_v2.py`: the old "a provider row takes no switch" case now expects the switch.
- `tests/unit/conftest.py`: no provider row read / state write in unit tests by default.

- `shared/ops_alerts.py` (two templates + enqueue), `services/jobs/ops_alert_email_job.py` (renderers),
  `api/routers/admin_llm_ops.py` (`_maybe_alert_low_balance`, `check_dfs_balance`), `services/llm_cost/reconcile.py`
  (`_mismatch_bell` → e-mail), `services/reports/digest.py` (`load_balances`, `balance_attention`,
  `render_balances`); `tests/unit/test_aa1003_r2_digest_email.py` patches the new loader in the isolation test.

## Tradeoffs

- Geoapify reads the switch at job start, not per request: a switch flipped during a 30-minute run applies from the
  next run. Per request would add a DB read per name; the job already reads the row once.
- A Jev ask refused by the switch writes one ledger row per decide() call (like the breaker-open refusals): the
  refusal count is visible, at the cost of rows during a long run with Jev off.

- 503 (not 403 / 409): the service is deliberately unavailable, and the caller should not retry until an operator
  opens it; the reason says how.

## Should know

- Tenant AI budgets are not in the digest balances section yet: `shared/llm_client/tenant_budget.py` has no read-all
  helper and lane L1 owns that module this session. Follow-up once L1 lands.
- A DataForSEO bell already unread within 24 h means no e-mail on the next check (the bell throttle gates the mail):
  for the live check, mark the old bell read first.

- The T-BATCH trigger row for the root backlog / `plan.json` goes with lane L7's root PR (plan §2), not here.
- ⚠️ `BATCH_SUBMIT_SWITCH` is an underscore env name: set it by hand in the task definition only when opening Batch.

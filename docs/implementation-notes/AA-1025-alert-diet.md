# AA-1025 (lane alert-diet, S263) — in-app ops checks instead of CloudWatch alarms (CIS)

Decision table: root `AA-Ecosys/docs/audits/2026-11-01-S263-alerting-cost/diet.md`. Infra side: AA-CIS-Infra
`docs/implementation-notes/AA-1025-alert-diet.md`.

## Decisions

- **One module, `shared/ops_checks.py`**: state checks (`jobs_starved`, `jobs_overdue`, `rds_role_connections`,
  `rds_xid_age`) from the api ops sampler tick, and log-event checks (the six `*_write_failed` events, the four
  `aa817_tenant_*` events, a tenant route on the admin pool, `too many connections for role "cis_app"`) counted by a
  structlog processor on the JSON handler (`shared/log_config.py`), so no call site changes and both the api and the
  worker are covered. The worker flushes its buffer every 5 minutes from its loop; the api on its tick.
- **E-mail through the alerts SNS topic**, not the `ops_alert_email` SES job: same recipients as the alarms it
  replaces, and a starved queue (the worker itself down) is still mailed from the api. Bell + e-mail dedupe in one
  advisory-locked transaction: a bell at most every 6 h per check, an e-mail only for the first bell of the UTC day.
  A failed publish deletes its bell so the next tick retries.
- **Evaluation time is a parameter** of `raise_alert` (`now=`, default the task clock) and of the last-bell query, so
  the 6-hour throttle and the UTC-day e-mail rule are tested at any hour (the integration tests no longer skip before
  06:00 UTC).
- **Thresholds unchanged** from the alarms (900 s, > 0 overdue, > 80 %, > 1e9).
- **XID age moved in-app**: `SELECT max(age(datfrozenxid))::bigint FROM pg_database` on the tick (readable by
  `cis_app`).
- **Known deliberate admin-pool rollback** (`publish_log_listed`) moved from Infra `tenant_pool_alarms.tf` into
  `ops_checks.KNOWN_ADMIN_POOL_ROLLBACKS`.
- **Dead code deleted**: `services/acp_produce/metrics.py` (AA-362, no caller) with its test, and
  `scripts/setup_run_health_alarms.py` (console-only acc1-era alarms on metrics nothing emits).
- The sampler keeps its `client` / `client_factory` parameters; they now carry the SNS client (fewer test changes in
  the files that call `sample_once`).

## Changed

- `api/core/ops_metrics_sampler.py`: no PutMetricData; new parts `xid_age` (in the roles part) and `_ops_checks_part`
  (after the backlog); line fields `ops_checks` / `ops_checks_error`.
- Admin bell list: `platform.ops_check.*` rows take their title from the payload ("Job queue starved").
- `shared/jobs/worker.py`: `_flush_ops_events` every `OPS_FLUSH_EVERY_S` (300 s), bounded at 20 s, never fails the loop.

## Tradeoffs

- The log-event buffer is per process and in memory: an event counted in the last few minutes before a task is killed
  is lost (the log line stays in CloudWatch Logs). The old metric filter did not have that gap; accepted for $0.
- No "sampling stopped" heartbeat: the api down is Infra's `aa-cis-dev-api-no-healthy-task`.
- `services/quality/thresholds.yaml` still names the deleted script in a comment; left untouched because that file is
  on the quality path (hashed by CI).

## Should know

- Needs the Infra apply first (task role `sns:Publish` on the topic); before it, publishes fail AccessDenied and the
  bell is removed again each tick (retried), no harm.
- Live proof: a due `noop_sleep`-style row the worker cannot claim for 15 minutes (or a job kind with no handler) makes
  the next tick write the bell and send the e-mail; the line `ops_metrics_sampled` shows `ops_checks`.
- Tests: `tests/unit/test_s263_ops_checks.py` (incl. a guard that every event name is still written by the code),
  `tests/unit/test_aa751_ops_metrics_sampler.py`, `tests/integration/test_s263_ops_checks.py`,
  `tests/integration/test_aa751_ops_metrics_sampler.py`.

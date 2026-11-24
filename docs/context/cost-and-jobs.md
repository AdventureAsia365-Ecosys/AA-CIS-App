# Cost observability, cost guard and the job runner

> Detail page of [CONTEXT.md](../../CONTEXT.md): the cost ledger and views, the spend guard and the durable job runner with its kinds. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

- **Cost observability (epic AA-616)**:
  - `shared.llm_call_log` records account (acc3 / acc2; rows before AA-998 may hold acc1), provider and
    `fallback_used` per call
    (AA-617), with correct Haiku 4.5 pricing and cache tokens (AA-635).
  - `shared.dfs_call_log` records each DataForSEO call, its real `cost` and cache hits (AA-618).
    The DFS cache TTL is 7 days (AA-625).
  - Cost attribution (AA-1058): every `llm_call_log` row names its object through the gateway context. The job worker
    binds the payload's `tour_id` / `piece_id` (`worker.job_attribution`) next to `bind_job`; `_execute_run_tour`,
    `_revalidate_tour`, `run_a3_atomize`, `run_write_background` and the `a1_seo` Jev of the SEO prefetch bind theirs
    (`call_log.bind_attribution` / `@attributed`). A row whose caller passes no `tour_id` / `content_piece_id` takes
    the bound one (never on an eval-labelled row). Rules: `shared/llm_client/attribution.py` (s1_ / a1_ / t5_ /
    a3_atomize -> tour, t9_ / t10_ -> piece, the A3 recompute Jev stages -> job); guard
    `tests/unit/test_aa1058_cost_attribution.py`. An empty stage is written `unattributed` (logged); a ruled row
    without its key logs `llm_call_log_attribution_missing`. DataForSEO rows always name stage and app: the caller's
    stage, else the bound job kind, else `dfs_balance` for the balance read, else `unattributed`. S1 writes
    `generated_content.prompt_version` (the registered version, else the writer's AA-289 hash), and the writer's
    ledger row falls back to the same hash when the register has no version (`billed_fields(resp, prompt_version=)`).
    Read-only coverage + cost-per-tour report: `scripts/aa1058/ledger_attribution.py` (run through the root ECS exec
    tool).
  - `shared.dfs_balance_snapshot` + a daily Lambda alert on low DFS balance (AA-627).
  - `shared.cost_explorer_snapshot` holds real AWS Cost Explorer figures for acc2/acc3,
    fetched on demand through `POST /admin/cost-explorer/check` (AA-623). acc2 calls CE
    directly; acc3 via assume-role (`shared/aws_client/cost_explorer.py`). AA-998: acc1 is no longer fetched; its
    stored rows stay and read with `account_key = "retired"` (shown as "Retired account", no account ID).
    AA-1127 (C2, tech lead decision S285): job kind `cost_explorer_refresh` (`services/jobs/cost_explorer_refresh_job.py`,
    core `api/services/cost_explorer_daily.py`) stores both snapshots (per account and service, and acc2 by App tag)
    for the month to date once per UTC day, queued by the ops metrics sampler from the hour of
    `cis.spend.ce_daily_refresh_hour_utc` while `cis.spend.ce_daily_refresh_enabled` is on (CIS 461, Platform ›
    Settings, area spend; about 3 CE requests, ~0.03 USD a run, reported as the job's own cost). The two Spend reads
    (forecast, By app) carry `ce_freshness`; the page shows "AWS figures stale" when the older of the two newest
    fetches is older than `cis.spend.ce_stale_after_hours`.
  - **How the admin names an AWS account** (AA-998): never by the internal key (acc2 / acc3 stay as keys in
    `account_route`, `llm_call_log.account` and the cost rows) but by its 12-digit account ID, read at run time
    (acc2: STS of the task's own credentials, once per process; acc3: `BEDROCK_ACCT_PRIMARY`) by
    `shared/aws_client/accounts.py` and served by `GET /admin/aws-accounts`. The frontend names accounts only
    through `frontend/lib/awsAccounts.ts` (`accountName` / `accountLabel`, hook `useAwsAccounts`); a guard test
    fails on a key spelled anywhere else in the frontend. Labels built by the API (Settings › Models "via",
    the account-route option, the forecast and Cost per app lines) use the same module.
  - The admin **Spend** page (`/admin/spend`; AA-665 Cost Operations at `/admin/cost-ops` until AA-1039 R6 slice 3,
    External Spend at `/admin/llm-usage` before that, both redirect) has the tabs Overview, AI & data spend (views LLM,
    DataForSEO, Jev), AWS bill and By app (the former Cost per app page) (Budgets moved to AI providers ›
    Budgets in AA-804 UI part 2: `GET /admin/ai-gateway/budgets` lists every cap by tier, edits still go through
    `PUT /admin/budgets/{provider}/{scope}`). It compares estimated spend (token
    logs) with the real AWS bill (AWS bill tab), splitting Bedrock from infrastructure per account, over any date range.
    - Overview: the **month-end forecast vs budget** (`GET /admin/cost-ops/forecast`, `api/services/cost_ops.py`):
      per provider family (Bedrock with the acc3/acc2 split, OpenAI, Jev, DataForSEO) and per AWS account (acc2,
      acc3; a `retired` row only for a month that still holds acc1 history),
      month to date + mean of the last 7 complete days x days left. DB only: the AI rows read
      `llm_call_daily`/`llm_call_log` like Cost per app, the AWS rows the STORED Cost Explorer snapshots (never a live
      CE call). Budgets: `spend_budget` global daily cap x days in the month; AWS from `cost_ops.AWS_MONTHLY_BUDGET_USD`
      (env `COST_OPS_AWS_BUDGETS_USD` overrides) until the AWS Budgets are read live (AA-665 C5).
    - DataForSEO › Runs: `shared.dfs_call_log.job_id` (migration 293) is the job running when the call was made
      (`call_log.current_job_id()`, as for `llm_call_log`); `GET /admin/dfs-usage/runs` groups spend by job with its
      kind, starter, payload scope, status, error and the job's LLM cost; `infer_unlinked=true` matches rows without a
      job (older rows) to the jobs running at the time, best effort, or to a recorded cost incident when no job ran.
    - Recorded cost incidents (`api/services/cost_incidents.py`): the reviewed findings of a spend investigation
      (trigger, scope, failure point, cause, what changed since) kept as code records; the DataForSEO tab shows each
      one overlapping the window, with its numbers read live from `dfs_call_log` / `llm_call_log` (rollup
      `llm_call_daily` after the raw retention). First record: the 25/09/2026 research sweep.
    - Jev: the Jev Decisions endpoints (`/admin/decisions/summary`, `/admin/decisions/jev-credit`).
  - Rule for manual scripts that call an LLM: go through `LLMClient.generate()` and log to
    `llm_call_log` with `stage="adhoc_<issue>"` (steering rule, from AA-635).
- **Cost guard (AA-649, migration 168)**: `shared/cost_guard.py` checks `shared.spend_budget`
  (per provider: `global` + `job:<kind>` rows; per-run and per-day USD, `hard_stop`, `alert_pct`)
  before every paid external call of a guarded job (today `segment_research`: DataForSEO + Bedrock).
  Each cap stops or only alerts according to the row that sets it: the DFS daily cap (`global`) is
  **alert-only** since 29/09/2026 (KAN-90: "control, not limit"), the per-run cap
  (`job:segment_research`, $5 DFS / $2 Bedrock) still stops a run. Edited via
  `PUT /admin/budgets/{provider}/{scope}` (no settings screen yet).
  **AA-642 S0 (migration 318):** two more callers. Jev: `decide()` checks `jev/global` `per_day_usd` (5.00, hard) against
  `cost_guard.day_spend('jev')` (llm_call_log provider `typesafe`) before every ask, re-read at most once a minute per
  process; over the cap the questions get zone `error` / `budget_exceeded`, no TypeSafe call, the decision_log rows
  are still written and the stage keeps its own rule. DataForSEO: every live `process_seo` fetch runs under a run
  budget (the S1 rewrite / batch prep: `dfs/job:s1_rewrite`, 0.50 per tour; no kind = the `global` row); a budget
  stop or a fatal DataForSEO error (401/402/403) skips the remaining sub-calls, the partial result is neither cached
  nor persisted and S1 goes on without fresh SEO data. `s1_prefetch.prefetch()` needs a budgeted client.
  Alerts: `maybe_alert` writes a `spend_budget_alert` bell row (one unread per provider per 24 h)
  and, since AA-828, queues one `ops_alert_email` job that the worker sends through SES
  (`shared/ops_alerts.py`, `shared/aws_client/ses_mailer.py`; env `SES_FROM_ADDRESS`,
  `SES_CONFIGURATION_SET` from AA-CIS-Infra). At most one e-mail per budget,
  threshold and UTC day (job idempotency key); `SES_FROM_ADDRESS` unset = nothing queued, one log line.
  Recipients (AA-913) are resolved when the job runs: the **confirmed** rows of `shared.alert_recipient`
  (Platform › Alert recipients, below) plus `ALERT_EMAIL_TO` (Terraform fallback, always included), deduplicated,
  all in one message; pending rows get nothing. When `ALERTS_TOPIC_ARN` is set the job first reconciles the table
  with SNS (best effort). Nobody to mail = `skipped`. Template `test` = the page's "Send test e-mail".
  **Jev credit (AA-845):** the same job sends `jev_credit_exhausted` (queued when the AA-720 402 bell
  `platform.jev_credit.exhausted` is written) and `jev_credit_low` (`shared/jev_credit.py`: estimated TypeSafe
  credit = AI providers › Jev credit top-ups − logged typesafe spend, below $3 → bell `platform.jev_credit.low`; checked
  every 30 min by the api ops sampler and on `POST /admin/decisions/jev-credit/check`). Throttle: one exhausted
  bell per 24 h while unread (AA-720), one low bell per 24 h read or not; e-mail only with a new bell, at most one
  per template per UTC day. The daily 402 sweep alerts only on real 402s after the latest recorded top-up and the
  latest successful canary. An explicit `threshold_usd` / `test_exhausted_alert` on the check route sends marked
  test alerts (admin bell only) that never throttle a real one and never show as the card's "last alert".
- **Durable job runner (AA-650, ADR 0001 decision 3)**: long work is a row in `shared.job`
  (migration 174), not an in-process asyncio task, so a deploy/restart re-queues it instead of
  losing it.
  - `shared/jobs/`: `queue.py` (enqueue with idempotency key, claim `FOR UPDATE SKIP LOCKED` under
    an advisory lock, lease + heartbeat, retry with backoff, reaper, cancel/retry), `registry.py`
    (`@job_kind(name, concurrency, max_attempts)`, `JobContext` for progress/cost), `worker.py`.
  - **Table shape (AA-751 / X6, ADR 0003a decision 2, migration 242)**: `shared/jobs/job_table_template.sql`
    is the reference DDL of every owner's `<schema>.job` (CIS `shared.job`, Booking `booking.job` keeps a
    byte copy, a future `tripplanner.job`); no runner applies it, each owner's migration instantiates it.
    `shared/jobs/job_table_shape.py` renders it and compares a live table through `pg_catalog` (columns,
    CHECKs, FK, indexes by definition, trigger, reloptions; names ignored), run by
    `tests/integration/test_aa751_job_table_template.py`. Since 242 `shared.job` enforces
    `job_kind_chk` (kind `^(?!bk_|tp_)[a-z][a-z0-9_]*$`: lower-case snake_case, never another owner's
    prefix), `job_lease_chk` (a `running` row has `locked_by` and `locked_until`; test helpers that insert
    running rows must set a lease) and `job_attempts_chk` (`attempt >= 0`, `max_attempts >= 1`); the
    BEFORE UPDATE trigger `job_set_updated_at` sets `updated_at` on every UPDATE (writers need not); queue
    autovacuum `scale_factor 0.02`, `threshold 50` (C5.1b as amended by AA-751). Changing the template =
    copy it to AA-Booking in the same session + one migration per owner.
  - Job kinds live in `services/jobs/` (`segment_research`, `tour_paa_backfill` — AA-1122, `t2_rewrite`, `t9_write`, `a3_atomize`,
    `s1_seo_prefetch`, `photo_sync`, `photo_medium_backfill` — AA-995, `photo_destination_match` — AA-671, `s1_rewrite`, `s1_batch_ingest`, `llm_batch` — AA-1076, `revalidate`, `recompute`,
    `place_cards_build`, `t7_slate`, `t8_angles`, and the place pipeline `place_extract`,
    `place_geocode`, `tour_graph_build` — AA-712, `place_shape`, `place_dedupe` — AA-847, `place_relocate` — AA-997, the five `retention_*` kinds — AA-825,
    `ops_alert_email` — AA-828, `data_erasure` — AA-839, the writing-memory kinds `writing_signal_backfill`,
    `writing_signal_fact_filter`, `writing_memory_mine` — AA-917, `writing_memory_r0` — AA-918, `writing_memory_r2`,
    `writing_memory_shadow` — AA-919, `writing_memory_post_activation` — AA-920, `writing_memory_kpi_weekly` — AA-922, `kpi_snapshot_daily`, `report_generate`,
    `inquiry_handoff_check` (a no-op since the R0 hotfix: TP-03 retired), `report_email` — AA-1003, `quality_funnel_watch` — AA-900 W0, `tenant_backup` — AA-1093, `tenant_erasure_saga` — AA-1093 L2). Every claim merges `progress.attempt_started_at` (AA-751, F18):
    `started_at` keeps the first attempt's start, so per-attempt age reads the progress key.
  - **Background kinds (AA-712)**: `@job_kind(..., background=True)` kinds never take the worker's
    last `BACKGROUND_RESERVE` (2) free slots (`Worker.claim_caps`), so a place-pipeline backlog cannot
    starve a wave's `a3_atomize` (claim is FIFO across kinds). Guarded by `test_aa737`. The rule is the
    pure `worker.claim_caps_for(max_parallel, caps, free_slots, running_background, background)`, also
    used by the backlog sample (below), so the two cannot drift.
  - **Retention (AA-825, migration 233, data standards DM-7 / C5.4)**: one background kind per log
    table in `services/jobs/retention_job.py` — `retention_llm_call_log` (180 d, daily rollup
    `shared.llm_call_daily`), `retention_tenant_api_usage` (90 d, monthly rollup
    `shared.tenant_api_usage_monthly`), `retention_job` (succeeded 30 d; failed/cancelled/stopped_budget
    90 d), `retention_decision_log` (180 d = Jev `CACHE_MAX_AGE_DAYS`; rows with a human `outcome`
    kept), `retention_cost_explorer_snapshot` (rows superseded by a newer fetch of the same day). The two
    tenant-row kinds skip the pilot tenant (`NOT_PILOT`, AA-969 d16).
    Policies in `services/retention/policy.py`. Rollups are written before raw rows are deleted;
    batches of <= 5,000 rows by (ts, id) keyset, archived first to
    `s3://<bronze bucket>/archive/<table>/yyyy/mm/` (`shared/aws_client/bronze.py`) as gzipped JSONL (pyarrow is not a
    dependency), never `VACUUM FULL`; rows referenced by a foreign key are never deleted. A manual run
    (`POST /admin/job-runner/retention/run`) is a DRY RUN unless `dry_run=false` + `max_rows` (the
    approved count). The ops-probe Lambda calls `POST /admin/job-runner/retention/schedule` once a
    day (skipped while a rerun wave is open): a kind stays DRY RUN until a live run with the same
    windows has succeeded, and a scheduled run whose candidates exceed 2x the last live run is HELD
    (bell `platform.retention.held`). Status: `GET /admin/job-runner/retention`.
    **Manual override** (S239, to prove rollup + archive before data ages): the same POST with
    `older_than` (a UTC date) enqueues `trigger=manual_override` with that cutoff instead of the window,
    only for the two rolled-up kinds, only where `ENVIRONMENT` is dev/test, cutoff >= 31 days old, DRY
    RUN by default, live at most 1,000 rows; it never counts as the last live run (schedule stays DRY
    RUN). Every live run of a rolled-up kind stops with nothing deleted if a day/month older than the
    cutoff is still missing from the rollup, and every archive object is HEAD-verified (Content-MD5 on
    the PUT) before its batch is deleted.
  - **Data erasure (AA-839, DM-6.7, runbook AA-Ecosys `docs/legal/dsar-erasure-runbook.md`)**: one background
    kind `data_erasure` (`services/jobs/erasure_job.py`, concurrency 1, no retry) for a **tenant** (`tenant` =
    tenant id) or a **traveller** (`email_sha256` = sha256 hex of the trimmed, lowercased address; the API
    refuses anything else, so an address never reaches a payload or a log). `POST /admin/job-runner/erasure/run`
    (admin, `tenant.write`) enqueues a **DRY RUN** by default: SELECT only, rows per CIS table and action,
    blockers, the delete order, the tenant's S3 objects/secrets (best-effort) and the per-app checklist
    (`booking.*`, `tripplanner.*`, `crm.*`, Cognito, mailbox, Sentry), plus a `fingerprint` of the subject and
    the counts (v2: the tenant id, or the address hash with a per-DRY-RUN salt that is stripped with the hash;
    a v1 DRY RUN, counts only, cannot be applied).
    Inventory: `services/erasure/inventory.py` — every CIS table with a tenant key has an action (`delete`,
    `detach` = keep the cost row without the tenant link, `merge` = add into the NULL-tenant rollup row, `keep` =
    the append-only `acp_shared.audit_log`). A tenant key is `tenant_id`, `tenant` (`shared.pipeline_jobs`),
    `owner_scope` (`acp_contract.tour_atoms`: tenant-owned T5 atoms may sit on an AA master tour) or a "tenant"
    soft key without an FK (`acp_contract.atomize_day_fingerprint.tenant_tour_version_id`); such entries carry
    explicit predicates (`where_any`) and are deleted before the tables those predicates read. A tenant-key table
    without an entry is reported `unregistered` and fails `tests/integration/test_aa839_erasure_job.py`. Rows
    that reach a tenant only through foreign keys are found from `pg_constraint` at run time
    (`services/erasure/plan.py`) and deleted children-first; a row of another tenant, a kept row, another
    owner's row, or a row of the same table outside the erasure (self FK: `shared.job.parent_job_id`,
    `tenant_tour_versions.parent_version_id`) pointing at an erased row is a blocker. Each check runs in a
    savepoint, so an unreadable table is one blocker, not an aborted DRY RUN. S3 keys held by deleted rows
    (`content_exports`, `webhook_deliveries`, `raw_sources`, `tenants`) outside `raw-inbox/<t>/` and
    `brand-identity/<t>/` are listed in `s3_refs` (more than 200 per column blocks). CIS holds no
    traveller data by design (only staff/alert e-mails are tagged in `shared`), so the traveller DRY RUN is a proof
    of absence (exact hash on the tagged columns, hashed e-mail tokens in notification/job/audit text).
    **Apply** is in the code but OFF: it needs `ERASURE_APPLY_ENABLED=true` (unset everywhere), a verified Cognito
    staff session, a succeeded DRY RUN of the same subject <= `ERASURE_DRY_RUN_MAX_AGE_HOURS` (24) old whose
    fingerprint is passed as `approved_fingerprint`, `approved_by`, `snapshot_id`, and for a tenant an offboard at
    least `cis.erasure.tenant_retention_days` (setting, default 30; the Dev drill sets 0 on Platform › Settings) ago. One transaction: recount = same
    fingerprint, every statement touches exactly the approved rows, a recount finds 0, one `acp_shared.audit_log`
    row (`tenant.erased` / `dsar.cis_erased`, counts only); for a traveller the hash is then stripped from every
    `data_erasure` row holding it. Inside the transaction the tenant row is locked (`FOR UPDATE`) and its
    offboarded state and retention window re-checked (a restore in between stops the apply). An apply claims its
    DRY RUN (`result.used_by_apply`, single use) and is never retried: `POST /admin/job-runner/jobs/{id}/retry`
    refuses it (409, `JobKind.retry_refusal`). A traveller job that fails or is cancelled has its hash replaced
    (`on_terminal`); `verify: true` on a traveller DRY RUN (after an apply) strips its own hash; Sentry events of
    this kind carry no frame locals (since AA-941 no Sentry event does, `shared/sentry_init.py`). Inventory and settings:
    `GET /admin/job-runner/erasure`.
  - **Per-tenant backup and restore drill (AA-1093 part 3, gap M27; plan D11)**: background kind `tenant_backup`
    (`services/jobs/tenant_backup_job.py`, payload `{"tenant_id", "trigger"}`, concurrency 1, $0) writes one tenant's
    CIS rows to the gold bucket under `exports/tenants/<tenant>/cis/<backup id = job id>/` (`tables/*.jsonl.gz` of
    `to_jsonb` rows, `manifest.json`, `SHA256SUMS`; SSE; the bucket's 7-day `exports/` rule is the retention). The
    table catalogue is the erasure plan (`services/tenant_backup/catalogue.py` over `services/erasure/`), so a table
    the erasure knows is backed up and an `unregistered` one makes the backup incomplete; rows are selected by the
    erasure predicate of the tenant (never the AA catalogue; the AA internal tenant is refused). Left out with a
    reason: the platform ledgers the erasure detaches / merges, `acp_shared.audit_log`, `shared.job`,
    `shared.pipeline_jobs`, `shared.llm_cassette`, `shared.tenant_api_usage`. Size cap `TENANT_BACKUP_MAX_BYTES`
    (512 MiB of JSON; tables smallest first, a table past the cap skipped whole and listed). On demand:
    `python -m services.tenant_backup.enqueue --tenant <uuid>`; nightly for the pilot tenant only with
    `TENANT_BACKUP_PILOT=on` on the api (default off; the ops sampler queues it from 20:00 UTC). Drill:
    `python -m services.tenant_backup.restore_drill [--tenant <uuid>] [--backup <job id>]` in the API container:
    checks every digest, loads each table into `pg_temp` (types of the live table, `jsonb_populate_recordset`),
    compares manifest / restored / live counts and live identity, rolls its transaction back, writes one
    `tenant.restore_drill` audit row (counts only), prints JSON (exit 0 complete, 3 incomplete backup, 1 failed).
  - **Tenant offboarding erasure saga (AA-1093 lane L2, contract `tenant.erasure.v1`, decisions D1-D8)**: CIS is the
    master of the tenant account (root ADR 0019 d1) and orchestrates the erasure of an offboarded tenant in CIS,
    Booking, TripPlanner and the CRM. Tables `shared.tenant_erasure_request` (one per offboarded tenant, no FK to
    `shared.tenants`: the erasure record outlives the tenant row; `is_pilot` / `is_synthetic` copied at T0) and
    `shared.tenant_erasure_step` (the newest report per round, app and mode in the contract's ErasureReport shape),
    CIS 408; feed event `account.erasure_requested` `{op, request_id}` on `tenant.entitlements.v1` 1.7 (CIS 409);
    Booking / TripPlanner read `catalog.v_tenant_erasure_v1` (CIS 410) and answer through their own report views
    (`booking.v_tenant_erasure_report_v1`, `tripplanner.v_tenant_erasure_report_v1`, read as JSON rows: a missing view
    or grant = `app_not_ready`, a view without a contract column = `report_shape_unknown` read from the catalog even
    with no row: blockers, never a skip); the CRM uses `GET /tenant/v1/erasure-requests` and
    `POST /tenant/v1/erasure-requests/{id}/reports` (M2M scope `cis.tenant/erasure`). T0 = `POST
    /admin/tenants/{id}/offboard` (request with `not_before` per app: the CRM T0 + setting
    `cis.erasure.crm_window_days` (90 days), the others T0 + `cis.erasure.tenant_retention_days` (30); never for the
    pilot); `POST /admin/tenants/{id}/restore` cancels it (and any failed request never approved) until a round is
    approved (409 after, also after a new round). Stages: stage 1 CIS + Booking + TripPlanner, stage 2 the CRM alone (`saga.STAGES`); each stage
    is a round: dry_run (each active app's plan hash; CIS = its own `data_erasure` DRY RUN, hashed without the tables
    the saga writes) → awaiting_approval (combined fingerprint = sha256(request, tenant, round, app:hash)) → approval
    by a **Cognito administrator** within `cis.erasure.saga_approval_valid_hours` (24 h; admin secret alone refused;
    never the pilot; on Dev synthetic only; apply switch setting `cis.erasure.saga_apply_enabled`, default off; the
    three keys come with CIS 411) → apply (the other apps in
    parallel, CIS **last**: `tenant_backup`, a fresh DRY RUN matched to the approved hash, the `data_erasure` apply
    with the approval's snapshot id and approver) → verify (each app's `pii:` census must be 0) → next stage or done
    with a receipt. A refused / failed step, a census above 0, a changed plan or an expired approval moves the request
    to **failed** (the contract's "the tech lead decides"): `.../new-round` starts a new round (never once the CIS
    apply job of the round started), `.../stop` keeps it failed for good. The approval re-computes the fingerprint
    from the step rows under the request lock (409 when a plan changed). Job kind `tenant_erasure_saga` (`services/jobs/tenant_erasure_saga_job.py`, payload `{request_id}`
    only, background, polls with `Reschedule`; the ops sampler keeps one live job per request with work). Staff routes
    `/admin/job-runner/erasure/requests[/{id}[/advance|/approve|/new-round|/stop]]`, `POST .../requests/drill` (window 0 for
    an offboarded synthetic tenant); admin page Data erasure › Tenant offboarding. Code: `services/tenant_erasure/`
    (`saga.py` rules, `policy.py` knobs, `store.py`, `reports.py`), `api/routers/admin_tenant_erasure.py`,
    `api/routers/tenant_erasure.py`; contract copy `docs/contracts/tenant.erasure.v1.openapi.yaml` (test
    `tests/unit/test_aa1093_tenant_erasure_contract.py`).
  - **Report core (AA-1003 R1, root AA-1001 report catalog)**: the registry of every recurring report and KPI is
    root `docs/reports/catalog.yaml` + `kpis.yaml` (linted there by `tools/docs/report_registry.py`); this repo reads a
    byte copy in `services/reports/registry/` (`registry.py`, loaded once; change root first, then copy). Two expand
    tables (CIS 305): `shared.kpi_daily` (one value per KPI ID, ICT day and scope `all` / `app:<app>` /
    `provider:<provider>`) and `shared.report_run` (one row per generation: period, status queued → running →
    succeeded / failed, summary, output JSON, output_markdown; one scheduled run per report and period). Background
    kinds in `services/jobs/report_jobs.py`: `kpi_snapshot_daily` computes the R1 KPIs from CIS tables only
    (`services/reports/kpis.py`: K-JOB-FAIL, K-JOB-QUEUEAGE from `shared.job`; K-LLM-COST-DAY by app and provider and
    K-LLM-FALLBACK-24H from `shared.llm_call_log`; K-CAT-LIVE from `gold_aa_internal.v_tour_liveness`, latest day
    only; K-RPT-FAILED from `shared.report_run`), writes what it could and raises when a part failed (retry), and a
    scheduled run chains `report_generate` for the CIS-generated daily/live reports (EX-04 scorecard, EX-06
    register; `services/reports/generate.py`, Markdown + JSON into the run row, no S3 yet). The api's ops metrics
    sampler queues the snapshot once a day for the day `periods.snapshot_day` names (day D from 00:30 UTC of D + 1;
    days are ICT, UTC+7). Manual runs and the register: `/admin/reports` (Platform › Overview › Reports, admin;
    `GET /admin/reports`, `GET /admin/reports/{id}/runs`, `GET /admin/reports/runs/{run_id}`,
    `POST /admin/reports/{id}/run`, 409 for a report not generated here; `POST /admin/reports/snapshots` {day?}
    re-computes one day's snapshot, the back-fill path, 90 days max). $0: DB reads only. A report with registry status
    `retired` (field `retired_on`, no generator) has due state `retired`: never due or overdue, its old failures are
    not flagged (EX-06, OPS-12), the register lists it under "Retired", the page shows "Retired".
  - **B2B-only model (AA-1003 R0 hotfix, root ADR 0019 d8 / d11; `services/reports/b2b_only.py`)**: since 2026-11-03
    the TripPlanner to Booking inquiry hand-off is off (send-to-advisor 410 `b2b_only`, `bk_inquiry_pull` off). **TP-03
    is retired**: no renderer, the ops sampler no longer queues the hourly check, no KPI snapshot of K-TP-HANDOFFGAP /
    K-BK-PARKED / K-BK-INQ2CASE (old `shared.kpi_daily` rows stay), no bell `platform.inquiry_handoff.gap`, no e-mail
    template `inquiry_handoff_gap`, no OPS-12 handoff section, no `INQUIRY_HANDOFF_ALERTS` variable; the kind
    `inquiry_handoff_check` stays registered as a no-op for a row queued before the release, and OPS-14 does not count
    it as a dead scheduler. The inquiry metrics other reports still show carry status `historical` (never red or
    amber): TP-01 inquiries sent and K-TP-T2I, the EX-01 B2C inquiry funnel and K-DEM-INQ, DATA-02 K-REC-INBOX and
    K-REC-UNMATCHED (DATA-02 no longer reads `booking.v_ops_inquiry_inbox` / `tripplanner.v_ops_inquiry_keys`; the
    owners' views and grants stay until the B2C drop). Tenant leads (`lead.submitted.v1`) get their own report once the
    contract has a producer.
  - **Daily safety net (AA-1003 R2; TP-03 retired by the R0 hotfix, see "B2B-only model" above, so the TP-03, hourly
    check, bell, e-mail and `inquiry_handoff` KPI parts below are history)**: `report_generate` also renders **OPS-12**
    (daily platform health digest,
    `services/reports/digest.py`: live /health + /health/ready of CIS (public origin `PUBLIC_API_BASE`, Dev default),
    Booking and the CRM (`platform_apps.http_health_card`), CIS workers' commits vs the API's (not compared when the
    API's commit is unknown), alarms in ALARM (DescribeAlarms), failed / overdue / lease-expired `shared.job` rows, LLM
    availability (`api.services.llm_availability`; `unknown` with no answered text call in 24 h is an attention
    item, never green), nightly probe verdicts, LLM spend vs the 14-day median, worker
    starts and migrations of the last 24 h, open waves, overdue reports; every section isolated, short when green) and
    **TP-03** (inquiry handoff, `services/reports/handoff.py`: TripPlanner `inquiry_sent` vs Booking
    `inquiry_received` per UTC day from the ops.overview.v1 contract views only). Kind `inquiry_handoff_check`
    (queued hourly by the ops sampler, key per UTC hour) checks the last 7 UTC days; a gap seen at two checks in a
    row raises the admin bell `platform.inquiry_handoff.gap` (24 h throttle unless it grows) and one ops alert e-mail
    per UTC day (template `inquiry_handoff_gap`); worker variable `INQUIRY_HANDOFF_ALERTS=off` turns bell and e-mail
    off (the check still runs). Kind `report_email` sends a scheduled OPS-12 run to its opt-in list
    only (`OPS_DIGEST_EMAIL_TO`, comma-separated; empty = skipped), never to the alert recipients. KPI computers
    `inquiry_handoff` (K-TP-HANDOFFGAP, K-BK-PARKED, K-BK-INQ2CASE, UTC day D) and `nightly_probes` (DATA-01:
    K-DATA-PROBES, K-DATA-LIVENESS, K-DATA-STALE from the `SCRIPT verdict` lines of the ECS run-script probes in
    CloudWatch Logs, `services/reports/probes.py`; needs `logs:FilterLogEvents` + `logs:DescribeLogStreams` on the
    log group; the ops overview client's short timeouts; the day's migrator streams are listed (DescribeLogStreams
    newest first, back to the day's start) and searched by name over the whole ICT day, because GitHub runs the
    nightly crons hours late; slices around each cron time are the fallback when listing is refused or the day is too
    far back; all searches of a day share a 12 s budget, an unfinished search counts nothing). A computer
    whose outside source is unreadable (probe log, or a contract view revoked / dropped / reshaped) raises
    `kpis.Unavailable`: no value, reason under `unavailable` in the job result, the snapshot still succeeds and chains. $0 (view reads, two cheap AWS reads, SES).
  - **Money reports (AA-1003 R3)**: `report_generate` also renders the weekly **EX-01** (business pack draft) and
    **FI-02** (spend and vendor balance digest, e-mailed to the `OPS_DIGEST_EMAIL_TO` list like OPS-12) for an ISO week,
    and the monthly **FI-01** (cost review draft, template cost-finops-review Part A) with **FI-03** (LLM
    attribution), **FI-04** (vendors), **FI-05** (unit costs), **FI-06** (budgets) and **OPS-08** (capacity) for a
    calendar month (`services/reports/money.py`, readers in `services/reports/money_sources.py`). The daily chain
    queues them: the snapshot of a Sunday chains the weekly reports for that ISO week, the snapshot of the day before
    business day 2 chains the monthly ones for the previous month (`periods.scheduled_period`; the register's due rule
    follows the same schedule). Stored rows only, per UTC day: the LLM ledger (`shared.llm_call_daily` +
    `shared.llm_call_log`), the vendor ledgers, the STORED Cost Explorer snapshots through `platform_cost.cost_by_app`
    (`refresh_if_stale=False`) / `cost_ops.cost_forecast` / `cost_explorer.read_cost_range` (a month's AWS data is
    labelled missing / partial / provisional / final; a report never calls Cost Explorer), budget bells, the
    contract views; OPS-08 adds catalog sizes and one CloudWatch GetMetricData call for the RDS metrics (already
    granted; unreadable = named, not failed). Per-App budgets mirror Terraform in `cost_ops.APP_MONTHLY_BUDGET_USD`
    (env `COST_OPS_APP_BUDGETS_USD`); the LLM account credit is set by hand in env `COST_OPS_CREDITS_USD`
    (`acc3=<usd>@<expiry>`). A source that cannot be read is a line in the report ("not wired" / "not readable"),
    never a failed run. No migration. K-UNIT-TOUR counts tours published or republished (published_at or updated_at
    in the month, CIS 273); EX-01 falls back to the live v_tour_liveness count when the week has no K-CAT-LIVE snapshot.
  - **Quality, reliability and data reports (AA-1003 R4)**: `report_generate` also renders the weekly **CIS-01**
    (catalog health: liveness, lifecycle events, S1 writer / cover / photo coverage per country from the data coverage
    live rule and `shared.v_tour_photos`, DATA-01 snapshots, action list), **OPS-13** (C1.2 objectives CIS can measure,
    `pg_stat_statements` per role, CloudWatch DatabaseConnections, two-week misses vs the previous run; draft),
    **OPS-14** (`shared.job` per kind vs the registry's `expected_seconds`, overdue, dead scheduled kinds, cost per run)
    and **DATA-02** (LLM ledger vs the stored acc3 bill; the handoff roll-up, inbox age and unmatched keys are
    historical since the R0 hotfix), the monthly **DATA-04** (rows past each retention window counted
    like the retention job, retention runs, growth, largest relations vs the previous run) and the event reports
    **CIS-06** (eval runs, writing-memory releases, worst items), **CIS-07** (rerun waves through
    `services/waves/report.py`, cost per stage, retry waste, projection) and **DATA-03** (`shared.schema_versions`
    per owner); event reports run on "Generate now" over the last 7 closed days (`periods.default_period`).
    `services/reports/quality.py`, `services/reports/reliability.py`. Booking / TripPlanner / CRM job tables, the
    CRM feed, GitHub checks and Vercel are named "not wired" / "not readable from CIS". No migration.
  - **R4 part 2 reports (AA-1003)**: the weekly **TP-01** (TripPlanner funnel from `tripplanner.v_ops_traffic_daily`,
    inquiries sent and K-TP-T2I historical since the R0 hotfix,
    AI searches and the TripPlanner online-stage LLM spend vs the mirrored default cap from `shared.llm_call_log`;
    `services/reports/planner.py`) and **DEV-03** (since AA-1108 the week's delivery per app from the GitHub delivery
    summary of Platform › Delivery; CIS deploys = worker starts on a new commit in `shared.job_worker`, rollbacks,
    migrations per owner, kept in the output so the monthly **DEV-01** can count a month after the worker rows are
    pruned; DEV-01 reads the month row of the same summary for the five DORA keys and the main failure rate, graded
    with the `cis.delivery.*` settings, and falls back to the worker rows when the summary is unreadable), the monthly **SEC-08** (audit hash chain re-verified with `platform_audit.verify_chain`, staff
    directory changes, calls by type, refused calls, off-hours bulk) and **SEC-03** (draft: staff account hygiene,
    audit chain, DATA-01 snapshots) (`services/reports/security.py`), and the event **OPS-15** (state at generation:
    ECS services via DescribeServices / DescribeTaskDefinition, already granted, image tags without the registry host,
    health, worker parity, newest migration per owner, active CIS jobs, open waves; the output is the session-close
    state-snapshot.json) (`services/reports/delivery.py`). Linear, GitHub (Dependabot, secret scanning, PRs, CI runs,
    releases), Vercel, Lambda and other owners' job and audit tables are listed as "Source not available". No migration.
  - **Quality funnel and nightly watchdog (AA-900 W0, root ADR 0020 "Measurement")**: `report_generate` also renders
    the daily **CIS-08** (`services/reports/funnel_report.py`, chained by `kpi_snapshot_daily` like EX-04): the funnel
    of `services/quality/funnel.py` per workflow and ISO week (ICT) over 8 weeks — T9 per channel
    (`acp_shared.content_piece`), S1 (`silver_aa_internal.generated_content` + `quality_scores` + `published_tours`),
    T2 (`gold_aa_internal.tenant_tour_versions`, AI edits only): created, delivered (T9/T2: approved + held, the
    agency receives both; S1: ever published — approved / published, or a `published_tours` row points at it, so a
    draft a later rewrite superseded stays delivered in its week), held, failed, first-pass yield (S1: no retry, no
    fix pass, no human edit / reviewer / `review_queue` row), attempts (S1: + 1 for the fix pass) and LLM USD per
    delivered object (the whole stage cost of the workflow; T9 per channel is the attributed part, the totals add
    `unattributed_cost_usd`); every failing T9 gate of every attempt (final ledger + `discarded_attempts`), with
    `F9_cta_fact` split into `.judge_unavailable` / `.text_integrity` / claims; S1 failure and brand codes per
    evaluation; per stage of `shared.llm_call_log`: rung share, fallback share, cut rate (`route_watch.CUT_ROW`),
    refused / error rows, cost. The stage numbers count **production rows only** (`route_watch.LEDGER_FILTER`: no
    shadow, eval or Jev row; no AI gateway route-test row, `quality_signal.route_test`) under their raw `stage`, so
    the funnel's cut rate is NOT `route_cut`'s (that one keeps eval rows under their real stage); `refused` leaves
    out the expected refusals (`shared/llm_client/expected_refusal.py`, counted apart as `expected_refusals`, as in
    `route_digest`). The run's `output` IS the snapshot; `GET /admin/quality/funnel` (ops.read or lab.read) serves
    the newest **scheduled** one (the run the watchdog read) plus 14 days of scheduled headline totals, never
    recomputed; the newest manual run is listed apart (`manual_run`, never alerted on). A succeeded scheduled
    CIS-08 run queues **`quality_funnel_watch`** (`services/jobs/quality_funnel_watch_job.py`, idempotent per run,
    deploy-gate exempt). Findings over the trailing 7 days: `qf_cut.<stage>` (cut > 5 % of the billed replies,
    setting `cis.ops.reply_cut_alert_pct`, shared with `route_cut`),
    `qf_fb.<stage>` (fallback ≥ the stage's `shared.llm_route_policy` `fallback_alert_pct`, route_watch's own row →
    `*` row → default 20 rule), `qf_drift.<stage>` (≥ 10 % of answers from a model under 1 % of the stage's answers
    the 7 days before, the minimum in both windows; settings `cis.ops.qf_drift_alert_pct` and
    `cis.ops.qf_drift_new_model_pct`); minimum rows = max(20, the policy's `min_requests`), the 20 being setting
    `cis.ops.qf_min_requests` (AA-1171, CIS 469; the values above are the seeded defaults). Alerts on
    the **transition** only: a finding the previous scheduled run (≤ 7 days back) already had is stored with
    `repeat: true` and not raised again (drift: unless a new model drifts), and the night's new findings go out as
    ONE `qf_digest` alert through `shared/ops_checks.alert_all` (bell at most every 20 h, setting `cis.ops.bell_every_nightly_hours`, as `route_digest`; one
    e-mail per UTC day). A failed e-mail or bell makes the job raise a retryable error (the bell is deduplicated, so
    the retry is safe). T2 per-attempt gate codes are not stored on the version (in `shared.job.result.t3` only). No
    migration (store: `shared.report_run`, report CIS-08 in the registry copy). $0.
  - **Ops metrics sampler (AA-832, AA-751)**: `api/core/ops_metrics_sampler.py`, a background task of
    the **api** (not the worker) started in `api/main.py`'s lifespan after the main pool, cancelled
    before it closes, off when `ENVIRONMENT=test`, never raises. Every 300 s (first tick 30 s after
    start) it makes one PutMetricData per namespace (boto3 in a thread, connect 3 s / read 5 s, one
    attempt) and logs one `ops_metrics_sampled` line with the values (`rds_error` / `retired_error` /
    `jobs_error` / `alert_recipients_error` when a part fails; the other parts still run). AA-913: one part
    reconciles the platform alert recipients with SNS (`shared.alert_recipients.periodic_reconcile`, no-op when
    the alerts topic is not configured). Each part runs under
    `asyncio.wait_for(PART_TIMEOUT_S = 60)` (the main pool has no `command_timeout`), so a hung query
    is a `TimeoutError` in the line and the next tick retries. Before the backlog sample it cancels
    queued/running rows of `shared.jobs.registry.RETIRED_KINDS` (kinds whose handler was removed;
    `ops_rds_role_connections` today, error `kind removed (AA-751)`, `retired_cancelled` in the line),
    so a rolling-deploy leftover neither pages as starved nor blocks the deploy gate:
    - `roles` (was the `AA/RDS` custom metrics until S263): sessions and % of the limit for
      every LOGIN role with a `CONNECTION LIMIT` (`pg_roles` LEFT JOIN `pg_stat_activity`, discovered
      each tick; the main pool logs in as `cis_app` since X1 S18, and `pg_stat_activity` still lists every
      role's sessions to it, only their query/state columns are hidden); `xid_age` = `max(age(datfrozenxid))`;
    - the backlog (was `AA/Jobs/CIS` until S263; `shared/jobs/backlog.py`): `overdue_running_jobs` = running rows
      whose current attempt (`progress.attempt_started_at`, else `started_at`) is older than 2 x the
      kind's `expected_seconds` (600 if unknown); `oldest_queued_age_seconds` = capacity-aware age of the
      longest-**starved** due queued row, 0 if none. A row is starved when no worker is live
      (`shared.job_worker`, `WORKER_ALIVE_SECONDS` rule), when no live worker has its kind in `caps`
      (unregistered), or when a live, non-yielding worker has a free slot, `claim_caps_for` keeps the
      kind and the kind is under its global cap. The age runs from the latest of `run_after` and the
      moment capacity last freed (latest finish / re-queue, the worker's start; for no live worker,
      the last time one was seen), so a full wave or the few seconds before the next poll read 0.
    **S263 alert diet (AA-1025): no CloudWatch metric or alarm any more.** The tick evaluates the values itself
    (`shared/ops_checks.py` state checks `jobs_starved` > 900 s, `jobs_overdue` > 0, `rds_role_connections` > 80 %,
    `rds_xid_age` > 1e9, `recompute_lifecycle_lag`: a tour on/off event waiting over 30 min for a finished
    recompute, AA-1056; `research_stale_share`, measured once a day: more than 20 % of the search demand research
    scope stale in a targeted market, AA-1055; since AA-1171 every one of these thresholds, the 2 x overdue factor,
    the 80 % app spend warning and the 6 h bell window are settings `cis.ops.*` on Platform › Settings, CIS 469,
    read through `shared/ops_thresholds.py`; the values above are the seeded defaults) and flushes the log events the process counted (structlog processor `ops_checks.capture`
    on the JSON handler: the `*_write_failed` cost-log events, the `aa817_tenant_*` fail-closed events, a tenant route
    on the admin pool, `too many connections for role "cis_app"`; the worker flushes its own every 5 minutes). A
    finding writes the admin bell (`shared.notifications`, `platform.ops_check.<check>`, roles admin, at most one per
    check per 6 h) and publishes one e-mail per check per UTC day on the alerts SNS topic (`ALERTS_TOPIC_ARN`, task
    role `sns:Publish`), the topic the removed alarms used. The api being down is the Infra alarm
    `aa-cis-dev-api-no-healthy-task`. Decision table: root `docs/audits/2026-11-01-S263-alerting-cost/diet.md`.
    The dead AA-362 `services/acp_produce/metrics.py` emitter (namespace `acp`, never called) is deleted.
    This replaced the AA-832 chain (ops-probe Lambda every 5 min ->
    `POST /admin/job-runner/rds-role-connections/schedule` -> job `ops_rds_role_connections` -> worker),
    both now removed. `@job_kind(..., deploy_gate_exempt=True)` stays as a mechanism (the pre-deploy
    guard `GET /admin/job-runner/deploy-gate` leaves such kinds out of its info counts and lists them in
    `exempt_kinds`; since AA-993 G5 those counts no longer block); only the test-only kind `noop_sleep` uses it
    (AA-993 G1, below). AA-990: the info counts also set apart queued rows due more than 1 h ahead
    (`queue.DEPLOY_GATE_HORIZON_S`, reported as `scheduled_later`). Today that is only the `place_geocode` run
    queued for the next Geoapify credit reset.
  - The worker runs as its own ECS service `aa-cis-dev-worker` (`python -m shared.jobs.worker`,
    AA-651); the API task sets `JOB_WORKER_IN_API=false`. The in-API loop (FastAPI lifespan) still
    exists for local/dev use.
  - Slot budget (AA-737): the service worker runs `JOB_WORKER_MAX_PARALLEL` jobs at once (default
    8), with the default thread executor and DB pool sized to match. Per-kind caps are global
    across workers: `s1_rewrite` 6, `a3_atomize` 1 (deliberate — platform-wide recompute + Cohere
    pacing). Keep `s1_rewrite` cap + `a3_atomize` cap below the slot budget, or rewrites starve
    atomize for a whole wave (S217).
  - A graceful shutdown releases running jobs (attempt not counted); a crash is caught by the
    reaper after the 90 s lease.
  - Drain on shutdown (AA-993 G1, ADR 0016 D1; root design `docs/audits/2026-10-26-S252-aa993-delivery-pipeline/`).
    On SIGTERM (`worker.serve()`) the service worker stops claiming, gives running jobs `JOB_WORKER_GRACE_SECONDS`
    (`worker_grace_seconds()`; AA-CIS-Infra sets 100 on `aa-cis-dev-worker`; absent = 20 s as before) and then
    releases the rest. The window must leave 15 s of the container's ECS stopTimeout
    (`JOB_WORKER_STOP_TIMEOUT_SECONDS`, default 120); otherwise the worker refuses to start (ValueError). The in-API
    worker keeps the 20 s class default. Logs: `job_worker_draining` (running, kinds, grace), `job_worker_drained`
    (finished / released), `job_released_on_shutdown` (next_status, `counted`, `release_delay_seconds`).
    - A release while a worker of a newer task revision is alive (a deploy, `queue.newer_worker_alive`) does not count
      toward `queue.MAX_RELEASES` (`queue.release(count_release=False)`), so deploys never fail a job; with no newer
      worker (or the check failing) it counts, as before.
    - Every release appends `{at, counted, worker}` to `progress.release_log` (last 20) and bumps
      `progress.shutdown_releases`; `progress.releases` stays the MAX_RELEASES counter. JSON in `progress`, not a
      column: `shared.job` is a hot table and the claim uses `RETURNING *` (design critic 6).
      `GET /admin/job-runner/released?since=` (default 24 h; machine GET in `MACHINE_ROUTES`, a row in
      `.github/verify/cis.rows`) returns `released`, `counted`, `jobs`, `by_kind`.
    - Registry fields: `release_delay_seconds` (a released job is due that much later; for kinds whose thread
      outlives the cancelled handler, set to the stopTimeout; `a3_atomize` = 120 since G3) and
      `deploy_blocking` (a kind a deploy must not interrupt: the deploy gate is unsafe while one of its rows runs or
      is queued due within 15 min, G5; today `data_erasure` and `s1_batch_ingest`; it cannot be combined with `deploy_gate_exempt`). `GET
      /summary` lists both per kind.
    - **Deploy gate (AA-993 G5, design D1.4).** `GET /admin/job-runner/deploy-gate` (`worker.deploy_gate_status`,
      also the Platform Apps card) answers `safe_to_deploy = false` only while a rerun wave is open (`open_waves`,
      `shared.rerun_wave.status = 'open'`) or a `deploy_blocking` kind runs (`blocking_running` per kind) or is
      queued due within `queue.DEPLOY_BLOCKING_HORIZON_S` = 15 min (`blocking_due`: the guard runs before the image
      build and the old task would claim it before its SIGTERM); `blocked_by` names the reasons. Queued/running jobs of other kinds no longer block: the worker drains them
      and the new revision re-claims a released job. `active` / `by_status` / `scheduled_later` stay as info.
    - `noop_sleep` (`services/jobs/noop_sleep_job.py`): a test-only kind, registered only when `JOB_TEST_KINDS=1`
      (set it on api **and** worker, or the post-deploy verify's worker-kinds row goes red); sleeps
      `payload.seconds` (1..3600), resumes from `progress.slept_seconds`, `max_attempts=1`, `deploy_gate_exempt`.
      It is the live drain proof: enqueue it, deploy, read the logs and `/released`.
    - Re-run safe kinds (AA-993 G2/G3). `s1_rewrite` records `progress.persist_stage` after each step
      (`services/content_generation/s1_checkpoint.py`: `writer_done` → `rewritten` → `content_inserted` → `scored` →
      `fix_pass_saved` → `routed` → `accounted`) and a re-claimed job resumes there: after the writer it re-enters the
      graph at validate (`build_graph_resumed`, the saved writer-state delta, v2 stamp kept — not the Bedrock Batch
      seed, which is v1-only); after the rewrite it pays nothing again; one version (found again by
      `generated_content.metadata.s1_job_id`), one `quality_scores` row (NOT EXISTS guard), one review entry. The early
      return is only `phase = "done"`. A real failure does not keep the checkpoint: a version INSERT data error drops
      the saved rewrite (next internal attempt rewrites) and a failed job clears `persist_stage` + the bulky keys, so a
      Jobs-page Retry rewrites; only a release (cancel) resumes. The polled Jobs list strips `s1_writer_state` /
      `s1_rewrite_result` from `progress`. `a3_atomize`: a `threading.Event` set when the handler is cancelled stops the
      thread at the next day boundary / between later steps (`AtomizeStopped`, late progress writes dropped), the
      chain runs under a session advisory lock on the tour (`handler.a3_tour_lock`, own connection, two int4 keys
      `hashtext('a3_atomize'), hashtext(tour_id)`; a waiting run polls, honours its own stop, gives up after 1800 s),
      and `release_delay_seconds = 120`; a `force` job records `progress.force_since` (first attempt) and a later
      attempt skips days whose fingerprint matches and `atomized_at >= force_since` (already re-read by this job).
  - After a deploy the old task drains for ~5 min; its worker stops claiming as soon as a worker of a
    newer ECS task-definition revision is live (AA-711, `job_worker.task_revision`, migration 199).
  - Job cost (AA-652 follow-up, migration 177): the worker binds the job id around each handler
    (`call_log.bind_job`), so every `llm_call_log` row the job causes carries `job_id`. Shown cost
    = `shared.job.cost_usd` (non-LLM spend the handler reports, e.g. DataForSEO) + the sum of
    those log rows, computed on read (`queue.LLM_COST_SQL`). `JobContext.add_cost` takes non-LLM spend only
    (DataForSEO, Geoapify, Cost Explorer): an LLM or embedding cost passed there counted twice (AA-1051 follow-up,
    S286); a per-job cap reads the ledger with `JobContext.llm_spend()`. Guard: an AST allow-list of the `add_cost`
    sites, `tests/unit/test_aa1051_add_cost_non_llm_only.py`.
  - Admin: `/admin/jobs` page, API `/admin/job-runner/*`. (`/admin/jobs/{id}` is still the older
    `shared.pipeline_jobs` poll for A1 run-tour.) The page lists with `fields=summary` (S241:
    payload/result come back null; the drawer reads them from `GET /jobs/{id}`) and polls every 30 s
    only while the summary shows queued/running jobs (Vercel Fluid CPU, S240 audit).
  - Observability (AA-687, migration 178): each worker upserts its row in `shared.job_worker`
    (start, ~every 15 s, `stopped_at` at shutdown; reaper totals + last reaped ids). Read-only for
    the page (`GET /admin/job-runner/workers`); the queue never reads it. `GET /jobs/{id}` adds
    `links` (tenant versions / pieces the job wrote, payload tour), `GET /jobs/{id}/llm-calls`
    lists the job's `llm_call_log` rows. Each kind has `expected_seconds` (flag, never a stop);
    long kinds report `progress.{step,done,total}` (a3_atomize since AA-688).
  - **T2 tenant rewrite is a job** (`t2_rewrite`, AA-652): `tenant_tour_versions.job_id`; a job
    that ends without success marks the version `status='failed'` (kind hook `on_terminal`), and
    the portal shows "Writing failed" + Retry (`POST /v1/tours/versions/{id}/retry`) instead of
    an endless "Writing…". The saved version also records (admin-only, hidden from every tenant read)
    how it was written (`generation_meta`, CIS 297, AA-920) and how it was scored (`judge_breakdown`, CIS 300,
    AA-795: the kept draft's brand fit / distinct / mission / feedback, the gate and the **binding axis** that set
    the score, plus `brand_claims` — age, group-size and brand-ethos claims of the saved text the A1 source does not
    support, deterministic and report-only, `services/content_generation/t2_brand_claims.py`); the admin reads it in
    Content Quality → Tour versions. **T9 write is a job** too (`t9_write`, `content_piece.job_id`; a job that
    ends without success marks the piece `failed`, which the portal already shows with Retry).
    **Machine-readable AI mark (AA-1040, EU AI Act Art. 50(2)):** every T9 piece is AI output;
    `content_piece.writer_input -> 'ai_provenance'` holds `ai_generated`, `generator`, the writer `model` of the final
    attempt (from the `t9_write` ledger row) and `generated_at`, stamped at finalize, plus `human_edited_at` on a
    tenant edit (no migration). `services/acp_content_writing/ai_marking.py` builds the mark: `piece["ai"]` in the
    tenant JSON, `data-ai-*` on the `<article>` and `<meta name="ai-*">` tags in exports and the WordPress post, the
    `X-AI-*` headers of a download. Facebook Page text posts have no API label (the mark stays in the piece).
    **AA-1168:** every channel that shows the text also shows the disclosure line: the last paragraph of the
    `<article>` (WordPress post, HTML exports), the end of a Facebook post (plus the social tag), and the plain-text
    export, which also ends with one parseable `AI-Content:` line (a saved `.txt` has no headers). WordPress posts also
    send the mark as REST post meta (`ai_*` keys, stored only when the site registered them). The wording and the tag
    are the setting `cis.content.ai_disclosure` (CIS 463, Platform › Settings, json `{"text": {<lang>: <line>},
    "social_tag"}`, `services/acp_content_writing/ai_disclosure_setting.py`): the piece language picks the line, English
    is required and the fallback; the line is a code invariant (an empty or malformed value is refused by the settings
    API and ignored by the reader, which then serves the seed). `ai_marking.DISCLOSURE_TEXT` / `SOCIAL_DISCLOSURE_TAG`
    are only that fallback. The Facebook text an attempt sends is stored on its row (`publish_log.sent_message`,
    CIS 464) and the AA-726 retry looks for it as well as the message rebuilt now, so a wording change between an
    attempt and its retry never duplicates a post. The agency studio export reads the wording from
    `GET /v1/content-writing/ai-disclosure`.
    T2 versions: `project_version()` adds `ai` from `edit_source` (`tenant_edit` = human-edited) and the DOCX download
    carries it in its core properties (keywords `ai-generated`, comments).
    Master catalog text (A-series `published_tours.aa_*`, S1 writer): `project_tour()` adds `ai` (`generator`
    "Adventure Asia catalog writer", `generated_at` = `updated_at` else `published_at`; `model` and `human_edited`
    null, both recorded admin-side on `generated_content`) to `/v1/tours/pool`, `/v1/tours/{id}` and `/{id}/full`.
    **A3 atomize is a job** (`a3_atomize`, one tour at a time), enqueued by publish
    (`process_export`) and by `POST /admin/atoms/atomize` (one job per tour).
  - **Recompute stage registry (AA-735 nac 3, ADR 0003 layer B + C)**: the platform-wide
    Segment/Score/Route recompute is a declared stage registry in `services/recompute/`, not two
    hand-written chains. Each stage (`segment`, `landing`, `score`, `route`) is a pure
    `run(ctx, scope)` that declares the tables it reads, the tables it writes and its
    `write_strategy` (`segment`/`landing` upsert; `score`/`route` versioned-swap — the SQL itself
    is unchanged, only declared). The orchestrator `run_stages(names, scope, pool=, progress=)`
    runs a named list in order. Two lists: `TOUR_STAGES` (segment→landing→score→route→retarget, a tour's
    publish / atom edit) and `PLATFORM_STAGES` (segment_catchup→landing→score→route→retarget→segment_gc: the AA-1059
    catch-up, platform score+route, the AA-1057 retarget, then the AA-988 orphan-Segment clean-up).
  - **Segment catch-up (AA-1059)**: the `segment` stage runs only per tour (a3 chain, tour-scope recompute), so a
    Master whose segment step failed (best-effort in the a3 chain), never ran, or read no atom (atomized while
    inactive, then restored) stayed without Segment / Score / Route. The platform pass now starts with
    `segment_catchup` (`services/recompute/segment_catchup.py`): active Masters none of whose live atoms is in a
    Segment and with >= 1 atom `forms_segment` accepts get `run_segment_matching`, at most `SEGMENT_CATCHUP_MAX`
    (env, default 10, 0 = off) per pass, each under the tour's A3 lock taken without waiting (`busy` when an
    `a3_atomize` holds it); failures counted per class, never failing the pass; touched Segments go to `landing`.
    Result key `segment_catchup`. A Master whose atoms are all transit / unnamed by rule has no Segment by design
    (`all_excluded`).
    The `recompute` job and the `a3_atomize` path (its inline segment matching) run their work
    only through `run_stages`; the thin wrappers `recompute_segment_score_route` /
    `recompute_rankings_and_routes` in `services/export/handler.py` map the stage results back to
    the Jobs-page result keys (`{segment, ranking, route, retarget}` / `{ranking, route, retarget, segment_gc}`).
  - **Tenant ids follow a rewrite / supersede (AA-1057 GAP 2)**: the `retarget` stage
    (`services/acp_shared/retarget.py`) moves a picked / used Route Subject off a superseded Route (the current
    Route of the same tour family with the same day span, else the one sharing the most Segments; one already held
    by the tenant in that channel → left as is) and an angle-gate request off an atom that is no longer live (the
    AA-990 same-place re-point, else, for a Segment Subject whose old tour is not paused, the member a pick chooses
    today: atom, `trip_id`, Blog walk). Guarded UPDATEs, never deletes; a run that writes puts its restore list (ids
    and every old value it overwrote) in its result. Mode env `RECOMPUTE_RETARGET_MODE` = `apply` (default since the
    audited one-off ran) | `report` | `off`; one-off audited run: `scripts/aa1057_retarget_once.py`
    (DRY RUN first; the apply is pinned to the reviewed ids).
  - **Recompute is drain-safe and swept (AA-1056)**: the `recompute` job runs under ONE platform session advisory
    lock (`services/export/handler.job_session_lock`, class `recompute`, key `platform`; the generic body of the
    AA-993 G3 `a3_tour_lock`), has `release_delay_seconds` = the worker's ECS stopTimeout
    (`shared/jobs/registry.WORKER_STOP_TIMEOUT_SECONDS`, also a3_atomize's), stops at the next stage boundary once
    its handler is cancelled (`run_stages(..., should_stop=, on_stage=)`), and fails with fixed codes
    (`services/recompute/errors.py`: `recompute_bad_payload`, `recompute_lock_timeout`, `recompute_stopped`,
    `recompute_stage_failed` with the stage). A job that finds the lock taken does not wait in process (S282): it
    is rescheduled (`registry.Reschedule`, reason `recompute_lock_busy`, the attempt not counted) after the release
    delay, and only an attempt that keeps finding it busy for the kind's expected time ends `recompute_lock_timeout`. `services/recompute/lifecycle.py` measures each tour on/off event
    (`retired` / `paused` / `restored`) to the first recompute that STARTED after it and finished: the api ops sampler
    reads it every tick (ops check `recompute_lifecycle_lag`: an event waiting over 30 min), sweeps every 2 h (one
    platform recompute through `enqueue_recompute` for the events nothing covered, `created_by`
    `scheduler:recompute_sweep`), and Data coverage shows it (rows `recompute.lifecycle_*`, source `recompute_lag`).
    `enqueue_recompute` is the one enqueue helper for every trigger.
  - **Segments follow a re-atomize (AA-988)**: the `segment` stage drops the member rows of its tour's dead atoms
    (soft-deleted by the re-atomize, or empty-day markers) in its write transaction; the `segment_gc` stage
    (`services/acp_contract/segment_gc.py`) deletes Segments nothing refers to any more (no member on a live atom, no
    ranking row, no alias row either side, no Subject, brand-fit cache row, current / Subject-named route or
    angle-request route list) and their dead member rows. Mode env `SEGMENT_GC_MODE` = `report` (default, counts and
    samples in the `recompute` result) | `apply` | `off`; `apply` holds (`held_volume`) above `SEGMENT_GC_MAX`
    (10,000). Lock `acp:segment_write`: every segment run holds it shared from its candidate read to its write; the
    GC takes it exclusive (30 s lock timeout → `skipped`). The `a3_atomize` path re-lands the Segments the tour had
    BEFORE its segment stage too (cache clear + recompute scope), and then runs the $0 place-card relink
    (`place_cards.relink_tour_place_cards`, `max_places=0`, result key `place_cards_relink`) before `place_extract`
    (AA-989). S262: a group the relink (or a build over its 40-place cap) leaves for a paid draft gets an
    `atom_place_card` row with `card_id` NULL and `miss_reason = 'needs_draft'` (`place_cards.NEEDS_DRAFT`, not a
    `place_card_miss` row: the build and T8 still draft it); such an atom is linked for free (`carried_over`) only
    when every replaced predecessor of the same source that competes for it carries the same non-rejected card:
    same name (any day while they agree, else the same day; a generic name such as "City Palace" the same day only),
    else same day + core name. A cardless predecessor (miss, needs_draft, no row, rejected card) competes too.

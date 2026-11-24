# Batch lanes, plans, billing and tenant runtime

> Detail page of [CONTEXT.md](../../CONTEXT.md): Bedrock and gateway batch lanes, plans and quota, billing, tenant account pages and live writing progress. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

- **Bedrock Batch for S1 (AA-606)**: `shared/llm_client/bedrock_batch.py` +
  `services/content_generation/s1_batch.py`, endpoints `POST /admin/s1-batch/submit` and
  `GET /admin/s1-batch/{job_id}`. Built and merged, but **never run for real**: AWS has not yet
  enabled Batch Inference on acc3 (support case 178979743800653, blocker AA-624). Batch atomize
  (AA-621) waits on the same blocker.
- **Gateway batch lane (AA-1076)**: `shared/llm_client/batch_lane.py` + `adapters/anthropic_batches.py` (Message
  Batches) / `adapters/openai_batches.py` (Batch API): bulk work nobody waits on runs as ONE provider batch at 50% of
  every token. Target = the stage route, model first, on its first `anthropic` / `openai` deployment of
  `vendor_route.deployment_plan` (scopes, roles, pii, circuits, budgets as on demand). Guards before submit: the
  switch `LLM_BATCH_LANE` (default on), tenant stages `t<n>_*` refused (stay synchronous), the stage kill switch, the
  app AI switch, a cost cap per batch (required, <= 100 USD) against the gateway's worst-case estimate at the batch
  rate, and the central budgets. Request bodies come from the on-demand builders (`anthropic_messages.build_params`,
  `client.openai_chat_body`). Driver: the `llm_batch` job (`services/jobs/llm_batch_job.py`), ONE row per batch that
  reschedules itself (`registry.Reschedule`: back to queued, due later, attempt not counted, state in `progress`;
  submit fenced by phase `submitting`); at the end one `llm_call_log` row per request (`pricing_mode` '+batch',
  `quality_signal.batch_id`, `unit_kind` `batch_request`), then the consumer hands the usable replies to the normal
  jobs (`services/llm_batch/consumers.py`: `s1_attempt1` -> `s1_rewrite` jobs, the S1 writer v2's write call only;
  `a3_atomize` -> `a3_atomize` jobs, the changed days; `adhoc` for scripts). The replies are bound with
  `batch_lane.bind_replies` and used by `prefetched(stage, system, user)` only for byte-identical prompts on attempt 1;
  failed / expired / cancelled items, stale prompts and every retry run on demand. Admin: `GET/POST
  /admin/ai-gateway/batches`, `POST /admin/ai-gateway/batches/{job_id}/cancel` (Batches tab of AI providers). The wave
  report counts an `llm_batch` job (payload `tour_ids`) as each tour's job.
- **Plans and quota (AA-640, AA-1028 R1 slice 3)**: the tour-rewrite quota is the tenant's resolved entitlement
  `content.rewrite` (starter 50 / growth 200 / business 500, edited on Tenants › Plans & add-ons), with the old
  `shared.membership_plans` read only as the fallback for a tenant with no numeric key. Over-quota rewrites are
  allowed, logged as `rewrite_over_quota` and billed as overage. **One rewrite ledger (AA-1127, CIS 448):** the month's
  count is `shared.tenant_rewrite_usage` (incremented only by `trigger_rewrite`); `GET /v1/quota`, the usage view's
  `tours_rewritten` (`GET /v1/billing`, the admin tenant list, Tenant 360) and the billed overage all read it. A
  tenant's own inline edit (`tenant_edit`) is not a rewrite. There is no `PLAN_LIMITS` any more: a tier's
  default RPM is its live content plan's `content.rate_limit_rpm`; an admin can override one tenant's RPM
  (`PATCH /admin/tenants/{id} {"rate_limit_rpm": n}`, AA-766, audited as `tenant.rate_limit_changed`).
- **Billing (AA-798, migration 223)**: `shared.v_tenant_monthly_usage.llm_cost_usd` = month-to-date
  `SUM(shared.llm_call_log.cost_usd)` for the tenant (shadow and NULL-tenant rows excluded). Admin
  billing and the admin tenant list read that column as-is (one definition, `api/routers/_billing.py`).
  Migration 233 (AA-825) made the view's month filters sargable (range on the raw column, index
  `tenant_api_usage (tenant_id, called_at)`), same columns and numbers. The admin tenant usage
  history (`GET /admin/tenants/{id}/usage`) reads `shared.tenant_api_usage_monthly` for rolled-up
  months and raw rows for the rest.
  No plan attached → plan fields are null (no invented Starter defaults). Tenants never see cost
  (`/v1/billing` has no `llm_cost_usd`, AA-796).
- **One live post per content request and channel (AA-1127)**: `GET /v1/publish-log/pending` does not offer, and
  `POST /v1/publish-log/{piece_id}/publish` refuses (409), the newest piece of a request whose earlier piece is live or
  in flight on the same channel (`REQUEST_LIVE_SQL`, `api/routers/v1_publish.py`), the rule My Content reads (S251).
  Taking the live post down makes the newest piece publishable again.
- **Tenant Account pages (AA-761)**: `GET /v1/account/activity` (the tenant's own `acp_shared.audit_log` rows,
  newest first, `category` / `action` / `since` / `until` filters, offset paging with a total, `page_size` ≤ 100),
  and `GET /v1/account/activity/export` (the same filtered set as CSV, at most 50 000 rows, a cut file says so).
  `GET /v1/account/api-key` was removed with the tenant API keys (AA-1036). Tenant JWT only; the id comes from the token. `audit_log` has no RLS
  and no tenant-role grant, so these read on the main pool with the id bound (the `/v1/billing` pattern), and only
  an **allowlist of actions** is visible (`api/services/tenant_account.py` `TENANT_VISIBLE_ACTIONS`): admin silent
  corrections, the staff request trail, brand / config / membership edits and the erasure record never are. A new
  audit action is invisible to tenants until it is added there. `details` never leaves raw (a per-action whitelist
  builds `detail`). Billing still reads `/v1/billing` + `/v1/quota`; CIS stores no invoices yet.
- **Pipeline routes are admin-only (AA-799)**: every `/v1/pipeline/*` route requires the admin secret or (AA-816
  C9) a verified staff principal (`_require_admin`, a router-wide dependency in `api/routers/v1_pipeline.py`); a
  tenant JWT gets 403,
  no auth 401. The edge (API Gateway / ALB) does not route `/v1/pipeline` today. `/run` has no caller
  (deletion candidate).
- **Live writing progress (AA-637, `docs/adr/0004-*.md`)**:
  - Tenant writing jobs run in the background: the T2 tour rewrite and the T9 post write.
  - While running, each job binds a stream sink (`shared/llm_client/stream_sink.py`).
  - Writer-stage LLM calls stream through that sink into a Redis snapshot
    (`services/acp_shared/writing_progress.py`, key `wp:{tenant}:{kind}:{job}`, 1h TTL).
  - The portal polls `GET /v1/progress/{tour|piece}/{id}` (`LiveWriter.tsx`).
  - Only piece text reaches the snapshot (AA-668): a writer-stage call whose reply is not piece
    text (the T9 sentence repair's JSON patch) runs under `stream_sink.suppressed()`, and the job
    publishes the text it keeps with `progress_show()` (after a repair splice and before the save),
    so the final snapshot is the saved post / tour version.
  - Both jobs show in the same right-side **Writer panel** (AA-668, `WriterPanel.tsx`'s
    `WriterDrawer` shell): Social Content (Slate → goal → angle → write) and the T2 tour rewrite
    from My Catalog Tours (`TourRewritePanel.tsx`). Closing the panel never stops the job;
    `useWriteWatcher` polls it and raises a toast when it settles.
  - This is a view only: persisted rows are unchanged. There is no SSE, and API Gateway is
    unchanged.

# AA-936 (CRM-08) — CIS sees the partner CRM (AppInfo "crm" + jobs)

Card CRM-08 checklist "CIS `AppInfo("crm")` + jobs tab" (root `docs/audits/2026-10-21-S246-crm-design-v3/design-v3.md`
§6.3). The CRM side (`/crm/ops/v1/jobs/summary`, `/crm/ops/v1/llm/usage`, contract `crm.ops.v1`) is AA-Partner
branch `feat/aa-936-slice1`.

## Decisions

- **Probe like Booking**: the CRM card is the shared HTTP health card (`_http_health_card`, Booking's probe made
  generic) on `/crm/health` + `/crm/health/ready` of the partner host; it joins the cached Apps payload (cache key v3).
- **Jobs with the staff member's own token**: `/crm/ops/v1` accepts only WORKFORCE `cis-admin` tokens (root ADR 0006b
  decision 6), so `GET /admin/platform/apps/crm/jobs` relays the request's Cognito Bearer (the BFF already sends it on
  staff paths, AA-816 C9). A legacy HS256 session gets `cognito_session_required` and no call. Not cached: the answer
  depends on the caller. No service credential, no new secret, no cross-schema read.
- **Cost page**: `cost_tags=("crm",)` gives the CRM its own row (AWS by App tag). CRM LLM calls are in the CRM's own
  `crm.llm_call` (D12), so `llm_app` is None. The row's AI spend comes from `GET /crm/ops/v1/llm/usage` (review
  S249: the checklist item "llm-usage, CRM row on the CIS cost page"): `GET /admin/platform/cost` passes
  `platform_apps.crm_llm_usage` bound to the staff member's own Cognito Bearer, which totals the rollup per model into
  the CRM row (`ai_usd`, `ai_calls`, `ai_by_source` as `crm_gateway:<model>`, `billed_in_aws_acc2` false). The answer
  carries `crm_llm.status`; when it is not `ok` (legacy session, CRM down, refused) the page shows one amber line
  "Partner CRM AI spend not included (<reason>)" instead of a silent 0. Slice 1 runs only the stub provider (cost 0),
  so the row shows the attempts with $0 until CRM-33a.
- **One helper for crm.ops.v1** (`_crm_ops_get`): token check, call, refusal / error mapping and a 200 that is not a
  JSON object (review S249: `r.json()` on a non-JSON 200 used to raise and turn the route into a 500) are handled once
  for the jobs tab and the cost row.

## Should know

- Route policy snapshot regenerated (`GET /admin/platform/apps/crm/jobs` -> `ops.read`).
- Unit: `tests/unit/test_aa936_crm_app_info.py` + the updated Apps / cost / consistency tests (169 passed locally);
  `tsc --noEmit` of the frontend green (first round). Review round (S249): the AA-936 / AA-884 / AA-882 / S243 unit
  files, 2,394 passed; flake8 (CI flags) clean on the changed Python files. The frontend change is one optional field
  and one note line; no local build this round (RAM), CI builds it.

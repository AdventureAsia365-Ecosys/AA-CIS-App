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
  `crm.llm_call` (D12), so `llm_app` is None; the CRM's usage endpoint (`/crm/ops/v1/llm/usage`) exists for when CRM-33a
  turns real calls on (Slice 1 runs only the stub provider, cost 0).

## Should know

- Route policy snapshot regenerated (`GET /admin/platform/apps/crm/jobs` -> `ops.read`).
- Unit: `tests/unit/test_aa936_crm_app_info.py` + the updated Apps / cost / consistency tests (169 passed locally);
  `tsc --noEmit` of the frontend green.

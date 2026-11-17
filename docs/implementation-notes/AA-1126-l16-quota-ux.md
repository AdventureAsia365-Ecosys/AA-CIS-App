# AA-1126 — lane l16 (quota UX): slices L1, L4 on the CIS side

Lane l15 of the same wave writes the gateway slices (G1 connection cap mode, G2 anomaly breaker, G4 run caps) in its
own notes file (`AA-1126.md`). This file covers the plan-quota user experience of the Content studio (L1) and the budget
read error (L4), plus the CIS data the TripPlanner (L2) and CRM (L3) lanes read.

## Decisions

- **One spend environment setting per app, not one flag per behaviour.** `cis.spend.environment` (dev | prod, CIS 447,
  Platform › Settings, area spend, maker-checker, EN + VI guide) and `tripplanner.spend.environment` (same, read by
  TripPlanner through `catalog.v_app_settings_v1`). dev = today's hard behaviour (the default); prod = the tech lead's
  pay-per-use rules. Going to prod is a settings change, not code. The gateway's own modes (connection cap, breaker)
  stay lane l15's separate keys because they are tuned separately.
- **402 `ai_quota_exhausted`** (`api/core/ai_allowance.py`) on every tenant request that would start new AI writing
  (rewrite, Retry, angles, write) while the tenant's plan allowance is used up (hard stop on, spend at or past its
  approval threshold). Body: `{code, message, resets_at}`. 402 because the fix is a plan change, and it is distinct from
  the 409s the studio already handles.
- **Versions carry `job_stop_code`** (`ai_quota_exhausted` for a job held by its tenant's budget, else null) and
  `allowance_resets_at`: a code only, never the job's error text (which names budget figures).
- **The agency is told in its own bell** (`ai.allowance_used`, once per tenant and UTC month: partial unique index of
  CIS 447) with an Upgrade link (`upgrade: true` on the bell item) to the studio's plan page. TripPlanner writes its own
  `tripplanner.ai_allowance_used` row (CIS 447 lets its role INSERT exactly that and nothing else).
- **Held work resumes on its own.** The hourly `tenant_account_tick` re-queues the jobs a tenant's budget held once the
  tenant no longer needs approval (new month, upgrade, raised budget), through the existing
  `queue.retry_by_reason(tenant_ai_budget_approval)` which keeps the jobs of a tenant still over its allowance held.
  So the hold message says "Writing continues on its own when the allowance resets or after an upgrade" truthfully.
- **L4:** under `prod`, a tenant budget that cannot be read lets the T-stage call go on with a warning log and one admin
  bell per tenant per 24 h (database de-dup) and per 10 min in process. Under `dev` it still fails closed (AA-642 A8).
  The 402 pre-check never blocks on a read error in either environment (the gateway decides at call time).
- **CRM plan AI budget (L3 data):** feature `crm.ai_budget_usd_monthly` (crm, limit, usd_month) in the feature
  catalogue, so it is a limit on the Plans page and a per-tenant override in Tenant 360; seeded on the live crm plans at
  today's CRM default (2 USD): no change on apply. The commercial values are the tech lead's to set on the Plans page.

## Changed

- Migration 447 (migration-only PR, batch with lane l15's 446): two setting keys, the notice index, the TripPlanner
  INSERT grant + policy, the CRM feature + plan rows. `shared/llm_client/spend_environment.py` ships in both PRs (the
  settings lint needs a reader of every enforced key; identical add/add merges cleanly).
- Code PR: `api/core/ai_allowance.py`, the four tenant routes, `v1_tours` version reads, `v1_notifications` titles,
  messages and `upgrade` / `resets_at` fields, `cost_guard` notice + unreadable bell, `tenant_budget` L4 + quota code,
  `lifecycle` tick step 4.

## Tradeoffs

- The 402 pre-check is one extra budget read per AI-starting request. Cheap (one indexed sum) and it saves a queued job
  plus a quota charge that would only stop.
- The unreadable bell writes through a fresh connection while the database may be the reason: best effort, paced,
  never raises.
- The CRM plan value seeded at 2 USD keeps Dev unchanged; the business owner's prices may set other values.

## Should know

- Renumber note: 445 is on main, lane l15 holds 446, this lane 447. On a renumber, `git grep '447_'` and `'447'`.
- structlog: never pass `event=` as a keyword (it is the log message's own argument): it raised TypeError in the notice
  path, which the gateway's "never on the call path" guard would have turned into a silent fail open. Caught by
  `tests/unit/test_aa1126_quota_ux.py`.
- The UI half of L1 is in AA-Partner (the Content studio lives in the CRM web app since R3).

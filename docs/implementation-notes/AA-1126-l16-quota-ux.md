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
  CIS 447) with an Upgrade link (`upgrade: true` on the bell item) to the studio's plan page. The TripPlanner notice
  `tripplanner.ai_allowance_used` is written by CIS too (S284 redesign, below).
- **S284 redesign: TripPlanner's notices reach CIS through a contract view, not a grant.** The S283 version of CIS 447
  let the `tripplanner` role INSERT into `shared.notifications` under a policy. That broke two guards on replay (CIS
  274 "shared.notifications has other policies", CIS 351 "tripplanner still holds privileges on shared.notifications")
  and the Integration shards of PR #1567 went red; the boundary is deliberate (data standards DM-5.4: cross-app lineage
  runs through a producer outbox and a contract view, a write into another app only through its HTTP API). Options
  weighed: (a) TripPlanner records the event in its own outbox and CIS pulls it through an owner view (the shape of
  `tripplanner.v_ops_*` / `v_tenant_erasure_report_v1`, root ADR 0019 pull model); (b) TripPlanner calls a CIS M2M
  route (it has no CIS M2M client: a Cognito client, a secret and Terraform for two notices, and a CIS outage would lose
  the notice unless TripPlanner kept an outbox anyway). Chosen (a): tp-034 adds the events `ai.allowance_used` /
  `ai.spend_degraded` to `tripplanner.outbox` and the view `tripplanner.v_ai_notices_v1` (SELECT to `cis_app` only);
  `services/tripplanner_notices.py` relays them on the worker's ops tick (every 5 min, the cadence of the S263 ops
  flush) into `tripplanner.ai_allowance_used` (agency bell) and `platform.tripplanner.spend_degraded` (admin bell,
  with the words), once each through the CIS 447 indexes. CIS 447 now changes no grant or policy.
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

- Migration 447 (migration-only PR; 446 shipped in S283): the five setting keys, the two once-only notice indexes, the
  CRM feature + plan rows (S284: no TripPlanner grant or policy any more). `shared/llm_client/spend_environment.py` ships in both PRs (the
  settings lint needs a reader of every enforced key; identical add/add merges cleanly).
- Code PR: `api/core/ai_allowance.py`, the four tenant routes, `v1_tours` version reads, `v1_notifications` titles,
  messages and `upgrade` / `resets_at` fields, `cost_guard` notice + unreadable bell, `tenant_budget` L4 + quota code,
  `lifecycle` tick step 4.

- Review fix pass (S283, the tech lead's "keep the admin threshold pages in sync"): the TripPlanner stage caps and the
  near-cap share are settings (CIS 447 `tripplanner.spend.stage_cap_compose_usd` / `stage_cap_search_usd`,
  `near_cap_pct`); the Apps board reads them (`api/services/spend_rules_view.py`) instead of the mirrored constants
  `app_registry.TP_STAGE_CAPS_USD` / `TP_NEAR_CAP_RATIO` (removed, one lint-baseline entry dropped). The Apps board,
  the Spend page's TripPlanner caps block and the AI gateway App profiles card now show, beside each app-wide cap,
  whether a reached cap stops the AI (dev spend rules) or only alerts (prod), from `cis.spend.environment` /
  `tripplanner.spend.environment` and the CRM's `crm.spend.environment` (relayed from crm.ops.v1 `/settings`, area
  spend, with the staff member's own token). A TripPlanner card whose spend cannot be read says "AI paused" only under
  dev. The staff bell titles the `platform.tripplanner.spend_degraded` rows the relay writes.
- S284: the code branch is no longer stacked on the migration branch (MIG-4): the 447 file and its tests left it; the
  reader `spend_environment.py` stays in both PRs (identical add/add). New: `services/tripplanner_notices.py`, the
  worker hook `Worker._tripplanner_notices` (ops tick), unit tests `tests/unit/test_aa1126_tripplanner_notices.py`,
  integration test `tests/integration/test_aa1126_tripplanner_notices_db.py` (cis_app, a stand-in of the tp-034 view),
  the RLS catalogue allowlist entry `tripplanner.v_ai_notices_v1:cis_app`.

## Tradeoffs

- The App profiles page now makes one CRM settings read per load (the CRM spend environment). It never fails the page:
  no answer = the rule is not shown for the CRM row.

- The 402 pre-check is one extra budget read per AI-starting request. Cheap (one indexed sum) and it saves a queued job
  plus a quota charge that would only stop.
- The unreadable bell writes through a fresh connection while the database may be the reason: best effort, paced,
  never raises.
- The CRM plan value seeded at 2 USD keeps Dev unchanged; the business owner's prices may set other values.

## Should know

- Renumber note: 446 is on main, this lane 447. On a renumber, `git grep '447_'` and `'447'`.
- The chain: CIS 447 and TripPlanner tp-034 applied before this PR ships. Before tp-034 the relay reads no view and
  does nothing (`ready: false`); before 447 its ON CONFLICT finds no index and the write fails (logged, retried on the
  next tick).
- The relay window is the current and the previous UTC month, a technical bound that follows the notices' own
  once-per-month / once-per-day keys, not a policy value; the relay has no batch cap (a few rows per tenant a month).
- AA-CIS-Infra `db/isolation_probe.py` CONSUMER_GRANTS and `db/owners.yaml` consumer_grants list the new view grant
  (Infra PR of this wave); the TripPlanner runtime-privileges exclusion lists the view (the runtime role has no grant).
- structlog: never pass `event=` as a keyword (it is the log message's own argument): it raised TypeError in the notice
  path, which the gateway's "never on the call path" guard would have turned into a silent fail open. Caught by
  `tests/unit/test_aa1126_quota_ux.py`.
- The UI half of L1 is in AA-Partner (the Content studio lives in the CRM web app since R3).

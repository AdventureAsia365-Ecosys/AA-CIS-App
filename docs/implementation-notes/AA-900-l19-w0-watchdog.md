# AA-900 — W0 "Funnel SQL + nightly watchdog" (lane l19, S271)

Root ADR 0020 "Measurement" layer; audit 2026-11-06-S271 writing-quality-audit, design review roadmap W0 item
"Funnel SQL + nightly watchdog", critic F15.

## Decisions

- **Store: `shared.report_run` as report CIS-08, no new table.** The report engine already has the shape a nightly
  snapshot needs (period, trigger schedule/manual, one scheduled run per report and period, job id, summary,
  `output` jsonb, Markdown), its own daily chain (kpi_snapshot_daily → report_generate from 00:30 UTC), a run page
  (/admin/reports) and its retention. `shared.writing_memory_kpi_report` was rejected: its unique key is one scheduled
  row per ISO week and its columns (soft stops, rules, project stop) belong to the writing-memory project, so a
  nightly funnel would overwrite or confuse it. A new table would have needed a migration-only PR, a DM-7.1
  retention row and an Infra copy for no gain. Cost: CIS-08 is added to the root report registry
  (docs/reports/catalog.yaml) and its byte copy here.
- **Nightly schedule = the report chain, alerts = a separate job kind.** CIS-08 is `schedule: daily, generator: cis`,
  so the existing chain renders it for each closed ICT day. A succeeded *scheduled* run queues
  `quality_funnel_watch` (idempotent per run, like `report_email` for OPS-12), which raises the findings the run
  stored. The bell therefore says exactly what the stored snapshot says, and a manual "Generate now" never alerts.
- **Alert path = `shared/ops_checks.alert_all`** (admin bell + alerts SNS e-mail, deduped). Per-stage findings
  `qf_cut.`, `qf_fb.`, `qf_drift.` are stored; what is raised is one `qf_digest` per night (second review pass,
  below). Bell window 20 h for all of them.
- **Thresholds** (constants in `services/quality/funnel.py`, documented in CONTEXT.md): window = trailing 7 days;
  cut > 5 % of the billed replies (the route_cut rule, weekly); fallback ≥ the stage's `llm_route_policy`
  `fallback_alert_pct` (default 20); served-model drift ≥ 10 % of answers from a model that served < 1 % the 7 days
  before; minimum rows (each window) = max(20, the policy's `min_requests`).
- **Second review pass (S271, independent reviewer).**
  (1) *Alert noise.* The findings are level-triggered over a trailing 7-day window, so one spike alerted up to 7
  nights per stage, and an outage multiplied by stages. Now: transition-only — `funnel.mark_repeats` flags a finding
  `repeat: true` when the previous **scheduled** CIS-08 run (newest of an earlier day, at most 7 days back,
  `funnel_report.PREVIOUS_SQL`) already had the same check (drift: and no new drifting model); repeats stay in the
  stored output and the page, and are not raised. The night's new findings are collapsed into ONE `qf_digest`
  finding (`funnel.digest_finding`, like `route_digest`), so an outage over five stages is one bell and one e-mail.
  Both were done (the reviewer offered "and/or"): transition alone still gave N bells on night 1 of an outage.
  (1b) *Route policy.* `funnel.watch_policy` uses `route_watch.effective` (own row → `*` row → defaults, field by
  field): `fallback_alert_pct` as is; minimum rows = max(`MIN_REQUESTS` 20, `min_requests`). The policy can raise
  the weekly floor, never lower it: its default 5 is sized for route_watch's 60-minute window and would make a
  weekly alert fire on 5 rows. The effective values are stored per stage (`watch.stages[].policy`).
  (2) Bell 20 h (daily job, drifting start), as `ROUTE_DIGEST_CHECK`.
  (3) AI gateway route-test rows (`quality_signal ->> 'route_test' = 'true'`, 16-token real requests that all stop
  on their ceiling) are out of STAGES_SQL, WATCH_SQL and T9_COST_SQL (`funnel.NOT_ROUTE_TEST`).
  (4) A digest result with `email == "failed"` or a database `error` makes the job raise `AlertNotDelivered`
  (retryable): `raise_alert` already deleted the bell it wrote, so the retry bells and mails again.
  (5) The page reads scheduled runs only (`LATEST_SQL`, `HISTORY_SQL` with `trigger = 'schedule'`); the newest
  manual run is returned apart as `manual_run` (meta only; its output on `GET /admin/reports/runs/{id}`).
  `history[]` also has `new_findings` (not repeats).
  (6) Expected refusals split out of `refused` in the stage table (`expected_refusals`, the
  `expected_refusal.EXPECTED_SQL` rule, as `route_digest` does) — it was a one-line change, so split rather than
  documented.
- **Definitions.** T9 delivered = approved + held (the agency receives held pieces; the label is admin-only, S270);
  S1 delivered = ever published (status approved / published, or a `published_tours` row points at the draft),
  `hitl` is held and NOT delivered; T2 delivered = a written AI version, held = `qa_status` escalated. First-pass
  yield = clean on attempt 1 / finished. Attempts: T9 = highest attempt reached (final or discarded), S1 =
  retry_count + 1 (+ 1 when the fix pass ran), T2 = qa_repair_count + 1 (T3 counts rounds from 0). LLM USD per
  delivered = the workflow's whole stage cost of the week / delivered, for all three workflows; T9 per channel can
  only use ledger rows that carry the piece id or (no piece id) its angle-gate request, so the per-channel figure
  is the attributed part, and the T9 totals add `unattributed_cost_usd` (`attributed_*` fields keep the split).
- **Review fix pass (S271).** (1) S1 first pass also excludes a draft the fix pass repaired (`fix_pass_applied`:
  flag_fix rewrites the flagged fields without raising `retry_count`) and any human touch (`human_edited`,
  `reviewed_by` / `reviewed_by_legacy`, or a `review_queue` row: a hitl draft a reviewer approved keeps retry 0).
  (2) S1 delivered was "published_tours points at it now"; `published_tours` is one row per tour and the next
  rewrite repoints it, so older weeks lost delivered drafts on every nightly recompute. process_export only
  publishes an `approved` draft and never moves it on, so "approved / published or pointed at" is "ever
  published". (3) The T9 headline used only the attributed cost (0.04 against a real 0.12 in the baseline week);
  it now uses the whole T9 stage cost, like S1 and T2.
- **Every failing gate per attempt (F15):** T9 reads the final `gate_ledger` and each `discarded_attempts[].gate_ledger`;
  `F9_cta_fact` is split into `.judge_unavailable` (degraded marker or "judge unavailable" violation), `.text_integrity`
  (TEXT_INTEGRITY violations) and the claims. The first-gate count (`held_reason` prefix) is kept for comparison.
- Rung rule, production filter and cut rule are imported from `shared/llm_client/route_watch.py` (one definition).
- Endpoint auth: `any_of(OPS_READ, LAB_READ)` (numbers only, no tenant text).

## Changed (vs the lane brief)

- No migration (decision above), so no migration-only PR; the root PR only registers CIS-08.
- The baseline is frozen from invented local test data as
  `tests/integration/snapshots/aa900_quality_funnel_baseline.json` (asserted by the integration test; regenerate
  with `AA900_WRITE_BASELINE=1`). The live Dev baseline is the first scheduled CIS-08 run.
- T2 per-attempt gate codes: not stored on `tenant_tour_versions` (only in `shared.job.result.t3`), so T2 reports
  `gate_fails: null` (follow-up).
- Expected refusals: now split out (second review pass, (6)); the stage table's `refused` is unexpected refusals
  only, `expected_refusals` the rest.
- Second review pass: the raised alert is one nightly `qf_digest` of the NEW findings, not one bell per stage
  finding; the brief's per-stage alert names remain the stored finding names.

## Tradeoffs

- Coupling to the KPI snapshot: if `kpi_snapshot_daily` fails a computer, the chain (and CIS-08) waits for its retry.
  Accepted: it already gates EX-04/OPS-12; a manual CIS-08 run works any time.
- The watchdog overlaps the 24 h `llm_route_watch` alerts on purpose: weekly-scale, per content workflow, plus drift.
- Snapshot size: 8 weeks × channels × gates; a few hundred kB at most on Dev volumes, stored once a day.

## Should know

- Transition baseline = the previous scheduled run's STORED findings. If a night's run is missing, the newest
  scheduled run within 7 days is the baseline; none in reach → everything is new. If a night's digest exhausts its
  3 attempts (SNS down for the whole retry window), the next night sees those findings as repeats and does not raise
  them; they stay visible on the page and in the run output (`repeat: false` on the night they started).
- A same-day re-render (retry of the scheduled run) compares with the night before, never with itself
  (`period_end < day`), so the flags are stable.
- `qf_cut.` / `qf_fb.` / `qf_drift.` stay registered in `PREFIX_CHECKS` (the job validates stored names with
  `check_for`; the texts document each kind) although only `qf_digest` bells now.
- The funnel's cut rate is production rows under the raw stage; `route_cut` keeps eval rows under their real stage.
  The two numbers differ on a week with eval runs, by design (CONTEXT.md).
- `tests/unit/test_aa1003_report_generate.py` counted 25 automated reports; CIS-08 makes 26 (the lane missed it,
  fixed in the review pass), and `tests/unit/test_aa882_gd3_router_samples.py` had no sample for the new
  `admin_quality_funnel` router (added). Both failed on the lane's HEAD.
- Local integration run (review pass): a throwaway Postgres 16 cluster in the scratchpad (`initdb -U cistest`, port
  55493, `CIS_TEST_DB_PORT=55493`), because the shared local DB's replay fails before the test
  (`shared.country_iso2` grant) and its `cistest` cannot create databases. `test_aa899_openapi_export` fails locally
  on HEAD too (local venv fastapi 0.110 / pydantic 2.13 vs the pinned 0.161 / 2.14); this pass changes no route
  signature, so openapi.json is unchanged.

- S1 "delivered" counts an `approved` draft whose export then failed with an unexpected error (no Master row) as
  delivered; a refused export (dead source) moves it to `hitl`, so only a crash mid-export is miscounted (the S248
  probe already reports an approved draft with no Master).

- CI Postgres has no pgvector: `content_piece.channel` and `tenant_tour_versions` are missing from the replay; the
  integration test adds them inside its rolled-back transaction. On a DB without them, `compute()` lists the part
  under `unavailable` instead of failing.
- Deploy order: root PR (catalog) and CIS PR carry the same catalog bytes; the root lint compares with `--copy`.
- `automated_from: 2026-11-07`: the register does not call earlier CIS-08 periods overdue.
- No UI page in this lane (follow-up: a Writing Lab / Reports tab over GET /admin/quality/funnel).
- Role reach of GET /admin/quality/funnel (`ops.read | lab.read`, pinned in the AA-1035 role matrix snapshot):
  admin, content_lead, tenant_qa, ai_ops. Numbers only (yield, attempts, USD per delivered object), no tenant text
  and no tenant directory or billing data, so the content-side "never money" rules hold.
- `quality_funnel_watch` is on capacity line `None` (`shared/llm_client/capacity.KIND_LINES`): it makes no LLM call,
  like `llm_route_watch`, so it is never held by the backpressure.

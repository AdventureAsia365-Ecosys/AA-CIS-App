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
- **Alert path = `shared/ops_checks.alert_all`** (admin bell + alerts SNS e-mail, deduped), new prefixed checks
  `qf_cut.`, `qf_fb.`, `qf_drift.` with a 24 h bell.
- **Thresholds** (constants in `services/quality/funnel.py`, documented in CONTEXT.md): window = trailing 7 days;
  cut > 5 % of ≥ 20 billed replies (the route_cut rule, weekly); fallback ≥ 20 % of ≥ 20 answered (the
  llm_route_policy default); served-model drift ≥ 10 % of answers from a model that served < 1 % the 7 days before,
  ≥ 20 rows in both windows.
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
- Expected refusals (no grant yet) are not separated from refused rows in the stage table (route_watch does that for
  its digest); the stage table counts every refused row.

## Tradeoffs

- Coupling to the KPI snapshot: if `kpi_snapshot_daily` fails a computer, the chain (and CIS-08) waits for its retry.
  Accepted: it already gates EX-04/OPS-12; a manual CIS-08 run works any time.
- The watchdog overlaps the 24 h `llm_route_watch` alerts on purpose: weekly-scale, per content workflow, plus drift.
- Snapshot size: 8 weeks × channels × gates; a few hundred kB at most on Dev volumes, stored once a day.

## Should know

- S1 "delivered" counts an `approved` draft whose export then failed with an unexpected error (no Master row) as
  delivered; a refused export (dead source) moves it to `hitl`, so only a crash mid-export is miscounted (the S248
  probe already reports an approved draft with no Master).

- CI Postgres has no pgvector: `content_piece.channel` and `tenant_tour_versions` are missing from the replay; the
  integration test adds them inside its rolled-back transaction. On a DB without them, `compute()` lists the part
  under `unavailable` instead of failing.
- Deploy order: root PR (catalog) and CIS PR carry the same catalog bytes; the root lint compares with `--copy`.
- `automated_from: 2026-11-07`: the register does not call earlier CIS-08 periods overdue.
- No UI page in this lane (follow-up: a Writing Lab / Reports tab over GET /admin/quality/funnel).

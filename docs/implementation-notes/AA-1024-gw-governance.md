# AA-1024 — lane gw-governance (S263): slices A4, A6, A2 (code part)

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.A (A2, A4, A6). Root ADR 0009 decisions 5 and 12.
The D7 part of the same PR (route change evidence) is in `AA-1026-gw-governance.md`.

## Decisions

- **A4 — the family rule is one module** (`shared/llm_client/family_rule.py`): `WRITER_FAMILY = "anthropic"` (decision
  12's writer clause), `family_of` reads `CatalogModel.family_key` (migration 320: `family`, else vendor), labels are
  stripped of the `satellite-` / `anthropic-api-` / `azure-openai-` prefixes. A label the catalog does not know has no
  family and never makes a conflict (the rule refuses what it can prove).
- **Admin**: the judge / writer checks in `_catalog_options` read `family_key`, not `vendor`; every refusal reason is
  English (`_REASON_*` constants). The PATCH 422 texts were already English.
- **Eval runners check twice.** Before the run, `stage_pair_conflicts` compares the writer stage's **primary** with
  **every** model of the judge route (primary + fallbacks). At call time, `trace_models` (a ContextVar list that
  `LLMClient._generate_with` appends `(role, model_used)` to after each successful route; one line in `client.py`)
  gives the models that really wrote an item, compared with the model that really judged it.
  Why both: migration 327 lets the A-series writers end on GPT-6 Luna (OpenAI API) while the judges are GPT; a static
  check over the writer's whole route would refuse every A1 run, a primary-only check would miss the fallback.
- **R2**: `check_judge_family(writer)` runs in `run_with_pool` (refuses with `R2Refused`, also when the route or
  catalog cannot be read) and hands the catalog rows to `run_r2(family_models=...)`; the DRY RUN (`estimate`) reports
  `judge_family_check` instead of refusing (it is a read-only plan). A same-family candidate / reverify-control
  verdict becomes an `error` row with `details.same_family` (counted as an error, never a verdict).
- **S1-old regression**: `check_judge_family` before any paid call (`SameFamilyJudge`), and per tour a trace; a tour
  whose writer-role and judge-role models share a family marks the run INVALID (`details.same_family_tours`), which
  `_detect_regression` already treats as "measures nothing".
- **A6**: both PATCH routes (model and route) return `warnings[]` from the same rule the nightly digest runs
  (`invariant.read` with `llm_availability.blocked_from(read_connections)`), on the saved row. Only a `breach` warns
  (a declared degrade mode passes). Report only: the change is already committed; an unreadable registry or no pool
  gives `[]`.
- **A2**: `model_tier` left the request models (`admin_pipeline.TourRunRequest`, `IngestS3Request`,
  `S1BatchSubmitRequest`, the dead `v1_pipeline.TourRunRequest`); a body with a value gets 422 through a pydantic
  "before" validator (`api/core/model_tier.py`), an explicit null is dropped (old clients). The live `s1_rewrite`
  idempotency key is now `s1_rewrite:<tour>:<batch>`, `jobs_repo.find_active_duplicate` drops the key too,
  `ingest_s1_batch` / `s1_batch_ingest_job` no longer pass a tier (the batch fallback runs on the route), and
  `generated_content.requested_tier` is written NULL for new rows.

## Changed

- No migration in this lane (the A2 `segment_research*` stage rows go to a later migration lane, per the brief).
- `services/eval/regression.py` keeps the literal `model_tier="haiku"` at the call (the
  `test_aa642_model_tier_allowlist` ratchet counts literal sites); `S1_OLD_WRITER_MODEL` names the same model for the
  family check, and a test pins that the two agree.
- Admin UI: the review Regenerate dialog no longer picks a tier (it sent the tour's `requested_tier`, which the API
  now refuses); it says the writer model comes from the `s1_generate` route.

## Tradeoffs

- The call-time check needs the gateway to record replies. A ContextVar trace is free outside an eval (one `get()`
  per call) and needs no change of the writers' return values; the alternative (reading the eval run's ledger rows
  after each item) would need a flush and a query per item.
- The R2 shadow and the AA-919 judge-compare harness (`scripts/aa919/judge_compare.py`) are not wired: the shadow
  scores production text it did not regenerate, and the harness compares judges on purpose.
- The control arm of a `rejudge` run is a stored output: its writer models are not known, so only regenerated
  outputs (candidate, reverify control) get the call-time check.
- The A2 dedup key change means a job queued before the deploy with the old key does not dedup against a new click
  for the same tour and batch (Dev: a double click across a deploy; acceptable).

## Should know

- Measure first (plan A2): the orchestrator runs the `GROUP BY model_tier` query on `shared.llm_call_log` before the
  merge (live_steps). The ledger has no `model_tier` column; the query groups by the request's `quality_signal` key
  where present and otherwise by `model` + `fallback_used` (see the PR's live steps).
- `_sonnet_upgrade` (AA-237 / AA-736) still passes `model_tier="sonnet"` internally: it is not a request field and is
  on the ratchet allowlist; it moves with S11 / A3.
- The family check reads the live catalog; on Dev today the A-series writers can only answer through GPT-6 Luna
  (acc3 Error 002, no Anthropic key), so an R2 run of a1 would end with every candidate as an `error` row
  (same family as the GPT judge) — that is the rule working, not a bug.

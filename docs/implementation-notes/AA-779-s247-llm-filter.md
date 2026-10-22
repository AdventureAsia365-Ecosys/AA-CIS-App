# AA-779 — S247: optional LLM question filter for the T9 blog FAQ (stage `t9_faq_filter`)

Earlier notes: `AA-779-s247.md` (the two $0 filter bugs), `AA-779-s245.md`, `AA-779-real-questions.md`,
`AA-779.md`. Decision record: `docs/adr/0011-t9-faq-question-filter-llm-step.md`. Agent: Claude Code.

Evidence (private root repo, `docs/audits/2026-10-22-S247-aa779-llm/`): a blind Haiku 4.5 classifier on the
200-question labelled set scored precision 0.987 (Wilson LB 0.930), recall 0.724, against main's precision
0.64; unbiased recall 0.621 against main's 0.638. Nghiệp hand-checked the 30 disagreements: the panel was
right 30/30. 29 of the 30 were over-drops of answerable questions about the piece's country or places.

## Decisions

- **Role `validate`, not `judge`.** The orchestrator brief said role `judge`. In CIS a `judge` stage must be
  non-Anthropic (ADR-2026-014/027, ADR 0006 §5, admin `_catalog_options`), and the tests pin every judge stage
  to the GPT Luna route (`test_aa714_judge_safe_defaults.py::test_every_judge_stage_is_covered`). A Haiku
  judge row would show its own primary model as "not selectable" in Settings > LLM Models and break those
  tests. The filter writes no prose and nothing scores its output, which is ADR 0008's definition of a
  `validate` stage (any vendor). So the row, `SAFE_DEFAULTS` and the `llm_call_log` rows use `validate`.
  The re-measure script logs `adhoc_aa779` with the same role for the same reason.
- **Model `haiku` (the legacy key the trial used, `satellite-haiku-4-5`), account route `acc3`, no model
  fallback.** The legacy key already fails over between satellite accounts (acc3 → acc1). A Sonnet fallback
  would decide questions with a model nobody measured; on any failure the code falls back to the
  deterministic result instead, which is the measured baseline.
- **Flag = env var `T9_FAQ_LLM_FILTER` on the worker task def** (`off` default | `shadow` | `on`), the CIS
  pattern for T-stage behaviour (`T2_JUDGE_GATE`, `T2_JUDGE_REPEAT`, `T2_JUDGE_ANCHORS`). `shadow` calls the
  model and records its verdicts but keeps the deterministic list, so live pieces can be compared before `on`.
- **`on` replaces the deterministic relevance tests, it does not AND with them.** The ship bar measured the
  classifier alone; ANDing with `paa_for_scope` would cap recall at main's. Only the mechanical clean-up
  (`normalize_paa_question`: citation tags, 10–160 chars, capital, `?`, folded dedupe) runs before the model.
  The FAQ droppers after the writer (`drop_unanswerable_faq`, `drop_restated_faq`, …) still run unchanged.
- **Review fix (S247): `on` post-filters the model's KEEP list with the deterministic gazetteer test**
  (`names_place(q, g) for g in out_of_scope`, the same test `paa_for_scope` runs). The model only sees up to 30
  of the trip's other places (`context['trip_places']`), the gazetteer sees all of them, and a false keep naming
  another trip place would reach a customer-facing FAQ. The verdict rows keep the model's own labels; the
  removed questions are listed in `writer_input.faq_filter.gazetteer_dropped` and counted in the call-log row.
  The cap of 6 applies after this drop. `llm_filter_questions(out_of_scope=())` (the default) still measures the
  classifier alone, so the re-measure can report both variants.
- **Review fix (S247): the call-log rows follow AA-927.** A usable reply writes one row with `json_parsed: true`
  (the stage's ok-rate key) plus `keep` / `drop_reasons` / `gazetteer_dropped`. A billed reply that is unusable,
  or ANY raise between `generate()` and the row write, writes one `billed_unusable` row via
  `record_unusable_sync` (no ok-rate key) and re-raises, so the caller falls back. The call site is registered in
  `tests/unit/test_aa927_llm_call_site_registry.py::LOGGED_CALL_SITES`.
- **One call per piece**, all candidates (≤ 30 = `CHAIN_MAX`) in one numbered list; the per-piece cap of 6
  (`PAA_MAX_QUESTIONS`) is applied to the model's KEEP list in candidate order (segment / place questions first).
- **Prompt = the trial prompt with a calibration folded into its rules** for the over-drop the hand check
  found (country as a destination: food, money, dress code, souvenirs, landscape, what it is known for;
  general travel facts: distances / travel times, visit length, booking ahead, wildlife a park is known for,
  what a monument enshrines; visitor safety "right now" and hazards near a site; an empty source is no reason
  to drop) and the one false keep (country trivia whose answer is another place). A first version appended a
  separate calibration block (+2.1k chars); it was folded into the existing clauses because the system prompt
  is most of the input tokens (estimate for the 200-question re-measure: $0.218 → $0.188, under the $0.25
  cap with room for the trial's +15% actual-vs-estimate). The note is capped at 10 words (trial: 20).
- **No eval-set question in the prompt.** The first draft quoted five of the 200 labelled questions as
  examples; they were replaced by held-out ones (Peru, Petra, Marrakech) so the re-measure is not a test on
  its own examples. The categories still come from the trial's misses on that set, so the re-measured recall
  is optimistic; `shadow` on new live blogs is the held-out check.

## Changed

- New `services/acp_content_writing/faq_filter.py`: mode, scope view, prompt, parser, `llm_filter_questions`
  (sync, gateway call + `llm_call_log` row) and `select_faq_questions` (async; deterministic first, then the
  model per mode, timeout + any error → deterministic).
- `quality_gates.py`: the question clean-up of `paa_for_scope` moved into `normalize_paa_question` (no
  behaviour change) so the filter cleans questions the same way.
- `service.py` (`run_write_background`): the blog's `paa_questions` come from `select_faq_questions`; the
  filter record (`faq_filter`) goes into `writer_input` (admin-only).
- `shared/llm_client/role_config.py`: `SAFE_DEFAULTS["t9_faq_filter"]`.
- Migration `api/migrations/264_t9_faq_filter_stage.sql`: the `shared.llm_role_config` row (ON CONFLICT DO
  NOTHING). 262/263 are reserved for another lane.
- Tests: `tests/unit/test_aa779_faq_llm_filter.py`.

## Tradeoffs

- Cost: up to ~$0.005 per blog (≤ 30 questions, ~1.5k tokens in, ≤ ~1k out on Haiku 4.5) when `shadow` or
  `on`; $0 when `off` or when a piece has no real questions. The brief's ~$0.001 assumed the trial's 8-question
  batches; production sends the whole candidate list in one call.
- Latency: one extra Haiku call (~2–4 s) before the writer, only when the flag is not `off`.
- `on` replaces the deterministic relevance tests with the model but keeps the out-of-scope gazetteer test as a
  post-filter (review fix). It can only lower recall versus the classifier alone, and only on questions naming
  a trip place outside the piece, which the prompt's own rule (`outside_piece_scope`) says to drop anyway. The
  trial measured the classifier alone; the re-measure should report both (`out_of_scope=()` and the gazetteer).

## Should know

- **Migration order.** Do NOT apply 264 on Dev before 262 (AA-859, `262_tenant_brand_seo_rls.sql`) and 263
  (reserved, AA-859 lane). `api/core/readiness.py` compares `max(version::int)` with `REQUIRED_CIS_SCHEMA`; 264
  applied first would hide a missing 262/263 from `/health/ready`, the deploy smoke and the Platform Apps page
  once code requires them. The row is optional (`SAFE_DEFAULTS` mirrors it): the flag works without it. Written
  into the migration header too; the Infra copy must be taken from this final file.

- Nothing changes until the migration is applied and `T9_FAQ_LLM_FILTER` is set on the worker task def.
  Without the migration the gateway uses `SAFE_DEFAULTS` (same row), so the flag alone also works.
- The re-measure script (not in this repo): root `.tmp-session/aa779-remeasure.py` (`--build` locally embeds
  this branch's `faq_filter.py` + the blind set; `--run` in the api container, detached, $0.25 hard cap, stage
  `adhoc_aa779`, model `haiku`; `--score` locally computes B1–B5 like the trial's `ship_bar.py`). It works before
  this branch is deployed: it attaches `normalize_paa_question` when the deployed `quality_gates` lacks it.
  Checked offline with a fake model (106 calls, 200 verdicts, scoring path); not run against Bedrock.
- No new gate (T9 rule "no new gates for 4 weeks"): this only chooses which real questions the writer is given.

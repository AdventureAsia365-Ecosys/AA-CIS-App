# AA-779 — S247: LLM answer-fit check for the T9 blog FAQ (stage `t9_faq_answer_fit`)

Earlier notes: `AA-779-s247-llm-filter.md` (the question filter, stage `t9_faq_filter`), `AA-779-s247.md`,
`AA-779-s245.md`, `AA-779-real-questions.md`, `AA-779.md`. Decision record: `docs/adr/0011-t9-faq-question-filter-llm-step.md`
(still Proposed, now covers both steps). Agent: Claude Code.

Evidence (private root repo, `docs/audits/2026-10-22-S247-aa779-llm/checkpoint8-panel.json`): on the live 8-blog
checkpoint a 3-judge panel read 31 FAQ pairs. 3 do not answer their question, unanimously:
"What is the average altitude of Mongolia above sea level?" (answered with Mörön's elevation, wrong subject),
"What is Paro called in English?" (no English name given, non-answer) and the writer-invented
"What is the Bhutan Institute of Performing Arts?" (answered with the Zorig Chusum crafts institute, wrong subject).
25 pairs are unanimous "answers"; 3 are split 2-1 with "partial". The deterministic `faq_answer_problem`
(subject-token test) passed all three: each answer names a place or "the institute", so a token test cannot
see that the subject or the asked-for fact is wrong.

## Decisions

- **A repair/drop step, never a gate.** T9's standing rule is no new gates. The step only removes FAQ pairs; it
  never holds, blocks, rewrites or regenerates. It runs once per piece, after the attempt loop, on the text that is
  stored (after the citation-tag strip, before `_final_integrity`), so the model sees clean text and the final
  integrity pass still reads the result.
- **Same flag as the question filter: `T9_FAQ_LLM_FILTER`** (`off` | `shadow` | `on`, `faq_filter.faq_filter_mode`).
  One switch for the whole AA-779 LLM layer: `shadow` records both steps, `on` applies both. A separate flag would
  allow "filter on, fit off" combinations nobody measured.
- **One call per piece, every pair** (`LLMClient.generate`, stage `t9_faq_answer_fit`, role `validate`): the same
  ADR 0008 reasoning as `t9_faq_filter` (a classification nobody scores; a `judge` stage must be non-Anthropic).
  At most `FIT_MAX_PAIRS` = 10 pairs are sent (the FAQ cap is 5); a pair past that is never dropped.
- **Input = question + answer only.** The check asks "does this answer answer this question?", not "is it true?"
  (truth is F1/F9's job). Leaving the body and seed out keeps the call small and makes the offline eval (which only
  has the 31 Q/A pairs) see exactly what production sends.
- **Verdicts `ANSWERS` | `NON_ANSWER` | `WRONG_SUBJECT`**, each with a ≤ 15-word reason. A partial or general answer
  is `ANSWERS` and "when unsure, ANSWERS": a drop must be a clear failure, because a wrongly dropped good pair costs
  FAQ content and the eval bar allows at most 1 false flag in 25.
- **`on` with fewer than `FAQ_MIN_PAIRS` left:** the failing pairs are still dropped (a FAQ answer that answers a
  different question is worse than a shorter FAQ), the piece keeps its status, and the record says `below_min: true`
  plus an admin-only `F7_faq_autofix` line. No regeneration (the spec: never regenerate). If every pair fails the
  FAQ section is removed (`replace_faq_section(text, [])`), recorded the same way.
- **Fail open.** A gateway error, an unusable reply or the 60 s timeout leaves the FAQ unchanged and records
  `fallback: true` with the error. A billed unusable reply writes its `billed_unusable` row (AA-927).
- **Writer-invented questions are checked too**: the step reads every pair of the final FAQ, whatever its source.

- **Migration number 266, not 265.** AA-859 wave 4 is written in parallel and may take 265; 266 avoids a clash.
  Apply order: 262, 263, 264, 265 (AA-859) first, then 266 (the readiness check reads `max(version)`). The row is
  optional (`SAFE_DEFAULTS` mirrors it), so holding 266 back costs nothing. `REQUIRED_CIS_SCHEMA` is not raised.
- **Embedding and summary are not recomputed after an `on` drop.** Removing 1–2 FAQ pairs barely moves the
  piece's embedding (used for F10 / within-tenant reuse); recomputing would add an embedding call per drop.

## Changed

- New `services/acp_content_writing/faq_answer_fit.py`: prompt, `build_user_prompt`, `parse_fit_verdicts`,
  `check_answer_fit` (the gateway call + AA-927 rows), `apply_answer_fit` (flag modes, drop, fail open),
  `fit_log_lines` (admin F7 lines).
- `service.py`: `_apply_faq_answer_fit` (stores `context["faq_answer_fit"]`, adds the F7_faq_autofix lines to the
  attempt's existing entry or a new one, returns the cost added to `total_cost`), called for `channel == "blog"`
  between the citation-tag strip and `_final_integrity`; `writer_input_snapshot` carries `faq_answer_fit` when set.
- `shared/llm_client/role_config.py`: `SAFE_DEFAULTS["t9_faq_answer_fit"]` (validate, claude, haiku, acc3).
- `api/migrations/266_t9_faq_answer_fit_stage.sql`: the stage row, same shape as 264.
- `tests/unit/test_aa927_llm_call_site_registry.py`: the new call site `faq_answer_fit.check_answer_fit`.
- `tests/unit/test_aa779_faq_answer_fit.py`: flag off / shadow / on, writer questions, cap, error / unusable /
  timeout fail open, the llm_call_log row (stage, role, ids, `json_parsed`), parser, service glue, the call's place
  in `run_write_background`, SAFE_DEFAULTS vs migration 266.
- ADR 0011 (still Proposed): context, decision 6, cost, the answer-fit eval bar (result pending), rollback.
- Root repo `.tmp-session/aa779-fit-eval.py` (not in this repo): `--build` / container `--run` (cap $0.10, stage
  `adhoc_aa779`) / local `--score` against the panel.

## Tradeoffs

- **Drop vs keep at the minimum.** `drop_subjectless_faq` keeps failing pairs at the 3-pair floor and lets the one
  regeneration fix them. This step runs after the loop, where a regeneration is not allowed (spec: never
  regenerate), so the choice is "a short FAQ" or "an answer to a different question". The customer-facing FAQ
  prefers short; `below_min` makes it visible in the admin record and the F7 flag.
- **After the loop, not inside it.** Inside the attempt loop the call could run up to 3 times per piece (one per
  attempt) and its drops would be re-judged by gates that never needed it. After the loop it is one call per
  piece on the text that ships. The F16_faq ledger entry is the gates' view before the drop; `below_min` is the
  record of what the drop did.
- **Q/A only vs Q/A + source.** Sending the seed and body would let the model also catch an unsupported answer, but
  that is F1/F9's job, triples the tokens, and the eval set has no per-pair source. Fit only.

## Should know

- Eval: `python3 -I .tmp-session/aa779-fit-eval.py --build` (root repo, $0; re-run it after the last commit so the
  embedded `fit_sha` / `worktree_head` match), upload `.tmp-session/aa779-fit-eval.run.py`, run it in the CIS api
  container with `--run` (8 calls, ≈ $0.013 estimated, cap $0.10), then `--score <downloaded json>` locally.
  The container script stubs the module's flag import, so it runs against an image without this branch.
- The flag is shared: turning `T9_FAQ_LLM_FILTER` to `shadow` now records both `writer_input.faq_filter` and
  `writer_input.faq_answer_fit`; `on` applies both. Do not turn `on` before the eval bar passes (ADR 0011).
- Held pieces are checked too: a held piece still reaches the tenant (AA-770), so its FAQ ships the same way.

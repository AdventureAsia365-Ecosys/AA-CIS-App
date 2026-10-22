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
  FAQ section is removed, recorded the same way.
- **Review fix 1 (S247): the drop cuts blocks, it does not rebuild the FAQ.** The first version rebuilt the FAQ
  with `replace_faq_section(text, kept)`, which keeps only each answer's first paragraph and moves every other
  paragraph above `## FAQ`; a dropped wrong-subject answer's second paragraph landed in the body (and the new text
  reached `_final_integrity`, which can hold). New `quality_gates.drop_faq_pairs(text, {n})` cuts each dropped
  pair's whole `_FAQ_PAIR_RE` block plus the blank line after it and keeps every other byte in place (the
  `drop_unanswerable_faq` approach, without its rebuild). Numbering matches `faq_pairs`. Exception: a failing LAST
  pair with more than one paragraph is kept and listed in `kept_multi_paragraph` (+ an F7 line), because the last
  block runs to the end of the text and its tail may be a CTA written after the FAQ; guessing would either delete
  a CTA or glue the wrong-subject tail onto the previous pair. `pairs_after` is now counted on the result text.
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
- **Review fix 4: Dev is already in `shadow`.** The Dev worker task definition has `T9_FAQ_LLM_FILTER=shadow`, so
  merging this starts answer-fit shadow calls on the next blog (~$0.0016 each, FAQ unchanged). The real risk:
  flipping the shared flag to `on` for the (measured) question filter also turns answer-fit drops on. ADR 0011
  "Rollout" now says: do not set `on` until the E1–E3 result and a read of new-blog shadow verdicts are in the ADR;
  if the question filter must go `on` first, split the flag first (code change). Put the same line in the PR body:
  "Do not set T9_FAQ_LLM_FILTER=on until the answer-fit E1–E3 result is in ADR 0011." and in the root backlog row.
- **Review fix 2: the eval is in-sample.** The prompt's examples are new, but its rules were written from the 3
  checkpoint failures, so E1 is optimistic on recall. Written into ADR 0011 like decision 5; `on` also needs
  shadow verdicts on new blogs.
- **Review fix 3 (deferred, conditional): predicted E2 risk.** The "one part instead of the whole" clause may flag
  the pair on northern Mongolia's ecological zones (answered via Lake Khövsgöl) and the pair on the ethnic groups
  along a route (answered via Phongsali Province); two false flags fail E2. Predicted E1 miss: the Paro
  English-name pair ("referred to as Paro town" may read as a name). The prompt is NOT changed before the run (the reviewer asked for it only if the eval
  misses, and tuning it on the checkpoint pairs before measuring would make the eval more in-sample still). If E2
  fails, replace the clause with: "the question asks for a figure or fact about the whole, and the answer gives it
  only for one part (asked a country's average altitude, answered with one town's)"; add "an answer about a part
  that still answers what was asked about the whole ANSWERS"; examples off the checkpoint pairs; rebuild and re-run.
- **Review fix 5: the eval cap is worst-case.** `aa779-fit-eval.py` counts each attempt (retries too) at
  `max(reported cost_usd, worst case)` where worst case = input at chars/3 tokens + all `MAX_TOKENS` (1,500) billed
  as output (~$0.0087 a call), and makes an attempt only if that worst case still fits under $0.10. A reply
  reporting cost 0 / none can no longer hide spend. `spent_usd` is still the reported cost; `budget_counted_usd`
  is what the cap counted. The container loader stubs the new `quality_gates.drop_faq_pairs` when the deployed
  image lacks it (the eval only calls `check_answer_fit`).
- Held pieces are checked too: a held piece still reaches the tenant (AA-770), so its FAQ ships the same way.

## S247 eval round 1 (in-sample) and the prompt revision

- **Result: FAIL** (`.tmp-session/aa779-fit-eval-score.json`, root repo; 8 calls, $0.0147, 0 errors). E1 2/3:
  missed "What is Paro called in English?" (`ANSWERS`: "explains that Paro refers to district and town"). E2 2/25
  false flags, both `WRONG_SUBJECT` and both judged against the BLOG topic, not the pair's own question: "Do I
  need a visa to visit Laos?" ("question asks about Kuang Si Falls") and "What ethnic groups live in the villages
  along this route?" ("answer discusses Phongsali Province and Nam Ou valley instead"). The predicted Review-fix-3
  risk (the "one part instead of the whole" clause on the Khövsgöl ecology pair) did not show; the Paro miss was
  predicted.
- **Decision: revise the prompt generically, not with the Review-fix-3 text.** That fix targeted a failure that
  did not happen. The new prompt (a) judges each pair only against its own question and says the blog topic, the
  other pairs and the question's fit to the post do not matter; (b) lets a country/region question be answered
  at that level; (c) says a "this route / this trip / the area" question is answered by naming the route's
  places; (d) makes a question asking for a specific datum (name, translation, number, date, yes/no) need that
  datum stated or a plain statement that it does not exist, explaining around it = `NON_ANSWER`; (e) keeps
  `WRONG_SUBJECT` = another thing, or a whole answered with a smaller place's fact. The named examples (Great
  Wall, Mount Fuji, a market) were removed: no example, place or question from the checkpoint or held-out sets.
  The prompt is shorter (~1.6k chars). Output format, verdict names, parser and code are unchanged.
- **Tradeoff: the in-sample eval is now even more in-sample.** Every new rule comes from the 3 round-1 errors,
  so passing E1–E3 again shows only that the rules took. The decision rests on the held-out set: 40 FAQ pairs
  from 12 older blog pieces (`.tmp-session/aa779-heldout/heldout.json`, read-only export, checkpoint requests
  excluded), labelled by a separate panel the prompt author did not read.
- **Eval script (root `.tmp-session/aa779-fit-eval.py`):** `--build --heldout <pairs.json>` adds one call per
  held-out piece (grouped per `piece_id`, pairs in `n` order, as in production) after the 8 checkpoint calls, in
  the same run under the same $0.10 cap. `--score <out> --heldout-labels <judges.json>` scores the held-out set
  separately: E1-held flags >= 80% of unanimous non-answers (n/a when there is none), E2-held false-flag rate on
  unanimous answers <= 5%, E3-held no errors / missing. Unanimous = every judge in the labels file, at least 2.
- **Cap accounting changed so 20 calls fit.** Round 1 counted every finished call at its worst case (~$0.008),
  which would stop a 20-call run at about 11 calls. Now an attempt still starts only if its worst case fits, but
  a finished one is counted at max(reported cost, reported tokens priced at Haiku rates with every input token at
  the 1.25x cache-write rate); no tokens and no cost, or a raise before any reply, counts the worst case. A
  wrapper client keeps the last reply, so an unusable (billed) reply is counted by its tokens too.

## S247 eval round 2 (both sets) and the round-3 plan (isolated mode, eval variants)

- **Round 2 result: FAIL** (`.tmp-session/aa779-fit-eval-r2.json`, root repo; 20 calls, $0.033, 0 errors; batched
  on Haiku, revised prompt). In-sample E1 2/3 ("What is Paro called in English?" still `ANSWERS`), E2 2/25 (the
  same topic bleed: "Do I need a visa to visit Laos?" → "not Kuang Si Falls-specific", and the "this route"
  question). The rule "judge each pair only against its own question" did not stop the model inferring the blog
  topic from the other pairs.
- **Scorer bug (fixed).** The held-out score said "0 unanimous non-answers, E1-held n/a": `panel_classes` counted
  only the kinds `non_answer` / `wrong_subject`, and the held-out judges wrote `partial` / `off_subject`. Now a
  pair is a unanimous non-answer when every judge says `answers_question: false` (kinds may differ), a unanimous
  answer when every judge says true, split otherwise; the same rule for both sets (the checkpoint panel gives the
  same 3 / 25 / 3 classes under both rules, checked). Re-scored round 2
  (`.tmp-session/aa779-fit-eval-score-r2-rescored.json`): E1-held 4/7 = 57% FAIL, E2-held 1/31 = 3.2%, E3-held pass.

### Decisions

- **Isolated mode = one call per pair, same verdict format.** The model gets only that Q and A
  (`ISOLATED_SYSTEM_PROMPT`: same `_VERDICT_RULES`, an intro for one pair, the same `{"items": [...]}` reply with one
  item, parsed by the same `parse_fit_verdicts(content, 1)`), header `FAQ PAIR:`, `ISOLATED_MAX_TOKENS` 150. The
  batched `SYSTEM_PROMPT` is rebuilt from the shared rules and stays byte-identical to round 2 (sha1 checked), so
  round 2 and round 3 batched results are comparable.
- **The mode is a module constant, `FIT_MODE` (default `batched`), named in ADR 0011**; a unit test checks the ADR
  line `FIT_MODE = "<value>"` matches the code. The production default is chosen after round 3 (ADR rule: B passes
  → `isolated`; only A → stay `batched` and move the stage row to Sonnet; neither → no `on`).
- **No model id in code.** `model_tier` stays None in production (the stage row, migration 266, decides); the eval
  passes a tier, as `aa779-remeasure.py` passes `"haiku"`. Variant A uses tier `"sonnet"`, which LLMClient accepts
  as a legacy key (`shared/llm_client/catalog.py` LEGACY_KEYS; satellite Sonnet, $3/$15).
- **Concurrency:** `ThreadPoolExecutor(max_workers=min(FIT_CONCURRENCY=3, pairs))`, each call in
  `contextvars.copy_context().run` (the `brand_fit._judge_parallel` pattern, AA-801), one shared LLMClient.
- **Fail open per pair.** A pair whose call raises, whose reply is unusable (its `billed_unusable` row is written,
  its billed cost still added to `cost_usd`), or with no reply by `PAIR_DEADLINE_S` (50 s, inside the 60 s step
  timeout) gets `verdict: None` + `error`, is kept, and is listed in the record's `pair_errors`; the others still
  count. `FitAllPairsFailed` (→ `fallback: true`, FAQ unchanged) only when no pair got a verdict. The pool is shut
  down with `wait=False, cancel_futures=True`, so a slow pair never holds the step past its deadline.
- **llm_call_log: one row per call.** The generate + row code moved into `_fit_call` (used by both modes), so the
  AA-927 registry entry is now `faq_answer_fit._fit_call`. Isolated rows carry `fit_mode: isolated`, `n_pairs: 1`,
  `pair_n`, `piece_pairs`; batched rows carry `fit_mode: batched`.

### Changed

- `faq_answer_fit.py`: `FIT_MODE*`, `FIT_CONCURRENCY`, `ISOLATED_MAX_TOKENS`, `PAIR_DEADLINE_S`, `_VERDICT_RULES`,
  `ISOLATED_SYSTEM_PROMPT`, `build_request`, `_fit_call`, `_check_isolated`, `FitAllPairsFailed`;
  `check_answer_fit(..., fit_mode=, concurrency=, deadline_s=)`; `FitOutcome` gains `fit_mode`, `calls`, `errors`
  (isolated rows also carry `model`); `apply_answer_fit(..., fit_mode=)` records `fit_mode`, `calls` and
  `pair_errors`.
- Tests: 13 new test functions, 15 cases (per-pair calls see only their pair, one row per call, on-drops, a
  failing / unusable / late pair kept while the others count, all failing → fallback, the concurrency bound at
  1/2/3 and the default, contextvars, tier override, unknown mode, the ADR names `FIT_MODE`); the no-FAQ record
  now has `fit_mode`.
- Root `.tmp-session/aa779-fit-eval.py` (round 3): variants A (batched, `sonnet`) and B (isolated, `haiku`), both sets
  in one run, cap $0.25, a thread-safe metering wrapper (cap gate per attempt incl. the in-flight worst cases,
  `BudgetStop`), retries (batched: the piece; isolated: the failed pairs), per-variant stats (models, fallback_used),
  the fixed scorer, per-variant/per-set report, a model-mismatch check (variant invalid if Sonnet fell back to
  Haiku). Rebuilt `--build` → `.tmp-session/aa779-fit-eval.run.py` (estimate $0.14: B $0.053, A $0.087). Not run.

### Tradeoffs

- **Isolated costs more input** (the ~1.5k-char system prompt is sent once per pair): ~$0.0027 per blog on Haiku vs
  ~$0.0016 batched. Still cheaper than batched on Sonnet (~$0.0044). Latency is bounded by the concurrency (≤ 5
  pairs → 2 rounds of 3).
- **Isolated loses cross-pair context on purpose.** That context is what caused the false flags; nothing in the
  verdict rules needs another pair.
- **Run order B then A**: if the cap stops the run, the cut lands on the expensive variant, not on the new mode.

### Should know

- The held-out non-answers are mostly `partial` (5 of 7), and the prompt says a partial answer `ANSWERS`. Round 3
  may fail E1-held for both variants for that reason alone; the ADR says the next step is then a decision on what
  a drop is for, not another wording pass on the held-out pairs.
- Run order for round 3: `--build` (done, re-run after the last commit so `fit_sha` / `worktree_head` match the
  commit), upload `.tmp-session/aa779-fit-eval.run.py`, dry run, `--run` in the CIS api container, download
  `s3://…/scripts/out/s247/aa779-fit-eval-r3.json`, `--score <file>` locally (held-out pairs + labels default to
  `.tmp-session/aa779-heldout/`), write the result and the chosen `FIT_MODE` into ADR 0011.

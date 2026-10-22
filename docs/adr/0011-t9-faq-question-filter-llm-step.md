# T9 blog FAQ: LLM steps filter the real search questions and check the answers fit, behind a flag (AA-779)

- **Status:** Proposed (S247).
- **Builds on:** ADR 0005 (model catalog), ADR 0006 (stage routes), ADR 0008 (validate stages may use any
  vendor). No ecosystem contract changes: the stage is a CIS row in `shared.llm_role_config`, read only by CIS.

## Context

A blog's FAQ may answer real search questions: People Also Ask and the piece's own Segment / place questions
(`services/acp_shared/real_questions.py`). A deterministic filter (`quality_gates.paa_for_scope`: scope
gazetteer, unanswerable topics, off-meaning names, shared seed words) chooses which ones the writer gets. On the
S246 labelled set (200 questions, three-labeller panel, kappa 0.913) it reaches precision 0.64. The FAQ is
customer-facing, so a wrong question costs more than a missing one.

S247 trial (root repo audit `docs/audits/2026-10-22-S247-aa779-llm/`): a blind Claude Haiku 4.5 classifier, one
call per scope, $0.21 for the 200 questions:

| | precision (Wilson LB) | recall | unbiased recall |
|---|---|---|---|
| main (deterministic) | 0.64 | — | 0.638 |
| Haiku classifier | 0.987 (0.930) | 0.724 | 0.621 |

All five ship bars passed. Nghiệp hand-checked the 30 disagreements: the panel was right 30/30. Twenty-nine were
over-drops: answerable questions about the piece's own country or places that the model called "unanswerable"
or "outside_piece_scope" ("Is it safe to trek in Nepal right now?", "What is the most famous food in Laos?").

T9 has a standing rule: no new gates for 4 weeks.

**Answer fit (added S247, after the live 8-blog checkpoint).** A 3-judge panel read the 31 FAQ pairs of 8 live
blogs (root audit `checkpoint8-panel.json`). Three answers do not answer their question, unanimously:

- a country's average altitude answered with one town's elevation (wrong subject; the source has no national
  figure);
- "What is <place> called in English?" never giving an English name (non-answer);
- a writer-invented question about one institute answered with a different one (wrong subject).

Three more were split 2-1 as "partial". The deterministic subject test (`quality_gates.faq_answer_problem`) passed
all three failures: each answer names a place or "the institute", so a token test cannot see that the subject or
the asked-for fact is wrong.

## Decision

1. Add an optional LLM step that **filters the questions** before the writer sees them
   (`services/acp_content_writing/faq_filter.py`). It is not a gate: it never holds, blocks or rewrites a piece,
   it only chooses which real questions the writer is given. The FAQ droppers after the writer are unchanged.
2. **Model Gateway stage `t9_faq_filter`**, role `validate`, Claude `haiku` (the key the trial measured),
   account route `acc3`, no fallback model, no shadow (migration 264, `SAFE_DEFAULTS`). One call per blog piece
   with every candidate question (at most 30) in one numbered list. Each reply writes one `shared.llm_call_log`
   row in the AA-927 shape: `json_parsed: true` when usable, `billed_unusable: true` (no ok-rate key) when not.
   - `validate`, not `judge`: a judge must be non-Anthropic (ADR 0006 §5). This step writes no prose and nothing
     scores its output, which is the ADR 0008 case.
3. **Flag `T9_FAQ_LLM_FILTER`** on the worker task definition. The default is `off`.
   - `off`: today's deterministic list and no model call.
   - `shadow`: the model runs and its verdicts are stored in `writer_input.faq_filter` (admin-only), but the writer
     still gets the deterministic list.
   - `on`: the writer gets the model's KEEP list, in candidate order, minus any question that names a trip place
     this piece does not cover (the deterministic gazetteer test of `paa_for_scope`), capped at 6. The model sees
     at most 30 of those places; the gazetteer sees them all. The count is recorded as `gazetteer_dropped`.
4. **Fallback.** On a gateway error, an unusable reply or a 60 s timeout, the writer gets the deterministic list.
   The step never fails T9.
5. **Prompt.** The trial prompt, with calibration clauses folded into its rules for the over-drops the hand
   check found:
   - the piece's country as a destination;
   - general travel times, visit lengths and wildlife chances;
   - visitor safety asked "right now";
   - an empty source text is no reason to drop.

   The clauses also cover the one false keep, country trivia whose answer is another place. Their examples are
   kept out of the labelled set, so a re-measure on that set is not a test on its own examples.
6. **Answer fit (S247): a drop step after the FAQ is final** (`services/acp_content_writing/faq_answer_fit.py`).
   - Where: once per blog piece, after the attempt loop, on the text that is stored (after the citation-tag strip,
     before the final integrity pass). Every final pair is checked, whatever its source: a real search question
     or a writer-invented one.
   - What: ONE call over all the pairs. It reads the question and the answer only, because it judges fit, not
     truth (F1/F9 own truth). Each pair is marked `ANSWERS`, `NON_ANSWER` or `WRONG_SUBJECT` with a short reason.
     A partial or general answer is `ANSWERS`, and "when unsure, ANSWERS".
   - Stage `t9_faq_answer_fit`, role `validate`, Claude `haiku`, `acc3`, no fallback, no shadow (migration 266,
     `SAFE_DEFAULTS`). One `shared.llm_call_log` row per reply in the AA-927 shape.
   - Same flag `T9_FAQ_LLM_FILTER`. `off`: no call. `shadow`: verdicts in `writer_input.faq_answer_fit`, FAQ
     unchanged. `on`: the failing pairs are dropped. If fewer than `FAQ_MIN_PAIRS` (3) are left, the piece keeps
     its status and the record says `below_min` (plus an admin-only `F7_faq_autofix` line). It never holds and
     never regenerates: it is a repair step, not a gate.
   - Fallback: on a gateway error, an unusable reply or a 60 s timeout the FAQ is unchanged and `fallback: true`
     is recorded.

## Cost

When the flag is `shadow` or `on`, about $0.005 per blog at most (30 questions, ~1.5k tokens in, ≤ 1k out on
Haiku 4.5). The S247 trial batches of ~8 questions cost ~$0.002 per call. When the flag is `off`, or a piece has no
real questions, the cost is $0.

The answer fit adds one call per blog, about $0.0016 (≤ 5 pairs, ~1.2k tokens in, ≤ 200 out). The offline eval
estimate is $0.013 for the 8 checkpoint blogs. The cost is $0 when the flag is `off` or the blog has no FAQ.

## Measurement bar (before `on`)

Re-run the S247 ship bar with this prompt and code on the same 200 questions: stage `adhoc_aa779`, a $0.25 cap,
`.tmp-session/aa779-remeasure.py` in the root repo. Turn the flag to `on` only if all five bars pass:

- B1: precision ≥ 0.90 and Wilson lower bound ≥ 0.80.
- B2: recall ≥ 0.70.
- B3: unbiased recall is no more than 0.05 below main's.
- B4: at most 2 false keeps that are sensitive or homonym questions.
- B5: no call or parse errors.

Before `on`, `shadow` on a few live blogs is the cheap check that production scopes behave like the set.

The calibration clauses were written from the trial's misses on this same set. Their examples are not set questions,
but its categories come from them, so the re-measure is optimistic on recall. A held-out check is still needed:
new real questions from the next blogs, read in `shadow`.

### Answer-fit eval bar (before `on`)

The eval runs offline on the 31 checkpoint pairs, grouped per piece: 8 calls, stage `adhoc_aa779`, role
`validate`, a $0.10 cap, `.tmp-session/aa779-fit-eval.py` in the root repo. It sends the question and the answer
only, the same input production sends. Bars:

- E1: flags all 3 unanimous non-answers.
- E2: flags at most 1 of the 25 unanimous "answers" pairs.
- E3: no call or parse errors.

The three 2-1 "partial" pairs are reported but are not in the bar. The prompt's examples are not checkpoint pairs.

**Result: pending.** The orchestrator runs it in the CIS api container; fill it in here before `on`. 31 pairs is a
small set, so `shadow` on new live blogs is the held-out check, as for the question filter.

## Rollback

Set `T9_FAQ_LLM_FILTER=off` (no deploy of code; the worker picks up the task definition). To remove the stage
row: `DELETE FROM shared.llm_role_config WHERE stage = 't9_faq_filter'` (the SAFE_DEFAULTS entry is harmless
while the flag is off).

The answer fit uses the same flag, so `off` turns both steps off; there is no separate switch. Its row goes with
`DELETE FROM shared.llm_role_config WHERE stage = 't9_faq_answer_fit'` (migration 266 rollback).


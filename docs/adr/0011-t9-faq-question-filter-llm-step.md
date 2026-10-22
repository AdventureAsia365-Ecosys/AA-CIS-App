# T9 blog FAQ: LLM steps filter the real search questions and check the answers fit, behind a flag (AA-779)

- **Status:** Accepted (22/10/2026, S247, Nghiệp). `on` follows the rollout rule below: a read of new-blog shadow verdicts first.
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
   - What: the model reads the question and the answer only, because it judges fit, not truth (F1/F9 own
     truth). How the pairs are sent is the call mode `faq_answer_fit.FIT_MODE` (see "Answer-fit call mode"
     below): `batched` = ONE call over all the pairs, `isolated` = one call per pair. Each pair is marked `ANSWERS`, `NON_ANSWER` or `WRONG_SUBJECT` with a short reason.
     A partial or general answer is `ANSWERS`, and "when unsure, ANSWERS".
   - Stage `t9_faq_answer_fit`, role `validate`, Claude `haiku`, `acc3`, no fallback, no shadow (migration 266,
     `SAFE_DEFAULTS`). The code never names a model: the stage row decides. One `shared.llm_call_log` row per
     call in the AA-927 shape (isolated: one row per pair).
   - Same flag `T9_FAQ_LLM_FILTER`. `off`: no call. `shadow`: verdicts in `writer_input.faq_answer_fit`, FAQ
     unchanged. `on`: the failing pairs are dropped. If fewer than `FAQ_MIN_PAIRS` (3) are left, the piece keeps
     its status and the record says `below_min` (plus an admin-only `F7_faq_autofix` line). It never holds and
     never regenerates: it is a repair step, not a gate.
   - How a pair is dropped: its whole block is cut out (question, answer and any later paragraph under it) and
     every other part of the text stays verbatim and in place (`quality_gates.drop_faq_pairs`). The FAQ is not
     rebuilt from first paragraphs (`replace_faq_section`), because that moves every other paragraph above
     `## FAQ`, and a dropped wrong-subject answer's second paragraph would land in the body (S247 review). The one
     case it cannot settle: a failing LAST pair with more than one paragraph, since text after the last pair may be
     a CTA. That pair is kept and listed in `kept_multi_paragraph` (and an `F7_faq_autofix` line).
   - Fallback: on a gateway error, an unusable reply or a 60 s timeout the FAQ is unchanged and `fallback: true`
     is recorded.

## Cost

When the flag is `shadow` or `on`, about $0.005 per blog at most (30 questions, ~1.5k tokens in, ≤ 1k out on
Haiku 4.5). The S247 trial batches of ~8 questions cost ~$0.002 per call. When the flag is `off`, or a piece has no
real questions, the cost is $0.

The answer fit adds one call per blog, about $0.0016 (≤ 5 pairs, ~1.2k tokens in, ≤ 200 out). The offline eval
estimate is $0.013 for the 8 checkpoint blogs. The cost is $0 when the flag is `off` or the blog has no FAQ. In
`isolated` mode it is one call per pair: about $0.0027 per blog on Haiku (the system prompt is sent once per pair).
`batched` on the Sonnet tier would be about $0.0044 per blog.

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

The three 2-1 "partial" pairs are reported but are not in the bar. The prompt has no example, place or question
from the checkpoint pairs (since the S247 revision, no named example at all).

The eval is **in-sample**, so passing it is weak evidence, as with the question filter's re-measure (decision 5).
The examples are new, but the rules were written from the three failures: "asked about a country, answered with
one town in it", "a generic reference ('the museum') … that describes something other than what the question
names", and "the question asks for a name …". E1 is therefore optimistic on recall. `on` needs more than E1–E3: it
needs `shadow` verdicts on new blogs (pairs the rules were not written from), read by hand, with no clear false
drop.

Known risk to E2 before the run: the "one part instead of the whole" clause may flag an answer that reaches the
whole through one part (a region's ecological zones explained through one lake; the ethnic groups along a route
explained through one province). If E2 fails on such pairs, tie the clause to the asked-for fact ("the question
asks for a figure or fact about the whole and the answer gives it only for one part") and add that an answer about
a part that still answers what was asked about the whole is `ANSWERS`, with examples kept off the checkpoint
pairs. Then re-run the eval.

**First run (S247, in-sample): FAIL.** 8 calls, $0.015, no errors (E3 passed). E1: 2 of 3 caught; the missed
one is the "What is <place> called in English?" pair, passed as `ANSWERS` because the answer explains the
district/town usage (the model read explaining around the name as giving it). E2: 2 false flags in 25, both
`WRONG_SUBJECT` and both because the model judged the pair against the BLOG's topic, not its own question: a
country-level visa question in a blog about one waterfall ("the question asks about the falls"), and "the ethnic
groups along this route" answered by naming the route's province and valley ("the route is the waterfall
route"). The predicted "one part instead of the whole" risk did not show; the topic bleed was not predicted.

**Prompt revision (S247, generic, no checkpoint or held-out example or place name):**

- judge each pair only against its own question; the blog's topic, the other pairs and whether the question
  suits the post do not matter;
- a question about a country or region may be answered at that level; a question about "this route / this trip /
  the area" is answered by naming the route's places;
- `NON_ANSWER`: a question that asks for a specific datum (a name, a translation, a number, a date, a yes or no)
  needs that datum stated, or a plain statement that it does not exist; explaining around it is `NON_ANSWER`;
- `WRONG_SUBJECT`: another thing than the one named, or a question about a whole answered with a smaller place's
  fact instead of the whole's.

The named examples were removed, and the prompt got shorter. Every rule above was written from the in-sample
failures, so a second in-sample pass is weaker evidence than the first. **The held-out set decides.** The next
run evaluates both sets in one container run under the same $0.10 cap: the 31 checkpoint pairs, and 40 FAQ pairs
from 12 older blog pieces outside the checkpoint (`.tmp-session/aa779-heldout/heldout.json` in the root
repo, read-only export, the checkpoint requests excluded; one call per piece as in production). The set is not
fully independent of the checkpoint: 5 of its 40 questions repeat a checkpoint question word for word with a
different answer ("Does Mongolia have any volcanoes?" twice, and "Which country is Kuang Si Falls in?", "Why is
Kuang Si Falls famous?", "Do I need a visa to visit Laos?" in one Laos piece). All 5 are unanimous answers, and
the Laos visa question is the false flag the round-2 prompt revision targeted. So the held-out bars are also scored
with those 5 pairs left out (`-indep`: E1-held-indep over the same 7 non-answers, E2-held-indep over 26 answers),
and the held-out set passes only when both the full and the independent bars pass. The held-out set is labelled by
a separate judge panel, and the eval is scored against it separately:

- E1-held: flags at least 80% of the unanimous non-answers (n/a when there is none);
- E2-held: the false-flag rate on the unanimous answers is at most 5%;
- E3-held: no call or parse errors, no missing verdict.

`on` needs E1–E3 and E1-held–E3-held to pass, plus a read of `shadow` verdicts on new blogs.

The round-2 eval's $0.10 cap covered the whole run (both sets; round 3: $0.25, below). An attempt (a retry too)
is started only if its worst case (the whole `max_tokens` billed as output) still fits under the cap. A finished
attempt is counted at the largest of its reported cost and its reported tokens priced at Haiku rates (round 3: the
variant's rates); a reply with no tokens and no cost, or a call that raised before a reply came back, is counted at
its worst case. So a reply that reports no cost cannot push the run
past the cap. (Counting every finished call at its worst case, as the first run did, would stop a 20-call run at
about 11 calls.) The typical estimate is about $0.013 for the checkpoint set alone.

**Second run (S247, both sets, revised prompt, batched on Haiku): FAIL.** 20 calls, $0.033, no errors
(`.tmp-session/aa779-fit-eval-r2.json`, root repo). In-sample: E1 2/3 (the "called in English?" non-answer still
passed as `ANSWERS`), E2 2/25 (the same two topic-bleed false flags: "Do I need a visa to visit Laos?" called
"not specific to the falls", and the "this route" question). The revised rules did not stop the model from judging
a pair against the topic it infers from the other pairs. Held-out: the first score said "E1-held n/a, 0
unanimous non-answers" because the scorer counted only the kinds `non_answer` / `wrong_subject`, while the held-out
judges used `partial` and `off_subject`. Fixed: a pair is a unanimous non-answer when every judge says
`answers_question: false`, whatever the kind (the checkpoint set gives the same 3 / 25 / 3 under both rules).
Re-scored (`.tmp-session/aa779-fit-eval-score-r2-rescored.json`): E1-held 4/7 = 57% (FAIL, bar 80%), E2-held 1/31 =
3.2% (pass), E3-held pass. The model caught both `off_subject` pairs and two of the five `partial` ones; the three
misses are `partial` for at least two judges and `ANSWERS` for the model. The one held-out false flag is the same
Laos visa question in another piece, again judged against the falls; it is one of the 5 checkpoint questions, so
the independent score is E1-held-indep 4/7 (FAIL), E2-held-indep 0/26 (pass).

Known tension for round 3: five of the seven held-out non-answers are `partial` for the judges (the answer stays
on the subject but leaves out what was asked), while the prompt says "a short, general or partial answer still
ANSWERS". The prompt is not changed for round 3 (tuning it on the held-out labels would spend the held-out set).
If round 3 misses E1-held on `partial` pairs, the next step is a decision on what a drop is for (only clear
non-answers, or also answers that leave out the asked-for fact), not another wording pass on these pairs.

### Answer-fit call mode

The round 1–2 false flags come from the batched input: the model sees every pair of the post, infers the post's
topic and marks a good pair as off-topic. Two ways out, measured in round 3 before one is chosen:

- `isolated`: one call per pair; the model sees only that question and answer (its own system prompt,
  `ISOLATED_SYSTEM_PROMPT`, with the same verdict rules and the same reply format, one item). The calls run
  concurrently, at most `FIT_CONCURRENCY` (3) at a time, each in a copy of the caller's contextvars. Fail open per
  pair: a pair whose call raises, or that has no reply by `PAIR_DEADLINE_S` (50 s, inside the 60 s step timeout),
  is kept and listed in `pair_errors`; the other pairs still count. Only when no pair gets a verdict is the FAQ
  unchanged with `fallback: true`. At the deadline the queued pairs are cancelled (never called, not in `calls`);
  a pair whose call is still running finishes in the background in a non-daemon thread (at most `FIT_CONCURRENCY`
  per piece, unbounded across pieces, as the batched `to_thread` call already is): it is billed and writes its own
  `llm_call_log` row, but its cost is not in the record's `cost_usd`.
- `batched` on a stronger model: the same single call on the Sonnet tier.

The mode is a module constant, `faq_answer_fit.FIT_MODE`. Until the round-3 result is written here it stays:

    FIT_MODE = "batched"

(a unit test checks that this line names the code's value). The model is never named in code: the stage row
(migration 266, `haiku`) decides in production, and only the ad-hoc eval passes a tier (`haiku` / `sonnet`).

**Round 3 plan (not run yet).** One container run, both sets (31 checkpoint + 40 held-out pairs), two variants:

- A: `batched`, tier `sonnet` (the legacy tier LLMClient accepts; satellite Sonnet, $3/$15 per Mtok), 20 calls,
  estimate $0.087;
- B: `isolated`, tier `haiku`, 71 calls, estimate $0.053.

B runs first, so a cap stop can only cut A. Hard cap $0.25 for the whole run, enforced in a metering wrapper around
`LLMClient.generate` (so it also holds for concurrent isolated calls): an attempt starts only if the counted spend,
the worst case of every attempt in flight and its own worst case (input at chars/3 tokens, the whole `max_tokens`
as output, at the variant's prices) still fit. Finished attempts are counted as in round 2, at the variant's prices.
A variant whose replies came from another model than its tier (Sonnet falling back to Haiku) is marked invalid.
Scored per variant and per set against the same bars: in-sample E1 3/3, E2 ≤ 1/25, E3 0 errors; held-out E1-held
≥ 80% of the unanimous non-answers, E2-held ≤ 5%, E3-held 0 errors, and the same E1/E2 bars with the 5
checkpoint-question pairs left out (`-indep`); split pairs reported separately.

Choosing the production default from the result:

- B passes every bar: `FIT_MODE = "isolated"`, the stage row stays `haiku` (cheapest passing variant);
- only A passes: `FIT_MODE` stays `"batched"` and the stage row moves to the Sonnet key (a migration or an admin
  change of the `t9_faq_answer_fit` row; the cost line above changes with it);
- neither passes: no `on`; the flag stays `shadow`, and the next step is the decision above on `partial`.

Whatever is chosen, the line above is updated to the code's value in the same PR.

### Rollout: one flag drives both steps

`T9_FAQ_LLM_FILTER` switches the question filter AND the answer fit together. The Dev worker already runs `shadow`
for the question filter, so merging the answer fit starts its `shadow` calls on the next blog (about $0.0016 each;
the FAQ is unchanged). The risk is the other way: setting `on` for the question filter, which has passed its bar,
also turns on answer-fit drops.

**Do not set `on` until the E1–E3 and E1-held–E3-held results, and a read of `shadow` verdicts on new blogs,
are written in this ADR.** If the question filter must go `on` first, the answer fit needs its own flag first (a
code change); do not flip the shared flag to get it.

## Rollback

Set `T9_FAQ_LLM_FILTER=off` (no deploy of code; the worker picks up the task definition). To remove the stage
row: `DELETE FROM shared.llm_role_config WHERE stage = 't9_faq_filter'` (the SAFE_DEFAULTS entry is harmless
while the flag is off).

The answer fit uses the same flag, so `off` turns both steps off; there is no separate switch. Its row goes with
`DELETE FROM shared.llm_role_config WHERE stage = 't9_faq_answer_fit'` (migration 266 rollback).



## Answer-fit round 3 result (S247, 22/10/2026) — decision input

Paid eval, one run, $0.140 (cap $0.25), 0 errors. In-sample = the 31 checkpoint pairs (3 unanimous non-answers, 25
unanimous answers); held-out = 40 pairs from 12 older blog pieces (7 unanimous non-answers, 31 unanimous answers; 5
questions repeat a checkpoint question word for word, also scored without them).

| Variant | In-sample E1 / E2 | Held-out E1 / E2 | Held-out without repeated questions | Result |
|---|---|---|---|---|
| A — batched, Sonnet | 3/3 / 1/25 | 7/7 / 0/31 | 7/7 / 0/26 | **PASS** |
| B — isolated, Haiku | 3/3 / 0/25 | 4/7 / 0/31 | 4/7 / 0/26 | FAIL (held-out recall 57% < 80%) |

Decision per the rule above: `FIT_MODE = "batched"`, stage `t9_faq_answer_fit` on `sonnet` (migration 267, SAFE_DEFAULTS
mirrors it). Cost about $0.004 per blog (20 batched calls $0.087). Rounds 1–2 (batched Haiku) failed in-sample: the model
judged pairs against the topic it inferred from the other pairs. `on` still needs this ADR accepted and a read of shadow
verdicts on new blogs.

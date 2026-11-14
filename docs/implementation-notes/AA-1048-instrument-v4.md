# AA-1048 — claim instrument prompt v4, out-of-sample guard and kept reasons (lane aa1048v4, S280)

Follows `AA-1048-instrument-v3.md`. The v3 evaluation (run 33835b87, 50 claims, 150 calls, 0.0320 USD) was
**no_go**:
- false positives dropped: 17/25 = 68% (bar 60%, pass);
- stored violations kept: 17/20 = 85% (bar 90%, fail).

By group: paraphrase 12/17 dropped, verbatim 4/4, place background 1/4. Relabels (in-sample) 5/5 kept.

The $0 miss analysis (counts and short quotes) is in root `.tmp-session/S280/aa1048/v3-miss-analysis.md`. That is
scratch, not a repo file: the pool holds real tour text. The bars and caps are unchanged.

## Decisions

- **What v4 targets.** The 3 stored violations v3 lost, and the 8 false positives it still fires on (5
  paraphrase, 3 background). The reasons come from the S279 panel and the stored labels; v3's own `why` was never
  kept (below).
  - Two of the lost violations are pace promises ("moves slowly", "at a slow pace"). The policy (D14) allows pace
    only as the source states it. v1 had that rule, but v2 and v3 dropped it. v3's P1 sets aside "the writer's
    reading", and its recall guard ("NOT style") had no pace item, so a pace word read as style.
  - The third lost violation joins two source facts into a new activity: a ride offered "by canoe or motor boat"
    plus canoeing on a lake, written as one canoe trip. Every word is a source word, so P1 "summaries" and P3
    "labels made of source words" excused it.
- **v4 = v3's restatement test plus targeted additions**, each traced to a miss:
  - P1 step 2: pace, effort, comfort, safety and who the trip suits join the NOT-style list. A purpose given to a
    source fact ("by design") is set aside.
  - P1 new-specific guard: a restatement must not add a number, name, time, service, activity or view that the
    whole source lacks and that does not follow in one obvious step. Joining two source facts, firming one side of
    an "or", or stretching a fact to other days all add a specific. This is the brief's guard.
  - P1 examples: two one-step readings of the source's order:
    - a stop listed after lunch is an afternoon stop (a clock time would still be new);
    - the last item listed for a day ends that day.
  - P2b: a visited place's own lasting features (architecture, inlay, setting, size) stay background, also when
    written as what the traveller sees there. A "view" means a sight beyond the place, or the place under a light,
    season or state the source does not give. Access the source does not give stays a promise. This is the writer
    source-fidelity policy: A (background about named places, including architecture and setting) is allowed; B
    (views, sightings, activities, services, times) is not. The three background false fires all describe a
    monument's lasting features in "what you see" wording, which v3's B5 ("scenery seen") pulled toward a fire.
  - P3: the start rule ("where the trip already is") now covers sentences, not only day headings.
  - P4 (new): a sentence about the text itself promises nothing. The trip facts inside it are still judged, so the
    v3 subset's scope.framing and misbinding violations (a wrong start in a "this guide ..." sentence) stay
    violations.
  - B5: adds a pace / effort / suitability item ("rest days, short drives or level ground do not by themselves make
    a trip slow or easy"). The "experience at a stop" item now excludes the visited place's own lasting features.
- **Unchanged, byte for byte:** P2, B3 and B4 (v3's strings), every other v3 B5 item, the five v3 P1 examples, and
  the user prompts (v2's: whole source, BEFORE / SENTENCE / AFTER). Only the system texts differ, so v3 and v4
  compare on rules.
- **Invented examples only** (Karsk, Brellin, Ostra, Velm, Tarro). The S280 leak check (scratch, the same rule as
  v2 and v3) found no three-word sequence with a content word shared between the v4 system texts and any of the 286
  pool claims. The first draft failed on 16 claims ("in the evening", "part of the", "on a ridge", "in front
  of" ...) and was reworded until 0 matched. Single shared content words with the v3 misses ("pace",
  "afternoon", "fort") remain. These are generic words, the same tolerance as v3.
- **`PromptVersion.shaped_on`** names the runs whose misses shaped a version: v3 is shaped on 3018008d (v2's run), v4
  on 3018008d and 33835b87. `instrument_eval run --live` refuses unless the `--exclude` files are those run reports.
  A report carries a `run_id`; a plain list of prefixes carries none, so it does not count. The dry run reports
  `out_of_sample` (`shaped_on`, `excluded_runs`, `missing`) without refusing. v3 was kept out of sample by the
  runbook alone; v4 makes it a gate. The field is not part of the hash (`sha` covers id and texts only), so no
  recorded tag changes.
- **`--with-reasons`** (opt-in) adds each entailment reply's `why` to the report, cut to 300 characters
  (`r1.REASON_CHARS`), with "" for a missing vote. `r1.run` now returns `reasons` for entailment instruments.
  Without the flag the report is unchanged (counts and prefixes). Without it, v3's no_go could only be read
  through the panel's reasons, not the model's.
- Tests pin the recorded tags of v1, v2 and v3 (`aa1048-r1-v3@7e4923a09780` is in the S280 ledger rows).

## Changed (vs. the lane brief)

- The brief asked for "each model reason" in the miss analysis. The v3 replies' reasons were never stored. The
  report holds booleans only, and r1 dropped `why`. The analysis therefore uses the panel and label reasons, and
  v4's run keeps the model's reasons (`--with-reasons`) for the next reading.
- The out-of-sample handling became a gate (`shaped_on`), not only a runbook step. The harness already supported
  several `--exclude` files; the brief asked to keep the run out of sample "where the harness supports it".

## Tradeoffs

- **Pace as a fact could bring back false fires.** Some ok-labelled persona claims state a pace the source does
  imply. The rule fires only when the source does not state it, and says why rest days alone are not enough (the
  panel's own D14 reasoning). The gate measures both sides on fresh claims.
- **A longer prompt** (about 8.0k characters vs. v3's 5.6k) costs a little more per call. The measured plan, scaled
  from v3's real cost per call (0.000214 USD), is 0.0372 USD for the 150 calls.
- **The last fresh round.** After excluding the v2 and v3 subsets, 31 stored violations are left; a 50-claim
  subset uses 20. Another fresh round of this size after v4 is not possible (11 left). A v5 would need a new pool
  (a new R1 fired set), not a fourth sample from this one.
- **The lasting-feature rule could excuse a real view promise** that is worded as a feature ("the domes glow
  gold"). Light, season and state stay promises in both P2b and B5, so only the permanent form is excused.

## Should know

- **No migration and no settings change.** Same bars and caps.
- The live run needs this code on Dev. Order:
  1. merge;
  2. Deploy Dev;
  3. the trial (3 claims);
  4. the full run (50 claims).

  Trial + full stay under 0.10 USD. Runbook and the rendered-script maker: root
  `.tmp-session/S280/aa1048v4/RUNBOOK.md`.
- Both `--exclude` files are the v2 and v3 run reports, which hold counts and prefixes only, so they are safe for
  the scripts bucket. The pool holds real text: upload it for the run and delete it after. A `--with-reasons`
  report holds model text: keep it in `.tmp-session/`. Only counts go to the root audit.
- If v4 is go: propose a full held-out R1 on v4 entailment (the tech lead's spend yes) before any shadow or pin. If
  v4 is no_go: read the misses with the model's own reasons ($0) and decide on a new pool. Never pay for a re-run of
  the same prompt.

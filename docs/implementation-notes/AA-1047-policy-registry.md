# AA-1047 part A (lane l1-w1-policy) — one writing policy registry

Quality W1 (root ADR 0020, layer L1). Source of the rules: the S271 decision page (D1–D16, the tech lead chose option
A for all 16), root `docs/audits/2026-11-06-S271-writing-quality-audit/` (README, decisions.md,
data/design-review.json `arch.roadmap[1]`, critic fixes F1, F6, F9, F10, F13). Parallel lanes of the same issue:
l2-w1-factsheet (FactSheet module, CIS 386) and l3-w1-exits (final_contract on every exit, W0 follow-ups). This lane
keeps to the policy / rubric / rules / eval / lint modules and the listed prompt lines; it does not touch the S1/T2/T9
exit paths.

Two PRs, in merge order:

1. **CIS migration 387** (migration-only, contract): registers the sixteen prompt versions the registry moves. Joins
   lane l2's migration-only PR (one CIS migration PR per wave: 386-387).
2. **CIS code PR**: the registry, compiler, lint, consumers, contradiction suite. Its branch carries the 387 commit
   (identical bytes) so the prompt registry gate passes locally; after the migration PR merges, that file is no
   longer in its diff.

## What was built

- `services/quality/writing_policy.yaml` — v1, frozen from D1–D16: one meta band 140–155 and one title band 45–60
  (D5), the slug cap 60 cut at a word boundary, writer aims below the band (S1 130–148, T9 130–145, T2/v1 145–152),
  the CTA as a literal line owned by code, FAQ pairs 3–4 (regenerate 4) and the FAQ topics that need a source line,
  meals (a source-day meal line is source-backed for every writer), day count = source, the claim policy per topic
  (background D1, brief D2, reflection D3, hypothetical D4, rarity D13, suitability D14, scene D15, timing, numbers
  F6, scope, truth_cut D6, elephant D8), `rules_config`, class severities, the eval judge classes, the label rubric,
  the T10 route rows and F9 class rows, the AA-788 gate promotions and the AA-920 rule injection (registry entries
  only), prompt fragments, retired wordings, and the synthetic examples.
- `services/quality/writing_policy.py` — schema check, compiler (`--write` / `--check`) into
  `writing_policy.compiled.json`, `policy_sha` (sha256/16 of the YAML without the examples) stamped on every
  compiled section, and accessors (`band`, `writer_aim`, `fill`, `fragment`, `label_rubric`, `judge_classes`,
  `rules_config`, `gates_with_repair`, `style_only_gates`, `f9_hold_classes`, `claim`, `meals`, `slug_at_boundary`…).
- `services/quality/policy_lint.py` — run by `python -m services.quality.lint` (CI Lint): p1 band number in a
  string, p2 length comparison with a band literal, p3 a band constant, p4 an 8-word copy of a compiled policy text,
  p5 a retired wording, p6 stale compiled file / taxonomy severity drift / unknown route-row gate, p7 the same in TS.
  Allowlist: the policy's own files and `frontend/lib/quality-registry.generated.ts`. Docstrings and comments are not
  scanned.
- `services/quality/policy_harness.py` + `tests/unit/quality/test_aa1047_contradictions.py` — every example runs
  through every fixer, gate and rule of its field; the combined verdict must equal `expect` for 100%.
- Consumers switched to the compiled output in the same PR: eval rubric and judge classes
  (`writing_memory.eval_judge`), LABEL_RUBRIC (`scripts/aa729/labels.RUBRIC`, same object), `claim_rules.RulesConfig`,
  quality_gates `CHEAP_REPAIR_GATES` / `STYLE_ONLY_GATES` / `OWNED_BY_FIX_STEP` and `f9_policy.HOLD_CLASSES` (route
  rows), every SEO band in code (quality_gates F4, generate SEO fit, seo_meta_utils, s1 writer v2, S1 v1 / T2 prompts,
  flag_fix, brand_standards, admin_pipeline, frontend reviewModel / CompareModal through the generated TS),
  `FAQ_MIN_PAIRS`, the FAQ regeneration count, the slug cut (service `_ascii_slug`, publish `blog_slug`).
- `policy_sha` everywhere: the compiled file and generated TS, the eval judge prompt and `quality_signal`,
  `RulesConfig.policy_sha`, `labels.RUBRIC["policy_sha"]`, the release fingerprint component `policy`
  (`writing_memory.config_fp`), and the prompt registry hash of any prompt whose source reads the policy.

## Decisions

- **One meta band 140–155 in code, writers aim below it.** The writer aims are policy values, not a second band: a
  model overshoots a character target (lesson S233), so the band is enforced by code (fit / gate) and the prompt says
  "aim X–Y, never over 155". The lint treats aim bounds as policy values too, so they cannot be restated by hand.
- **Numbers (critic fix F6).** A novel number holds only inside a trip frame (the trip, reader, group or a day is the
  subject — `f9_policy.TRIP_SUBJECT_RE` — or `claim_rules` sees a concrete trip service / promise frame); outside it
  (a year, a height, a share) it warns. F1 and `apply_rules` use the same rule. Word numbers count like digits in F1
  (`claim_rules.find_novel_word_numbers`), and the lines of other trips never support a number (F1 now reads the
  seed's own lines, `hygiene.split_seed`).
- **Rarity (D13).** F18 still reports every rarity finding; it blocks only when the claim is attached to the trip
  (trip subject) or is a quantifier / social-proof finding; a rarity claim about a place follows D1 and warns
  (`trip_facts.rarity_blocks`).
- **Background (D1) in claim_rules.** An uncited place_background sentence about a place the SOURCE names passes
  (before: it failed whenever any place had a card); a number or year in it warns (`background_numbers`); a trip
  activity / service / time / view, a place the SOURCE never names, or a rule or fee blocks. This is the unwired F9 v2
  rule table, so live behaviour moves only where F1 / F18 moved.
- **FAQ topics.** `faq.topics_need_source` is one list for the deterministic PAA selection, the FAQ dropper and now
  the LLM question filter (`faq_filter.select_faq_questions` drops a kept question on a topic with no source line,
  logged as `policy_dropped`).
- **Meals.** The A1 exemption for a meal line copied from the same source day now covers T2 too
  (`brand_audit_node.meal_time_claims_supported`).
- **Slug.** Over 60 chars the slug is cut at a hyphen, never mid-word, in the T9 fold and in the publish
  `blog_slug` stem.
- **CTA check.** `claim_rules.check_cta` also passes a text whose prose ends with the CTA line (a short one-paragraph
  text: the 35% tail started inside the line, so `hygiene.ensure_cta` left the text alone while the check failed it).
- **Taxonomy severities** other.f7, other.c3, other.s5 → block (they were warn while F17 / F16 held); scope.framing
  stays block in the taxonomy while F12 runs in warn mode until measured (AA-788 mode switch, ADR 0020: only measured
  gates hold). fact.embellish's definition now says reflection with no trip fact is voice (D3).
- **Eval judge (W1 item 2).** No 16000/9000 clipping: R2 and the judge comparison script read
  `eval_judge.evidence_view` (uncut SOURCE + labelled OUTPUT + their sha); an item over the cap raises
  `InputOverCap` before any model call and is a counted error at $0. The verdict carries `evidence_sha`.
- **Prompt registry.** A prompt whose source FILE reads the policy (`prompt_registry.reads_policy`: it imports the
  policy module, or a constant whose value comes from it, such as `SEO_META_MIN` from seo_meta_utils or
  `FAQ_MIN_PAIRS` from quality_gates; helper functions are not followed) also hashes the compiled `policy_sha`
  (`prompt_registry.prompt_hash_of`), so a policy edit is a prompt edit and needs a version bump + migration. File
  level on purpose (S272 review): the first cut looked for `_wp.` in the hashed symbols only and missed s1_generate
  (`_BACKGROUND`), t2_generate (`_RULES_3_TO_6`…), s1/t2_flag_fix (meta rules built inside `flag_fix_node`) and
  t10_judge (`f9_user_prompt`, now also one of its hashed symbols). Sixteen stages read the policy today.
- **Short T9 meta (S272 review, contradiction 12 inside the policy).** The T9 writer and SEO fit aim 130–145 (lesson
  S233: a model overshoots its target), the band is 140–155. An aim under the band floor is now a documented
  exception the schema check only accepts with `short_fix` (the deterministic step that lands a short value in the
  band). T9: `service._fit_seo_meta` gives a complete 130–139 meta the DESTINATION tail (`seo_meta_utils.
  extend_short_meta`, ', in <destination>.') before and after the fit call when that lands it in the band (no LLM
  call, no word the source lacks); the fit user prompt tells a short meta "too short: must be 140–155" instead of
  "must be 130–145". S1 keeps `fit_seo_meta_final`. The aim itself is not raised (that would be a writer-prompt change
  needing a paid smoke, and lesson S233 says the overshoot comes back).
- **Route rows: no migration.** The route rows are code sets compiled from the YAML; no DB row changes, so CIS 387
  only registers prompt versions.

## Changed writer-prompt lines (critic fix F9; everything else renders byte-equal)

Proof: `tests/unit/aa1047_prompt_lines.py` puts these four lines back and the pre-AA-1047 pins
(`SYSTEM_PROMPT` sha b5978839…, PROMPT_RULES 9ea12897…, round 5 79cd046a…) still match. The S1 v1 / T2 prompts, the
S1 writer v2, the brand-fit house style, the place-card prompt, brand_standards, the F9 judge prompt and the T9 SEO
fit / FAQ prompts render byte-equal to main (checked by dumping each prompt on main and on the branch).

| where | before | after | topic |
|---|---|---|---|
| T9 writer `SYSTEM_PROMPT` (prompts.py, SOURCE FIDELITY) | ALLOWED: short, widely known background … who founded it, its religious or cultural role, whether it is still in use, written in words, without numbers unless the seed has them. | adds "its geography", "its architecture or setting", and "Background never says what this trip does there: no activity, service, time or view." (`t9_writer.background_allowed`) | background (D1) |
| T9 writer `SYSTEM_PROMPT` | - Include the given call to action, worked naturally into the piece, not pasted on as a bare final sentence unless the channel's own style calls for that. | - End with the given call to action as its own sentence, word for word (code puts the exact line in when it is missing or reworded); never rework it into another sentence. (`t9_writer.cta`) | CTA |
| T9 writer AI-tells rules (ai_tells.PROMPT_RULES) | so the piece closes on its last fact with the call to action worked into it. | so the piece closes on its last fact, then the call to action line. (`t9_writer.cta_close_example`) | CTA |
| T9 writer AI-tells rules | WRITE the call to action as the piece's own sentence, worked into the last fact, with no label line before it. | WRITE the call to action line, word for word, as the piece's own last sentence, with no label line before it. (`t9_writer.cta_dont_write`) | CTA |
| T9 sentence repair `_REPAIR_SYSTEM` (generate.py) | Use only what the SOURCE states, or widely known background about a place it names. | Use only what the SOURCE states, or short background about a place it names (what it is, its history, geography, culture or setting), never what this trip does there. (`t9_repair.background`) | background (D1) |
| T9 SEO fit user prompt (generate.fit_seo_fields, the "must be" note) | shown when the meta is outside 120–155 | shown when outside the policy band 140–155; a meta under 140 reads " — too short: must be 140-155" (S272 review: "(135 chars — must be 130-145 …)" was self-contradictory) | meta band (D5) |

Meals changed in code only (no prompt line). The T9 writer `WRITER_PROMPT_VERSION` moves b5978839 → cc9b4979.

## S271 T9 contradictions (blog-pipeline-audit.json `plan.contradictions`)

| # | topic | status in this lane | proof |
|---|---|---|---|
| 1 | place background widths | gone (code + writer/repair lines) | bg-*, rar-01/04/07 |
| 2 | brief as source | gone (rubric + judge: the brief is an instruction) | test_contradiction_2 |
| 3 | judge rubric vs embellish | gone (D3 in rubric and taxonomy) | test_contradiction_3 |
| 4 | timing: repair vs F17 | code agrees (F17 holds every unsourced time) | tim-* |
| 5 | severities f7 / c3 / s5 / scope | gone (taxonomy = policy, lint p6) | tim-01, faq-03/05, scp-01 |
| 6 | FAQ filter vs dropper | gone (one topic list) | faq-* |
| 7 | length vs background | writer-vs-writer: moves to W5 (writer prompts, measured with the paid smoke) | — |
| 8 | numbers (digits only, other trips) | gone (F6 frames, word numbers, own lines) | num-* |
| 9 | scope | code agrees (F12 by policy severity; run-time mode stays warn until measured) | scp-* |
| 10 | CTA worked-in vs ensure_cta | gone (writer lines + check) | cta-* |
| 11 | structure rules vs retired F3/F5 | writer-vs-code: moves to W5 (writer prompts) | — |
| 12 | three meta bands, slug mid-word | gone (one band, slug at a word boundary) | meta-*, tit-*, slug-* |
| 13 | retry ownership (token_budget) | not policy: no owner yet — needs an issue (gateway retry ownership); see "Done-when gap" | — |
| 14 | effort per provider | not policy: no owner yet — needs an issue (route effort per provider); see "Done-when gap" | — |
| 15 | held vs delivered | exit contract: lane l3-w1-exits (same issue, this wave) | — |
| 16 | release fingerprint scope | in part: `policy` component added; route-control / catalog parts are change-control work | test_contradiction_16 |

### Done-when gap (S272 review)

The AA-1047 done-when says the 16 listed T9 code-vs-code contradictions go to 0. This lane closes #1–#6, #8–#10, #12
and part of #16; #7 and #11 are writer-prompt work (W5), #15 is lane l3-w1-exits, and #13 / #14 are not writing-policy
rules and have no owner yet. Part A of AA-1047 is therefore NOT done by this lane alone. The orchestrator posts the
comment below on AA-1047 (this lane does not write to Linear) and gets the tech lead's agreement before part A is
marked done; #13 and #14 need an issue (asked first, per the Linear rule):

> AA-1047 part A (policy registry, S272 W1) closes T9 contradictions #1–#6, #8–#10, #12 and part of #16 (proof: the
> contradiction suite, 103 synthetic twins, each owning detector checked on its own). Open and moved: #7 (length vs
> background) and #11 (structure rules vs retired F3/F5) to W5, the writer-prompt wave, measured with the paid smoke;
> #15 (held vs delivered) to lane l3-w1-exits of this wave; #13 (retry ownership on token_budget) and #14 (effort per
> provider) are gateway / route rules, not writing policy: proposed as one new issue in the AI gateway project. #16
> keeps its route-control / catalog parts for change-control work. Part A stays open until this is agreed.

## Tradeoffs

- **Fragments only where a line resolves a contradiction.** Other prompt lines keep their hand text until W5 (an
  unmeasured writer change costs a paid smoke each time); the lint catches a copy of any compiled fragment or rubric
  line, so new hand restatements cannot appear.
- **F1 number holds narrow.** A background year or height no longer holds a piece (it warns). The F9 judge still sees
  the sentence; a wrong year in background is a light fix by the rubric (D1).
- **Examples are not part of policy_sha.** Adding a twin changes no behaviour and needs no prompt bump; a rule change
  moves the sha and every prompt that reads the policy.
- **check_cta / RulesConfig belong to the unwired F9 v2 path**; they are aligned now so W4 switches on a consistent
  table, and the harness runs them as the reference checkers.

## Should know

- Contradiction suite: 103 synthetic examples, 100% as the policy expects (each topic has fire and no-fire twins:
  background, timing, numbers 11; meta band 10; rarity, scope, slug, FAQ 8; CTA 7; meals 6; title band, day count 5;
  hypothetical 3, reflection 2). Every body example carries a claim row, and every body fire twin lists its owning
  detectors (`all:`), each of which must reach `expect` on its own (S272 review: `combined` hid a lax detector).
  Not an owner by design: apply_rules for a time or a digit outside place background (F17 / F1 own them; with a cited
  trip_fact row it passes), check_replacement for scope. The admin sentence check (`check_replacement`) runs in the
  harness and keeps each gate's own `blocking` flag (a background year warns there as in the pipeline).
- Pins: `tests/unit/snapshots/aa919_release0_prompts.json` regenerated; only the 40 T9 system prompts (blog + social)
  moved, every A1 / T2 prompt and all 80 user prompts kept their pins. `WRITER_PROMPT_VERSION` tests assert both the
  new value and the pre-AA-1047 one through `pre_aa1047`.
- Local only: `test_aa899_openapi_export` fails in the shared venv (fastapi 0.110.1 instead of the pinned 0.161.1);
  no route or schema changed here, so `docs/api/openapi.json` is not regenerated. `tsc` errors seen locally come from
  a stale shared `node_modules` (dompurify, react-markdown missing), none in the touched TS files.
- CTA gap not fixed (out of scope): a social text whose CTA sits in the second-last paragraph with one more paragraph
  after it passes `check_cta` (the closing window is the last two paragraphs) although the policy says "last
  sentence". `ensure_cta` keeps it. Left for W5 (channel shapes).
- Behaviour changes on Dev after deploy (T9 gates, no paid call): F4 meta band 120–158 → 140–155; F1 holds only trip
  numbers; F18 place rarity warns; the FAQ filter drops source-less topics; T2 meal lines copied from the source day
  pass; slugs cut at a word boundary. Prompt versions move (t9_write v4, writing_memory_eval v3, v2 of t9_repair,
  s1/t2_brand_audit, a3_place_card, a3_place_card_check, s1/t2_generate, s1/t2_judge, s1/t2_flag_fix,
  t9_faq_answer_fit, t10_claims, t10_judge) — CIS 387 registers all sixteen. A T9 meta of 130–139 that the
  destination tail lands in the band no longer costs a fit call or an F4 hold.
- The paid 3-piece writer smoke (critic fix F9, ~$0.50) was NOT run here: it is a live step of the code PR.
- Not in this lane: critic fix F10 (A3 input check, unanchored-atom share) is the W1 input measurements item; F13
  (review capacity for A1 rows) is a decision for the tech lead; both stay on AA-1047.
- `config_fp` gets a new component `policy`: every release fingerprint moves once at deploy (reverify reason
  `prompt_version_mismatch`).

## S272 adversarial review 2 (fixes on PR #1368)

Decisions
- One trip-frame predicate for a novel number: `claim_rules.trip_number_frame`, used by F1 (`gate_grounding`), the
  rule table (`apply_rules`: the number branch, now digits too inside the frame, and the place_background number case)
  and the admin check (`check_replacement` runs F1). The frame: trip / reader / group / day subject, a service word
  (f9_policy.SERVICE_RE), a concrete or framed trip word, a trip activity / leg / stay noun (walk, hike, climb,
  crossing, ride, ferry, transfer, visit, tasting, nights, overnight, stay), a clock time, a departs / leaves / takes /
  lasts verb with a duration or distance, or an activity verb shared with the moment's own itinerary lines. 'boats'
  is deliberately not a trip noun ('Brecca harbour holds 300 fishing boats' is background).
- Inside the frame the AUDIENCE and BRAND lines support an age only (`novel_trip_numbers`): 'Walkers aged 25-50'
  never backs 'takes 50 minutes' or 'capped at 25 walkers'; 'suits children aged 7 to 16' still rests on it (AA-768).
- Access rarity ('a rare chance', 'a handful of travellers', 'few groups', 'the only ones allowed') is one lexicon,
  `hygiene.ACCESS_RARITY_RE`, read by F18 (always blocking: its own problem text, not the place-rarity one) and the
  rule table's social tripwire (`claim_rules.RARITY_RE`).
- `check_replacement` now runs F17 / F18 on the admin's sentence (`gate_trip_facts(..., sentence_only=True)`: the
  piece-level checks — missing family caveat, thin moment — are skipped) with the pipeline's blocking flags.
- Positions: the rule table runs the position check for every claim type (not only place_background) and no longer
  accepts Facts as support (F17 reads the moment's own seed lines only). Fail closed until F17 reads Facts too.
- T9 short-meta tail: the DESTINATION (raw_tours.country, one per trip) is named only when it is the place's own seed
  `Location:` country (`seo_meta_utils.place_country_tail`); otherwise no tail and the fit call / F4 decide. The trip
  name is passed so the AA-794 other-country check runs; an alias spelling of a country the meta already names gets no
  second tail.
- Prompt registry: a module alias (`from pkg import mod as m` / `import a.b as m`) read as `m.CONST` counts when CONST
  is policy-derived — file level for a module-level constant, per hashed symbol for a helper function
  (`symbols_read_policy`), so a file that only calls a policy-reading helper outside the hashed symbols (brand_fit's
  v2 judge path) does not move t7_debate_judge. No stage hash moved: the registry check reports 0 problems and the
  migration 387 manifest is unchanged.
- FAQ filter prompt: its KEEP list stays broader than `faq.topics_need_source` on purpose; the code post-filter
  (`policy_dropped`) owns the rule (comment above SYSTEM_PROMPT; no prompt text change, no version move).

Tradeoffs / not fixed
- The T9 writer aim stays 130-145 (under the 140-155 band): raising it, or putting the band into the fit system
  prompt, moves the t9_write / t9_seo_fit prompt versions past migration 387. Left for a later prompt release. Known
  residue: with a 4-letter destination (Laos, Peru) a 130-meta reaches only 139 with the tail; a long destination
  overshoots; a meta ending in '!' or '?' is never composed (meta_complete_sentence needs '.'); a meta that already
  names the destination gets no tail. These stay with the fit call, then F4 holds — never a bad meta.
- Policy lint: concatenation (`"..." + str(155)`) and a bare band literal with no field word in its statement are not
  detected; the lint docstring says so.
- Versus origin/main, the body examples that main blocks and this branch warns on are only the D1 / D13 decisions
  (background numbers and years, place rarity); every review counter-example blocks in F1, apply_rules and
  check_replacement (or F17 / F18 and check_replacement).

## S272 adversarial review 3 (fixes on PR #1368)

Decisions
- The number frame fails closed. `claim_rules.trip_number_frame` = `trip_attached` OR NOT `background_number_signal`:
  a number the SOURCE lacks warns only when the sentence carries a positive background signal (a year form, a
  founding / building verb, a dimension unit with its dimension word, a share or population, a count a place holds
  that is not people or beds) and nothing ties it to the trip. Review 2's frame was an allow-list of trip words with
  warn as the default, so a leg, a dotted time, a capacity or a group size phrased without those words only warned.
  Same predicate in F1, `apply_rules` (number branch and place_background number case) and `check_replacement` (F1).
- `trip_attached` (the positive half) widened: descent / ascent, boat(s), skipper, dotted clock times ('6.30', not a
  decimal measure like '1.25 km' or '0.45%'), durations ('a 20-minute', '2 hours away', 'a 3-hour affair'), a lie /
  is / leads / set-aside verb with a duration or distance (not '40 km long'), and group-size / capacity frames
  ('holds 12 hikers', 'at most 8 hikers share', 'capped at 25 walkers', 'keeps 4 places').
- 'boats' is now a trip noun (fail closed): 'Brecca harbour holds 300 fishing boats' blocks; 'holds 300 fishing nets'
  stays background. The review 2 note that called boats background is superseded.
- F18's trip-vs-place test (`trip_facts.rarity_blocks`) is `claim_rules.trip_attached` with the moment's own itinerary
  lines (gate_trip_facts passes them), so the number frame and F18 agree on one sentence. The rule table reads F18's
  rarity lexicon (`trip_facts.unsupported_rarity`, the same `RARITY_CLAIM_RE` and source test): trip-attached fails,
  place rarity warns (D13 / D1). It is a separate tripwire, not part of `RARITY_RE` (the social tripwire always
  fails, which would block the place-rarity warn twins). apply_rules now owns every rarity twin.
- T9 short-meta tail: `seo_meta_utils.place_country_tail(destination, *seed_texts)` reads EVERY `Location:` line of
  the own seed and of each route segment (`location_countries`) and names the destination only when every line
  resolves to it; a line with no country part, or any other country, means no tail.
- Harness: a meta example may carry `route: [<seed key>, ...]` (route moments' seed blocks joined by blank lines, as
  `service._reread_seed`); a new synthetic seed `harbour` (Dunmere Harbour, Tesselmoor) is the cross-border moment.

Changed
- New twins: num-22..30 (the review's leg / time / capacity / group / duration cases block; a count a place holds and
  'since 1820' warn), rar-13..17 (rarity on a crossing / visit / tour / walk blocks; place rarity warns), meta-13 /
  meta-14 (route across a border: no tail; route inside the destination: composed). rar-01 / 04 / 07 list
  apply_rules. Unit tests in test_aa1047_review2.py: the reviewer's Luang Prabang + Hanoi reproduction through
  `_fit_seo_meta`, route segments, a Location line without a country part, the frame cases and rarity agreement.

Should know
- Compared with origin/main on every body example (F1, F17, F18): the only cases main blocks and this branch warns on
  are the D1 / D13 decisions (background numbers with a background signal: bg-02, bg-06, num-05, num-09, num-10,
  num-21, num-30; place rarity: rar-01, rar-04, rar-07, rar-17). Every review counter-example blocks.
- No policy key changed (examples only): policy_sha stays 56729ec18379581c, the prompt registry check reports 0
  problems, migration 387's manifest is unchanged.

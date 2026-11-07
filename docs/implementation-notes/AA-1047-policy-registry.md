# AA-1047 part A (lane l1-w1-policy) — one writing policy registry

Quality W1 (root ADR 0020, layer L1). Source of the rules: the S271 decision page (D1–D16, the tech lead chose option
A for all 16), root `docs/audits/2026-11-06-S271-writing-quality-audit/` (README, decisions.md,
data/design-review.json `arch.roadmap[1]`, critic fixes F1, F6, F9, F10, F13). Parallel lanes of the same issue:
l2-w1-factsheet (FactSheet module, CIS 386) and l3-w1-exits (final_contract on every exit, W0 follow-ups). This lane
keeps to the policy / rubric / rules / eval / lint modules and the listed prompt lines; it does not touch the S1/T2/T9
exit paths.

Two PRs, in merge order:

1. **CIS migration 387** (migration-only, contract): registers the six prompt versions the registry moves. Joins
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
- **Prompt registry.** A prompt whose hashed symbols read the policy (`_wp.`) also hashes the compiled `policy_sha`
  (`prompt_registry.prompt_hash_of`), so a policy edit is a prompt edit and needs a version bump + migration.
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
| T9 SEO fit user prompt (generate.fit_seo_fields, the "must be" note) | shown when the meta is outside 120–155 | shown when outside the policy band 140–155 | meta band (D5) |

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
| 7 | length vs background | writer-vs-writer, open until W5 | — |
| 8 | numbers (digits only, other trips) | gone (F6 frames, word numbers, own lines) | num-* |
| 9 | scope | code agrees (F12 by policy severity; run-time mode stays warn until measured) | scp-* |
| 10 | CTA worked-in vs ensure_cta | gone (writer lines + check) | cta-* |
| 11 | structure rules vs retired F3/F5 | writer-vs-code, open until W5 | — |
| 12 | three meta bands, slug mid-word | gone (one band, slug at a word boundary) | meta-*, tit-*, slug-* |
| 13 | retry ownership (token_budget) | not policy: gateway lane / later wave | — |
| 14 | effort per provider | not policy: route lane / later wave | — |
| 15 | held vs delivered | exit contract: lane l3-w1-exits | — |
| 16 | release fingerprint scope | in part: `policy` component added; route-control / catalog parts are change-control work | test_contradiction_16 |

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

- Contradiction suite: 99 synthetic examples, 100% as the policy expects (each topic has fire and no-fire twins;
  most have 8–12, scope 8, slug 8, meals 6, day count 5).
- CTA gap not fixed (out of scope): a social text whose CTA sits in the second-last paragraph with one more paragraph
  after it passes `check_cta` (the closing window is the last two paragraphs) although the policy says "last
  sentence". `ensure_cta` keeps it. Left for W5 (channel shapes).
- Behaviour changes on Dev after deploy (T9 gates, no paid call): F4 meta band 120–158 → 140–155; F1 holds only trip
  numbers; F18 place rarity warns; the FAQ filter drops source-less topics; T2 meal lines copied from the source day
  pass; slugs cut at a word boundary. Prompt versions move (t9_write v4, t9_repair v2, writing_memory_eval v3,
  s1/t2_brand_audit v2, a3_place_card v2) — CIS 387 registers them.
- The paid 3-piece writer smoke (critic fix F9, ~$0.50) was NOT run here: it is a live step of the code PR.
- Not in this lane: critic fix F10 (A3 input check, unanchored-atom share) is the W1 input measurements item; F13
  (review capacity for A1 rows) is a decision for the tech lead; both stay on AA-1047.
- `config_fp` gets a new component `policy`: every release fingerprint moves once at deploy (reverify reason
  `prompt_version_mismatch`).

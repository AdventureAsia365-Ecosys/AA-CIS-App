# AA-1049 — T9 SEO builder: a meta under the band floor (lane l19, S273)

Trigger: the S273 paid trial with `T9_SEO_BUILDER=on` (audit tenant) held a blog piece on `F4_seo_surface`: the meta
was 119 characters, the policy band is 140-155 (`writing_policy.yaml` `bands.meta`, D5), after 2 SEO-fit calls (one
per attempt). Tech lead decision: keep the switch off and fix the builder first. Everything below runs only with
`T9_SEO_BUILDER=on`; the default (`off`) is main's code path, byte for byte in behaviour.

## Root cause (three parts)

1. **The builder could only shorten.** `seo_builder.build_seo` tried whole-sentence / separator PREFIXES of the
   writer's value. A meta under the floor has no passing prefix, so it went to the fit untouched.
2. **The fit is told to aim low.** The registered `_SEO_FIT_SYSTEM` says "aim for 130-145 characters ... ONE complete
   sentence of about 20 words" (lesson S233: a model overshoots a character target, so the aim sits under the band).
   That is right for an over-long meta. For a short one it pulls the answer to about 120-135 characters; the user
   prompt said "too short: must be 140-155" (S272), but the system rules contradicted it. The trial answer stayed short.
3. **Nothing after the fit could lengthen.** With the builder on, the post-fit step only shortened the fitted value
   and the retry call was gone (D-seo-2), so a short fitted answer went straight to F4.

## Decisions

- **Code first: complete the meta with ONE cited sentence of the post** (`seo_builder.cited_sentences`,
  `meta_extensions`). A candidate is the writer's meta (or one of its whole-sentence prefixes, ending a sentence)
  followed by one whole prose sentence of the post whose citation tags ([R:<id>] / [F:<id>]) all name a source line
  of this piece (`seo_builder.source_ids`: `context.atom_id`, the `route_segments` ids, the `[Fact id=...]` ids of
  `facts_text`), tags removed. Excluded: headings, bullets, the FAQ, questions, a lowercase start, and a sentence that
  leans on the one before it (`LEANING_STARTS`: "It", "They", "This", "Then", ...). The SAME `meta_ok` predicate of
  `_fit_seo_meta` decides (band, keyword, scope places, unsupported products, date authority, rarity,
  authenticity); the first passing candidate wins (the whole meta before its prefixes, post order for sentences).
  Prefixes are tried before completions, so an over-long meta keeps D-seo-1 behaviour.
- **Why a cited post sentence and not a template:** the added clause is the writer's own text that already cites the
  source, so code adds no fact the post does not state from its source (writer source fidelity). The F9 judge sees
  the meta every attempt, as before. A template or a place-card tail was withdrawn in S272 (wrong country on
  cross-border inputs) and stays out.
- **Escalation: one fit call that states the band.** For a meta still under the floor after the code step,
  `_fit_seo_meta` passes `meta_band=(140, 155)` (read from the policy registry) to `generate.fit_seo_fields`, which
  adds one user-prompt line (`_META_BAND_LINE`): "META LENGTH FOR THIS POST: 140-155 characters, never under 140. This
  replaces the aim and the 20-word guide in the rules: write one or two complete sentences ...". Two sentences let
  the builder trim an overshoot at a sentence boundary. An over-long meta gets no band line (the low aim is what
  works there, lesson S233).
- **Re-check, then hold with a reason.** The fitted answer is merged only when it passes `meta_ok`; then the builder
  runs again on it (prefix, else completion). If the meta is still outside, `_fit_seo_meta` logs
  `t9_seo_meta_outside_band` (chars, band, fitted chars, band stated, number of cited sentences, reason) and F4 holds
  the piece with its unchanged message ("meta description is N characters — outside 140-155"). Still at most one fit
  call per attempt.

## Changed

- `services/acp_content_writing/seo_builder.py`: `source_ids`, `cited_sentences`, `meta_extensions`,
  `LEANING_STARTS`; `build_seo(..., meta_sources=())` (default = old behaviour, a log line says "completed").
- `services/acp_content_writing/service.py` `_fit_seo_meta`: under `on` only — cited sentences from the body, the
  `meta_band` kwarg for a meta under the floor, the post-fit builder with the same sources, the warning.
- `services/acp_content_writing/generate.py` `fit_seo_fields(meta_band=None)`: the band line in the USER prompt only.
  `_SEO_FIT_SYSTEM` is unchanged, so no prompt version bump and no migration (prompt registry check: 0 problems;
  change-control lock unchanged).
- `scripts/quality/replay_aa1049_t9.py`: declared ids D-seo-3 (completion by a cited sentence) and D-seo-4 (the fit
  request states the policy band), each checked against its shape independently of seo_builder: the anchor check
  re-reads the sample's own source ids and the post's tagged prose sentences (`_cited_post_sentences`,
  `_completion_shape`), the band must equal `writing_policy.band("meta")` and be sent only for a meta under its floor
  (`_band_problem`); any other request change stays UNDECLARED.

## Tradeoffs

- **Coverage of the code step is small on today's data.** Upper bound on the S273 real-data export (24 blog pieces)
  plus the golden fixture (15 blog): 14 metas under the floor (124-137 chars); even if EVERY prose sentence were
  cited, the code step completes 1 of 14. A 124-137 meta needs a 3-30 character sentence; post sentences are longer.
  The code step helps metas far under the floor (about 85-115 chars); the 120-139 case relies on the band-stated fit.
  Clause-level composition (appending a phrase inside a sentence) would cover more but is the hand-written text
  composition that did not converge in three S272 review rounds, so it is out.
- **The band line contradicts the registered system rule on purpose** (it says it replaces the aim). Changing the
  system prompt would need a prompt version, a migration and an eval; the user-prompt line is opt-in only and can be
  promoted to the system prompt once the trial measures it.
- The replay cannot judge the fitted answer (a fit call is a cassette miss): whether the band-stated fit lands in the
  band is a paid-trial question.

## Evidence (local, no paid call)

- Unit: `tests/unit/test_aa1049_seo_builder_meta_band.py` (short 106 / 121 chars, in band, long, the caller's
  predicate, end to end through `_fit_seo_meta` with the switch on and off, the band line in `fit_seo_fields`);
  replay harness `test_s273_*` (D-seo-4 only with `--opt-in`, a non-policy band is UNDECLARED, D-seo-3 on a tagged
  body, an uncited completion is UNDECLARED, a tag of another piece is no anchor).
- Replay (base = CIS origin/main export, branch = this worktree):
  - golden fixture-1.2.0 (30 pieces): switch off 0 declared / 0 undeclared (7 MISS = main); switch on + `--opt-in`
    D-seo-4 x5, MISS x2, 0 undeclared.
  - real-data export of S273 (60 pieces, 24 blog): switch off 0 declared / 0 undeclared (11 MISS = main); switch on
    + `--opt-in` D-seo-4 x9, MISS x2, 0 undeclared. Stored bodies are tag-stripped, so D-seo-3 cannot show there.

## Should know

- Live check before turning the switch on: rerun the audit-tenant trial (a few blog pieces with metas under the
  floor) and read `t9_seo_built_in_code` ("completed ... with one cited sentence") and `t9_seo_meta_outside_band` in
  the worker log; F4 holds should drop to the fitted-meta misses only.
- `_seo_predicates` in the replay is unchanged (no acceptance predicate moved).

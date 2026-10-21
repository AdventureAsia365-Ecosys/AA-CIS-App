# AA-925 PR 2 — one day parser, the S1 day oracle (shadow), the S2 cut signature (shadow)

## Decisions

- **One parser, in a leaf module.** `services/content_generation/day_parse.py` holds the "Day N" marker regex,
  the number words and `parse_duration`; it imports nothing from prompts / graph / seo_meta_utils (they import
  it), so there is no cycle with the oracle. `prompts` re-exports `_DAY_MARKER_RE`, `_NUM_WORDS`, `_day_num`.
  - `prompts._estimate_day_count` = `parse_duration(...).day_count` (sub-day → 1, nights only → nights + 1,
    weeks × 7, a range → its low end, as the first-integer reading gave).
  - `seo_meta_utils.tour_days` = `parse_duration(...).exact_days`: None for a range or a sub-day tour, so the
    fact tail never states a length the source does not pin down.
  - `parse_source_day_word_counts` stays THE day splitter (source_facts says "never a second one"): same
    split, plus a `markers` key (`(day_from, day_to, line_idx)`, ranges expanded). The marker regex gained a
    capture group for the range end; group 1 and the `sub()` behaviour are unchanged.
  - `output_days` reuses `itinerary_utils._CANONICAL_DAY_RE` (imported lazily) and keeps duplicates, which
    `parse_canonical_itinerary_days` (a dict) hides.
- **Range guard.** "Day 1 - 3 hours drive" is day 1, not days 1–3 (a unit after the number), and a backwards
  or > 14-day span is a single day.
- **Oracle codes** (`services/quality/day_oracle.py`, pure): `DAY_SUBDAY_SPLIT`, `DAY_OUTPUT_DUPLICATE`,
  `DAY_OUTPUT_ORDER`, `DAY_OUTPUT_GAP`, `DAY_OUTPUT_EXTRA`, `DAY_DEPARTURE_MIDTRIP`, `DAY_COUNT_VS_DURATION`,
  `SOURCE_DAY_CONTRADICTION` (class `source.contradiction`: the writer is not charged and the count check is
  not run), `DAY_CHECK_SKIPPED` (no markers, no usable duration — PR 3 turns it into the `day_check_skipped`
  degraded entry). A gap the source itself has is not charged; `ITINERARY_DAY_COUNT_MISMATCH` is unchanged
  and still owns "a source day is missing".
- **Shadow first (D5).** `DAY_ORACLE_MODES` ships every code in `shadow`: findings go to `quality_shadow`
  (state + `generated_content.metadata.quality_shadow`), no score, no verdict. An `enforce` code is added to
  `failure_codes` only when `not is_tenant_rewrite` (T3's `_t3_structural_issues` and `human_edit_gate` read
  `_HARD_BLOCK_CODES`; T2 writes from the master `aa_*` text — lesson AA-800) and becomes:
  `_HARD_BLOCK_CODES` + `HARD_CODE_REPAIR` "llm: one haiku->sonnet rewrite (AA-736), else review" +
  `admin_pipeline._REWRITE_CLASS_HARD_CODES`, all derived from `enforced_codes()` so the S218 guard holds.
  `SOURCE_DAY_CONTRADICTION` and `DAY_CHECK_SKIPPED` can never be enforced.
- **S2 cut signature** (`seo_meta_utils.meta_cut_signature`): a final word that is a number compound
  ("20-kilometre"), a hyphenated modifier ("two-day", "family-run"), a modifier that needs its noun
  ("expert", "guided") or a weak ending. Shadow only — never a hard code (AA-794: nothing repairs it
  deterministically). The report script uses the same function.
- **validate_node owns `quality_shadow`**: recomputed on every validate (the latest content's findings), so a
  repair that removes a duplicate day also removes its finding.
- ContentState gains `quality_shadow` and `degraded` (PR 3 fills `degraded`); `v1_pipeline._rewrite_tour`'s
  initial state, its returned dict and `scripts/aa729/score.graph_state` carry both (test_aa729_golden_set
  checks every key is declared).
- Review Queue (`reviewModel.ts`): the enforceable `DAY_*` codes are red (product truth); they only appear in
  `failure_codes` once enforced.

## Changed

- New: `services/content_generation/day_parse.py`, `services/quality/{__init__,day_oracle}.py`,
  `scripts/aa925/{__init__,day_oracle_report}.py` (force-added: `scripts/` is gitignored),
  tests `tests/unit/quality/test_{day_parse,day_oracle,day_oracle_report}.py`, Q0 cases
  `a1_day_structure.json` (8) + `a1_truncated.json` (2), fixture `a1-nepal-14d`.
- `prompts.py`, `seo_meta_utils.py` (`tour_days` wrapper, `meta_cut_signature`), `graph.py` (validate_node,
  ContentState, the derived hard-code sets), `admin_pipeline.py` (metadata + rewrite class), `v1_pipeline.py`,
  `scripts/aa729/score.py`, `reviewModel.ts`, `CONTEXT.md` (A1 day oracle paragraph).

## Q0 cases (all red on the PR 1 tip — checked locally with the CI script)

| id | class | detector | expected |
|---|---|---|---|
| a1-day-subday-split | struct.day_structure | DAY_SUBDAY_SPLIT | fire |
| a1-selector-subday-estimate (selector) | struct.day_structure | DAY_SUBDAY_SPLIT | no_fire (red on base: the old estimate made the nudge rewrite a correct one-day itinerary) |
| a1-day-duplicate | struct.day_structure | DAY_OUTPUT_DUPLICATE | fire (ITINERARY_DAY_COUNT_MISMATCH does not) |
| a1-day-extra | struct.day_structure | DAY_OUTPUT_EXTRA | fire |
| a1-day-departure-midtrip | struct.day_structure | DAY_DEPARTURE_MIDTRIP | fire |
| a1-day-count-vs-duration | struct.day_structure | DAY_COUNT_VS_DURATION | fire |
| a1-day-clean-14d | struct.day_structure | DAY_COUNT_VS_DURATION | no_fire (negative twin, exempt) |
| a1-source-day-contradiction | source.contradiction | SOURCE_DAY_CONTRADICTION | fire, count check suppressed |
| a1-meta-cut-signature | struct.truncated | META_CUT_SIGNATURE | fire |
| a1-meta-cut-signature-clean | struct.truncated | META_CUT_SIGNATURE | no_fire (negative twin, exempt) |

## Tradeoffs

- **The `_estimate_day_count` / `tour_days` unification changes writer input** for sub-day tours, "N nights",
  "weeks" and "7D/6N": the fallback split (generate nudge targets, `source_facts`, the grounding per-unit
  source) and the meta fact tail. "13 Nights 14 days" now estimates 14 (was 13). It is small and covered by a
  red-on-base selector case; it can be split into its own PR on request (Q3 territory).
- A range duration ("10–12 days") no longer gives `tour_days` = 12: no "over 12 days" tail for a tour whose
  length is a range.
- `DAY_DEPARTURE_MIDTRIP` and `DAY_COUNT_VS_DURATION` are expected to stay shadow (wording / optional
  extension days); the precision read decides.

## Should know — CI-only unit failure in `test_aa660_decide` (not caused by this diff)

- `test_cached_verdict_is_reused_with_todays_floors_and_no_call` failed on this PR and on `main` (run of
  66c8764): `executemany` awaited once. The Jev cache-hit rollup also flushes by time
  (`_HITS_FLUSH_S` = 60 s), and its clock `_hits_flushed_at` is set when `decide.py` is imported, at
  collection. `reset_decision_memo()` (the autouse test reset) cleared the counts but not the clock. In CI
  with coverage the test runs about 63–68 s after the session starts, so its first cache hit flushed. Locally
  and on the last green `main` run it ran 36–47 s in, so it passed. The Q0 corpus tests (they sort first)
  pushed CI over the line.
- Fix: the test-only reset also restarts the clock; guard tests cover the time-based flush and the reset.
  Production flush behaviour is unchanged (a hit pending past 60 s goes out on the next ledger write, by
  design).

## Should know — the oracle run (Done-when 2)

- Before merge the image has none of these modules. Archive the PR's detector files with the scripts and
  load them by path (`--oracle-dir`): `services/quality`, `services/content_generation/day_parse.py`,
  `prompts.py`, `seo_meta_utils.py`, `scripts/aa925`, `scripts/aa729`. `/app` stays first on `sys.path`; the
  loader registers the archive modules under their package names.
- Output: `s3://<bronze>/scripts/out/aa925/<run>/day-oracle.json`, counts per code × set × status × country,
  the leak count (fires while validate's `ITINERARY_DAY_COUNT_MISMATCH` does not), `DAY_CHECK_SKIPPED`, the
  cut-signature count, ≤ 30 random samples per code (tour id, day numbers + titles, no body text).
- Precision read per code: every hit when ≤ 30, else the 30 samples; `--wilson K N` prints the Wilson 95%
  lower bound. A code with precision ≥ 0.70 may flip to `enforce` (A1 only) in this PR before merge, with the
  counts in the PR body. Sample tables go to the private root audit folder, never this repo.

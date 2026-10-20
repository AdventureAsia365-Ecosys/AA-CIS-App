# AA-779 (S245) — real-question source chain for blogs, part (A) + off-meaning PAA

Scope item 2 part (A) and item 3 of AA-779 (updated description). Parts (B) admin SERP top-up and
(C) Autocomplete are not in this change; the chain is built so they plug in as two more sources.
Agent: Claude Code (coding lane `aa779-cis`). Runtime cost: $0 (DB reads only).

## Decisions

- **One module owns the chain**: `services/acp_shared/real_questions.py`. Order (each tier at most
  12 raw questions, 30 in all, duplicates dropped across tiers by a folded key):
  1. `segment_match` — `acp_contract.atom_matches` (kind `question`) on every atom of the piece's
     Segments (Segment-level, so a question landed on another tour's atom of the same Segment counts).
  2. `place_paa` — `acp_contract.search_demand.people_also_ask` of keywords that share a distinctive
     word with one of the piece's places (`atom_segment.canonical_place` + `tour_atoms.place`), any
     market, English questions only.
  3. `tour_paa` — the request snapshot's tour-level PAA (the only source before).
- **Stale `atom_matches` rows**: the table is upsert-only, so a question a later landing pass
  dropped keeps its row. Per Segment only the `questions_count` newest rows (by `matched_at`) count —
  exactly what the last landing pass claimed. When `questions_count` is NULL (invalidated, not yet
  recomputed) rows newer than `questions_computed_at - 1 day` count.
- **Place words**: folded tokens of ≥ 4 letters that are not a kind of place (`PLACE_KINDS`,
  `hygiene.COMMON_CAPS`: lake, province, desert…) and not the tour's country — 'mongolia' would match
  every Mongolia keyword. Whole-word match against the keyword (`keyword_words`); ranked by shared
  words, then volume, then market US/UK/AU/DE/FR/NL.
- **English only**: DE/FR/NL markets can return German/French/Dutch PAA. A question must open with an
  English question word/auxiliary and carry no German/French/Dutch function word (`looks_english`).
- **Off-meaning PAA (item 3)** is dropped in two places: in the chain merge (word-sense cues, so T8
  never sees them) and in the existing scope filter `quality_gates.paa_for_scope` (word-sense cues
  plus name-as-common-noun tests against the moment's own name tokens: 'What is a moron?', 'Is moron
  offensive?', 'call someone a moron'). The Mörön case: the town's DFS keyword is 'moron', whose SERP
  asks about the insult; before this, 'Is moron a bad word?' passed `paa_for_scope` because 'moron'
  folds to the seed name 'Mörön'.
- **T8 (angles)**: for a `blog` request the angle prompt and the measurable ranking
  (`asked_questions`) get the chain (real place/Segment questions first, then the tour's). The
  snapshot stores what the angle LLM saw (AA-501) plus `tour_people_also_ask` (the tour-level list),
  question texts only. Non-blog requests: unchanged, byte-identical snapshot.
- **T9 (write)**: `start_write` re-reads the chain live for a blog (scope = the piece's kept moments'
  atoms), with the snapshot's `tour_people_also_ask` (or `people_also_ask` for a pre-change snapshot)
  as the tour tier. `context["people_also_ask"]` stays a list of strings (old job payloads keep
  working); `context["real_questions"]` carries `{q, source}`.
- **Admin record, never tenant**: `content_piece.writer_input` (admin-only projection, AA-769) gains
  `search_questions` (in-scope questions the writer got, with source), `question_sources`
  (candidates / kept per source) and `faq_questions` (each final FAQ question with the real source it
  matches, or `writer`). The tenant API returns the request snapshot, which holds texts only — never a
  source label. The admin Content Quality drawer shows the three.
- **FAQ question ↔ real question match**: same text after folding, or ≥ 80% shared content words
  (the measure the S245 advice proposes for the Done-when); matched first against the questions the
  writer got, then every candidate.
- **B/C plug-in point**: `DB_SOURCES` (a tuple of `QuestionSource` objects with `name` + `fetch(conn,
  scope)`); `serp_topup` and `autocomplete` labels are reserved. Their admin jobs buy and store; their
  readers go after `place_paa`. This module never calls DataForSEO (AA-646).

## Changed

- New `services/acp_shared/real_questions.py` (chain, sources, language / off-meaning checks, admin
  record helpers).
- `quality_gates.paa_for_scope`: drops off-meaning questions (`is_off_meaning(q, seed_names)`), after
  the unanswerable-topic check. Nothing else in the filter changed.
- `acp_content_writing/service.py`: `snapshot_tour_paa()`; `start_write` collects the chain for a blog
  (non-blog unchanged); the job labels the kept questions (`context["search_questions"]`) and, before
  `_finalize_piece`, the final FAQ questions (`context["faq_questions"]`); `writer_input_snapshot` adds
  the three admin keys for a blog whose payload has `real_questions` (older payloads: unchanged shape).
- `acp_angle_gate/service.py`: `_blog_search_demand()` (T8 blog only, ≤ 12 questions for the prompt and
  ranking) and the `tour_people_also_ask` snapshot key.
- Admin UI: `content-quality/PieceDrawer.tsx` "Search questions" block inside "What the writer was given";
  `shared.ts` types.
- `scripts/aa779_question_coverage.py`: read-only coverage measure for Done-when line 1 (ECS exec, $0).
- CONTEXT.md: one S245 paragraph after the AA-779 FAQ paragraph.
- No migration (no new column: `writer_input` is jsonb; the snapshot is jsonb).

## Tradeoffs

- **Real-first can push the tour's PAA out.** Each tier is capped at 12 and the chain at 30, and
  `paa_for_scope` keeps the first 6 in-scope ones in chain order, so a blog with many Segment/place
  questions never sees the tour's. That is the point of the issue (the tour PAA is about the tour title,
  not the piece), but a very relevant tour question can lose its seat.
- **Place matching is by spelling.** A keyword is claimed by whole folded words of the place names, so
  'khuvsgul' / 'hovsgol' spellings of Khövsgöl are missed by `place_paa` (the `segment_match` tier, which
  matched by embedding, still covers them). No fuzzy matching: a wrong place's questions are worse than
  fewer questions.
- **English check is a word list**, not language detection: a question must open with an English
  question word/auxiliary and carry no common German/French/Dutch function word. Google PAA questions
  always open that way; an English question that opens otherwise ('Lake Khövsgöl: how deep?') is lost.
- **Off-meaning test is lexical.** Word-sense cues (mean/meaning/definition/slang/insult/bad word/
  pronounce/etymology…) drop a question anywhere; 'rude'/'offensive' only right after the place name, so
  'Is it rude to refuse tea in a ger?' stays. A question naming the place's meaning ('What does Khövsgöl
  mean?') is dropped too — the seed rarely answers it. Not covered: an off-meaning question with none of
  these cues (no example yet; Jev already scopes the `segment_match` tier).
- **`segment_match` staleness rule** relies on the last landing pass writing every claimed question with
  a fresh `matched_at`; an atom shared by two Segments could let one Segment's newer match crowd the other's
  window. Rare (alias members), and every question still goes through `paa_for_scope`.
- **T8 relevance when the tour has no `seo_context`**: a blog with only place/Segment questions gets
  `score_dfs_relevance([])` (the documented default) in the angle prompt's SEARCH DEMAND block, which
  was absent before for such a request.

## Should know

- **$0 runtime.** Only SELECTs on `acp_contract.atom_segment`, `atom_segment_member`, `atom_segment_alias`,
  `atom_matches`, `search_demand`, `tour_atoms`, `silver_aa_internal.raw_tours` (all granted to `cis_app`).
  `start_write` (HTTP request) gains ~4 small queries for a blog.
- **Tenant visibility.** The tenant API (`/v1/angle-gate/requests/{id}`) returns `dfs_paa_snapshot`; it now
  may hold `tour_people_also_ask` (question texts, same kind of data it already showed) but never a source.
  Sources live only in `content_piece.writer_input` (read only by `admin_content_quality.py`).
- **Integration test** `tests/integration/test_aa779_real_questions_sql.py` runs the SQL on the real
  schema inside a rolled-back transaction; `atom_matches` (migration 161, needs pgvector) is missing on
  CI's postgres:15, so the test creates a same-columns stand-in inside the transaction.
- **Done-when line 1** (≥ 80% of blog Subjects with ≥ 3 real in-scope questions) is measured once after
  deploy with `scripts/aa779_question_coverage.py` (read-only, lower bound without Facts). If (A) alone
  is below 80%, the `below_3` list is the target set for (B)/(C).
- The A1 (S1) writer prompt is untouched; the blog writer prompt template is unchanged (only which
  questions fill the existing SEARCH QUESTIONS block).

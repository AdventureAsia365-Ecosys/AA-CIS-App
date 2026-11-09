# AA-1055 (lane l11, S274) — qualify namesake keywords, drop non-places and garbled phrases in the keyword map

Why: an independent 3-judge panel scored 90 sampled keyword|place pairs of research run R1 (root
`docs/audits/2026-11-09-S274-session/aa1055-live/r1-relevance-panel.json`): kept precision 93 % (80/86), all 4 Jev
rejections correct. The 6 wrong kept keywords were namesake place names bought without a locality (a deity or saint
plus "temple" / "cathedral", a hotel name, an "Eagle's Nest"), one non-place (a plant) and one garbled phrase. A
namesake keyword's volume is global, so it inflates demand and can push a wrong Subject over the Blog bar. The tech
lead asked for this before research run R2.

## Decisions

- **One pure module, `services/acp_contract/keyword_qualify.py`** (no I/O, no LLM): `is_namesake_name`,
  `not_a_place`, `garbled_reason`, `qualify`, `unqualified_words`. The map stage and the ranking side read the same
  rules (lesson "one value computed on two code paths shares one function").
- **Namesake rule**: every word of the name (parenthetical included) is a TYPE word (ranking's kind nouns + landmark
  nouns: cathedral, church, mosque, hotel, fort, palace, nest, ...), a NAMESAKE word (deities, saints and feasts,
  honorifics, stock descriptive names, famous ranges and animals) or a ranking qualifier, with at least one type word.
  One distinctive word clears it ("Zarvok Temple"); a parenthetical locality clears it too.
- **Qualify, do not just drop**: for a namesake place the plain name becomes `"<name> <most specific locality>"`
  (place card locality, else region, else country; else the most common tour country). Every other keyword must
  contain one locality phrase or it is dropped. No locality known: nothing is bought for the place.
- **Not a place**: the place card's `what_it_is` fact decides ("X is a species of palm" / "a sweet dish"); a type word
  before the noun ("a lake known for fish") or a site word anywhere ("sacred", "temple grounds") keeps it a place.
  Without a card, the head noun of the name (tree, plant, spice, dish, ...). Nothing is bought for a non-place.
- **Garbled** (never applied to the plain place name): a number glued to a word ("km9", ordinals excepted), a brochure
  verb nobody types ("sightsee", "marvel", "admire"), fewer than 2 meaningful words.
- **Stored, not silently dropped**: a rule-dropped keyword is a `place_keyword` row with `jev_zone = 'skipped'` and
  `jev_mode = 'rule:<reason>'` (`rule:unqualified`, `rule:not_a_place`, `rule:garbled_<why>`). `keywords_from_map`
  leaves them out; a map of only rule rows is a usable map with nothing to buy. No migration: `jev_mode` is free text
  and `skipped` is already allowed by the 393 CHECK.
- **Prompt**: `PROPOSE_SYSTEM` gains the namesake / only-places / no-verb rules and "2-6 words", so `PROMPT_VERSION`
  changes (every place is re-proposed the next time it is stale anyway). The listing line of a namesake place carries
  `— always add: <locality>`.
- **Targeted rebuild of fresh places**: R1 places are fresh for 365 days, so a version bump alone would never touch
  them. `_plan` also plans a FRESH place whose stored map is of an older version and that the rules would change
  (`needs_requalify`: namesake or not-a-place name, or a garbled stored keyword that was bought). It is planned stale
  in every requested market and in the SERP tier (`_Place.requalify`), so its new keywords are bought and one SERP for
  the qualified keyword. Summary key `places_requalify`.
- **A place left with no keyword is logged researched** (volume per market + SERP, counted in `serp_no_volume`), so it
  is not planned and counted stale on every run.
- **Ranking side**: `compute_demand`, `demand_candidates` and `_candidate_questions` skip, for a namesake place, a
  keyword made only of the place's own words plus filler and generic search modifiers (hours, tickets, history...).
  Without this, the global "<namesake> temple" rows bought in R1 keep winning the (fit, volume) tie-break for 365 days.

## Changed (vs. the lane brief)

- The brief scoped the fix to the keyword map stage; the ranking guard was added because the already-bought R1 rows
  stay in `search_demand` and are claimed by name, so the map fix alone would not remove the inflation.
- Non-place detection is card-based when a card exists (the brief's "when the place card type says so"); the card has
  no type column, so the `what_it_is` fact is read. The name head-noun fallback only applies with no card.

## Tradeoffs

- Deterministic word lists over another LLM / Jev question: free, testable, explainable; a famous unique place with a
  namesake-shaped name ("Golden ...", "Grand Palace") is qualified too. Its qualified keyword ("<name> <city>") is a
  real search, so the cost is a smaller but honest volume.
- `NAMESAKE_WORDS` is a curated list: an unlisted namesake word is not caught. Jev (`a3_keyword_belongs`) still judges
  every non-plain keyword after the rules.
- The ranking guard has no locality (no card read on the hot path): it refuses only keywords made of the name's own
  words + generic modifiers, so any extra distinctive word (a town) lets the claim through.

## Should know

- Read-only count for the orchestrator: `scripts/aa1055/keyword_qualify_count.py` (ecsx, after the deploy): namesake /
  not-a-place / garbled counts over every mapped place, the places the next run re-proposes and their Segments, and
  the per-market volume of unqualified namesake keywords the claim now ignores (and how many alone were >= 1,000).
- The next research run (R2) pays for the requalified places: about 1 LLM call per 15 places, a Jev verdict per
  non-plain keyword, their new keywords in the volume tasks and one SERP each. The dry run shows them in
  `places_requalify` and in the estimate.
- The ranking change lands on the next recompute of the affected Segments (the research run of the requalified
  places enqueues it; otherwise the nightly landing sweep / any platform recompute).
- Tests: `tests/unit/test_aa1055_keyword_qualify.py`; SQL on real Postgres in
  `tests/integration/test_aa1055_buy_side_sql.py` (place card read, rule rows round trip, targeted requalify plan).

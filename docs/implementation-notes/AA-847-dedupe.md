# AA-847 — Problem 2: automatic duplicate-place merge (design, S246)

Design only, no code in this repo yet. Full design, prototype and labelled pair set: root
`docs/audits/2026-10-21-S246-aa847/dedupe/` (`README.md`, `dedupe_proto.py`, `pairs.json`, `eval-r1.2*.txt`).
Kept in its own notes file so parallel AA-847 lanes do not edit the same file (lesson S242).

## Decisions
- **Owner decision S246 (2):** duplicates are merged automatically; the admin fix (`validity_fix` duplicate_place)
  stays as an override, not the normal path.
- **One decision function** `services/places/dedupe.decide(a, b)` → `auto_merge` / `llm` / `keep`, used by the new
  `place_dedupe` job, by the in-tour dedupe in `place_extract` (rules band only) and by validity V2. Order: hard keeps
  (country, number / qualifier differs, kind matrix incl. town vs landmark, kind radius) → id evidence (same
  `geocoder_place_id`, same place card) → score name × kind × distance (+ Geoapify evidence, + embedding) with
  thresholds 0.80 / 0.30; ambiguous name or kind relations are capped at the LLM band.
- **Kind words are no longer dropped from the name:** a name made only of kind / weak words ("Royal Palace") keeps
  them as its identity; kinds are hard (English) or soft (local words that also start proper names).
- **"Town vs landmark never merge" needs the settlement status**, not only the name: overnight stop
  (`geo.tour_day.overnight_destination_id`), Geoapify `result_type='city'`, or a town word = hard settlement; `suburb` /
  `district` = soft (goes to the LLM, because "Ben Thanh" may be geocoded to the ward while the tours mean the market).
- **Middle band → one cached model call**, stage `a3_place_dedupe`, role **`validate`** (not `judge`: judges must be
  non-Anthropic, ADR 0006 §5 / ADR 0008; this check is scored by nobody), haiku primary, `gpt-6-luna` fallback, through
  `LLMClient.generate` (one `llm_call_log` row per call). Merge only on `same` with confidence ≥ 0.8; `unsure` keeps
  the places (no human queue). Cache `geo.place_pair_verdict` keyed by an input hash; undo / admin pins are sticky.
- **Reversible:** `geo.place_merge` log (every factor, every repointed row, chain flattening), undo by merge id with
  drift check (409) and a `different` pin; hard-rule re-check of old merges each run; circuit breaker per country.
- **TripPlanner old ids:** new `catalog.v_destination_redirect` (merged id → keeper) instead of writing TripPlanner rows.

## Changed
- Nothing in this repo yet. Planned: `services/places/dedupe.py` (new), `validity.is_same_place` and
  `pipeline._in_tour_keepers` delegate to it, `validity_fix` duplicate_place writes `geo.place_merge`, new job kind
  `place_dedupe`, migration ≥ 255 (two geo tables + `geo.destination_vector` + stage row + spend budget row + redirect
  view), `/admin/places` Merges tab, `tests/unit/test_aa847_dedupe.py` (the 105-pair set; the 22 pairs of
  `test_aa847_validity.py` are a subset and keep their expected results).
- Versus the brief: the LLM stage is `validate`, not `judge` (vendor rule); the decision mode is a CIS runtime setting
  (`off | dry_run | apply`, default `dry_run`), not a new environment variable.

## Tradeoffs
- Rules alone: precision 1.000, recall 0.764 (42/55), 0 false merges also with metadata stripped (0.709 / 0.636
  recall). Recall ≥ 0.9 only with the LLM band (0.982 if the model answers the 13 band pairs right). Pushing more pairs
  into the rules (e.g. bare name within 100 m of a built place) would raise recall on this set but would merge "Long
  Bien" (district) into the bridge — the ambiguity is real, so the model reads the day text.
- A kind only keeps names apart or sends them to the model: a missing synonym costs a model call or a missed merge,
  never a wrong merge.
- Linear features use a 50 km point radius until Problem 1 stores a line; then line-to-line ≤ 1 km.

## Should know
- **The pair set is author-labelled; 79 of 105 pairs are constructed** (real places, approximate coordinates), and
  three rule fixes followed the first run (r1 → r1.2). Before `apply` is switched on: the paid measurement run (≈ $0.11,
  Haiku + Luna, 75 calls each) and a blind sample of ≥ 60 Dev pairs from a `dry_run` on Laos + Vietnam, labelled by an
  agent panel without seeing the decision.
- The strongest signal is `geocoder_place_id`: with ids stripped, the cross-language pairs (Van Mieu, Wat Arun,
  Tiger's Nest) are missed. Never clear it on re-locate; the embedding candidate rule covers rows without it.
- `place_geocode` stays the only writer of coordinates; `place_dedupe` writes `duplicate_of` and the repoints; both
  take one advisory lock. ADR 0007 needs the amendment proposed in the design §8.4.

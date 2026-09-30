# AA-694 A3-2 — Segment activity type: verb rule vs atom `activity_type`

Design: `docs/architecture/at-series-v2-design.md` §4.2 A3-2 ("Choice when the two disagree (transit matters for A3-5)").

## Decisions
- **Where the two opinions come from:** the verb rule is `classify_exclusion()` (→ `transit`); the second opinion is the member atoms' `activity_type` from atomize, counted as a **typed majority** (`atoms_say_transit()`; untyped atoms do not vote, a tie is not transit).
- **When Jev is asked:** only when both opinions exist and disagree. Not asked for `unnamed_place` Segments (that exclusion is about the place, not the activity) or Segments with no typed atom.
- **Question:** `a3_activity_type`, kind `choice`, labels `transit` / `experience` (migration 189, shadow). State = `{moment}` ("place — action"), subject key per moment → one Verdict per distinct moment, cached.
- **Applying a pick** (`exclusion_after_pick()`, only when enforced + confident): `transit` → excluded as transit; `experience` on a rule-transit Segment → included, unless its place names nowhere (`unnamed_place`). No trusted pick → the rule stands.
- **One resolver for both consumers:** `resolve_exclusions()` replaces the direct `classify_exclusion()` calls in `precompute_question_landings()` (A3-5 landing) and `run_atom_ranking()` (Score). Route reads `atom_ranking.excluded_reason`, so it follows without a change.

## Changed
- Both Segment queries add `array_agg(ta.activity_type) AS activity_types`.

## Tradeoffs
- A binary choice instead of the full 7-value enum: only transit vs not-transit changes what Score/landing/Route do; the finer type is not used downstream today.
- "experience" explicitly covers journeys that are the activity (trek, ride, cruise), because "Kathmandu to Pokhara — cycle" is the typical rule/atom disagreement.

## Should know
- **Migration 189 must be applied on Dev before this deploys**; before that `decide()` finds no question and the rule stands (fail-open).
- Calibration still to do (Dev data): sample the disputed moments, label, pick an accept floor, then enforce. Until enforced nothing changes.
- Tests: `test_aa694_activity_type.py`. The unit suite keeps 3 failures that also fail on `main` (aa324 ×2, aa652).

## S205 — wording fix + calibration (migration 190)
- **Changed:** migration 189's criteria contradicted the rule's meaning (meals/stays under `experience`). The rule's `transit` = "not something to write about" (logistics + plain frame, Ms. Thư ADR 0019/0020). Migration 190 rewords instructions + criteria; keys unchanged, so no code change. The new wording hash drops cached Verdicts.
- **Calibration:** 160-moment sample, accept floor 0.95 → 81/81 correct, 217/400 disputed moments covered. Record: `docs/calibration/a3_activity_type.md`. Pending Nghiệp's review before enforce.
- **Should know:** the sample exposed a rule gap — `attend`/`receive`/`board`/`ride` openers exclude ceremonies, workshops and boat safaris. Jev only fixes this where the atoms disagree with the rule; fixing the opener lists themselves is a separate decision (they are ported reference data).

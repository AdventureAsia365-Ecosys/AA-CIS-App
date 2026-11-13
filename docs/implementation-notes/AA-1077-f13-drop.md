# AA-1077 lane f13-drop (S279) — the repeat drop reads both F13 repeat findings

Lane notes for one slice of AA-1077 (the main notes are `AA-1077.md`). The S279 trial-3 note said "the exact
repeats are reported only by the non-blocking `F13_hygiene` gate, which the drop step does not read". This slice
feeds the drop step every repeat the two F13 gates quote, measures the effect with a $0 replay, and records what the
code says about that premise.

## Decisions

- **Candidates = both F13 gates' repeat quotes.** `repeat_drop.candidates()` reads `hygiene.find_issues()` once: every
  `F13_repeat` finding (blocking) and every `F13_hygiene` warning that starts with `repeats an earlier sentence: '`
  (the hygiene repeat warning; other hygiene warnings such as social proof or hashtags are not repeats and are never
  candidates). A sentence both gates quote counts as `F13_repeat`.
- **One rule for both sources.** A hygiene candidate is dropped under the same rule as an `F13_repeat` one: its copy
  words match an earlier prose sentence in order with a Dice share >= the setting `cis.t9.repeat_drop_near_exact`
  (no new threshold, no literal), the blog stays at or above the F4 floor, and the invariants hold (no orphaned
  pronoun, every fact token kept, `integrity_problems` empty; the T9 caller's step contract still runs). The gates
  stay the backstop: a candidate the drop refuses is quoted by its gate exactly as before.
- **`sources` argument.** `drop_repeats(..., sources=...)` takes the gates to read (default both). The replay runs the
  old arm (`F13_repeat` only, the #1468 behaviour) next to the new one, so the effect of the hygiene feed is counted
  alone. The T9 hook is unchanged (it calls the default).
- **`trace` argument.** Each candidate's outcome as a code: `dropped`, `not_near_exact`, `floor`, `pronoun`, `facts`,
  `integrity`, with its best Dice share and the chars left. No text. The replay uses it; T9 does not.
- **Log line names the gate.** `dropped a near-exact repeat of an earlier sentence (F13_hygiene; 4,950 chars left)`.
  The prefix is unchanged (the tests and the admin F13_autofix drawer read it as before).

## Changed

- `services/acp_content_writing/repeat_drop.py`: `candidates`, `dice`, `sources` / `trace`, outcome codes; the loop
  split into a small runner class (AA-1097 complexity limit 10 per function).
- `scripts/aa1077/repeat_feed_replay.py` (new, $0, read only): per stored blog piece, the deterministic gates before
  and after the drop (old arm vs new arm) and per candidate the outcome; SUMMARY counts the pieces whose hygiene
  repeat warnings disappear and lists every gate verdict other than the two F13 gates that changes (expected none).
- Tests: `tests/unit/test_aa1077_s279_repeat_feed.py` (the feed) and `tests/unit/test_aa1077_s279_repeat_feed_replay.py`
  (the replay's pure parts and one stored-shape row through the real `PieceReplay`).
- `scripts/` is git-ignored in this repo: the replay is force-added like the other `scripts/aa1077` files.

## Tradeoffs

- Not lowering the share for hygiene candidates. A hygiene repeat warning is the weak arm of the repeat rule (two
  shared content trigrams, often a place phrase or the keyword: "elevation of roughly ... metres above sea level").
  Deleting such sentences on a looser rule would delete facts told in a new context; the setting stays the one knob.
- Not making `F13_hygiene` blocking. It is a warning by design (S234 round 4 review): the strict arm is already
  `F13_repeat`.

## Should know

- **The premise does not hold in code, and the replay is the check.** An exact or near-exact repeat (Dice >= 0.9,
  the default; between the setting's minimum 0.8 and 0.9 only a sentence that wraps a 4-5 word earlier one can escape
  `copies`, and such a short earlier line is under the hygiene rule's 6 content words too) is a `copies` match, so `hygiene.near_duplicates` reports it as `F13_repeat` ("says
  again what an earlier sentence says"), and `find_issues` then leaves it out of the hygiene warnings. The hygiene
  repeat warning is only the "two shared trigrams, not a strict repeat" arm, which is never near-exact. A unit test
  pins this (exact and one-word-changed repeats are quoted by `F13_repeat`, not `F13_hygiene`). So on today's
  detectors the hygiene feed is expected to drop nothing; it is a backstop if the detectors change. `F13_hygiene` is
  non-blocking and never holds a piece.
- **Where the trial's repeat hold really comes from.** The S279 trial-3 hold on 56b61f58 is an `F13_repeat` "copy"
  finding whose ordered Dice share is under 0.9 (a short sentence that re-tells most of a longer earlier one, or a
  longer one that wraps an earlier sentence in new words). The drop refuses it as `not_near_exact` by design. The
  replay prints the Dice of every refused `F13_repeat` candidate, so the tech lead can decide from data whether to
  lower `cis.t9.repeat_drop_near_exact` (Platform › Settings, evidence = this replay) or leave such sentences to the
  sentence repair.
- Live plan: deploy CIS api + worker, run the replay with ECS exec (`SINCE` = the phase-2 start), then one 3-piece
  paid trial on audit-atlas-hearth (<= 0.5 USD).

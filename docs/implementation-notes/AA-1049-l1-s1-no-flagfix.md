# AA-1049 — S1 train: no s1_flag_fix call left on the contract path (lane l1, S277)

Follow-up of the S1 train (`AA-1049-l4-s1-train.md`, CIS #1423). The S276 smoke (wave of 3 tours, worker
`S1_CONTROL=contract`) still made one `s1_flag_fix` call (Haiku, 0.0003 USD) on one tour.

## Decisions

- **Which path made the call.** Not flag_fix and not its `_rerepair_meta` loop: the smoke report shows
  `local_fix.routes = {}` and `fix_pass_applied = false` on that tour. It was the grounding node's source-aware sentence
  repair (`grounding.repair_and_recheck` -> `grounding.repair`, stage `s1_flag_fix` by default), which the S276 design
  had kept on purpose ("the only model repair left on the path"). That tour had 25 Jev grounding calls (the others 9 and
  34 with no repair), consistent with a first check, one repair and a re-check.
- **Replace it with a code cut, not a hold.** On the train's path `grounding_node` calls `grounding.cut_and_recheck`:
  each violating sentence is deleted when its field keeps the rest (a highlight while 3+ remain; an itinerary prose
  sentence while its day keeps another prose sentence, never a day title or a fact line; a subtitle / summary sentence
  while the field keeps another sentence), then the repair contract over the edit and the same re-check
  (`check_grounding`, Jev on the unchanged sentences hits its Verdict cache). A sentence that may not be cut stays and
  its violation goes to review (`manual_check`) as before. Holding every grounding hit for review instead would have
  sent each such tour to the queue (the S276 replay had 444 rows that needed a repair on main).
- **Route table.** `UNSUPPORTED_NUMBER` / `UNSUPPORTED_CLAIM` are `cut` rows now, and `repair_sentence` is no longer an
  S1 action: the schema refuses a row that would bring a model sentence repair back (test). The lock is updated with
  `parity:tests/unit/test_aa1049_s1_train.py`.
- **Observability.** The train report gets `grounding: {found, cut: [{field, code, day}], left}` (only when the train
  is active and grounding found something), stored with the rest of `metadata.s1_train`.

## Changed

- `services/content_generation/grounding.py`: `cut_violations`, `cut_and_recheck`, the train branch in
  `grounding_node` (main's branch untouched).
- `services/acp_content_writing/route_table.yaml|py` (rows, `S1_ACTIONS`), `services/quality/change_control.lock.json`,
  `services/content_generation/s1_train.py` (docstring), `CONTEXT.md`.
- Tests: `tests/unit/test_aa1049_s1_train.py` — the number scenario now cuts with 0 model calls (it failed on
  9539e953), every harness scenario makes 0 `s1_flag_fix` calls with the switch on, a cut unit test (what is never cut),
  a schema test. The main-recorded parity golden is unchanged and still passes (switch off = main byte for byte).

## Tradeoffs

- A cut drops the whole sentence, where the model repair could keep its supported part. In exchange the train's
  path is $0 and deterministic, and the cut never invents a replacement. A sentence that is the only one of its
  field / day is not cut (review instead), so a field never goes empty.
- The cut reads the violation's `day` (from `sentence_units`) to keep at least one prose sentence per day.

## Should know

- The S1 judge, brand LLM audit and `s1_itinerary_nudge` were already off the train's path; `s1_source_facts` is not
  called by the writer v2. After this PR the only model calls of an A1 v2 draft with the switch on are `s1_generate`
  (writer, meta, one meta retry, per draft) and Jev (`s1_grounding`).
- Live check: the dry-store replay (`scripts/quality/replay_aa1049_s1_local.sh`) on the new main must show no
  `s1_flag_fix` call in the `on` run; then the ledger query in the PR body over the next S1 jobs.

# AA-1024 — S274 lane l3: deterministic answer-fit deadline tests

## Decisions
- `tests/unit/test_aa779_faq_answer_fit.py::test_isolated_a_pair_past_the_deadline_is_kept` no longer measures wall
  time. The slow pair's call blocks on a `threading.Event` until the test releases it, and `faq_answer_fit.futures_wait`
  is patched with a stand-in that "reaches the deadline" once every other pair has replied and the slow call has
  started (the real `concurrent.futures.wait` underneath, no timeout). "Returned at the deadline, not after the slow
  pair" is asserted as `not futures[slow].done()` at return time.
- The sibling test `test_isolated_calls_count_only_the_calls_started_before_the_deadline` had the same wall-clock
  dependency (pair 1 had to reply within 0.2 s); it uses the same helpers.

## Changed
- Test file only. `services/acp_content_writing/faq_answer_fit.py` is untouched: it already imports
  `wait as futures_wait`, which is the seam.
- The `time.sleep(0.5)` that let the late call finish while `record_call_sync` was patched is replaced by waiting on
  that call's future (bounded at 30 s).

## Tradeoffs
- The stand-in asserts `timeout == deadline_s`, so the production code still has to pass the deadline to the wait;
  the timeout itself (a real clock) is not exercised by these tests any more. The orchestration (cancel queued pairs,
  keep the running one, count only started calls) is what they pin, and it no longer depends on runner load.

## Should know
- S273: the old 0.45 s budget read 0.96 s under merge-queue load and failed a CIS PR in the queue. Checked locally
  with three CPU-burning threads beside pytest: the two tests pass at 8-16 s per run (the old budget would fail).

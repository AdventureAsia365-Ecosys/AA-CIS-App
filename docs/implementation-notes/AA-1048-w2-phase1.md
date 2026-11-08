# AA-1048 W2 phase 1 — held-out objects for T2 and T9 blog, round 2 and adjudication batches (lane l18, S273)

Follows `AA-1048-ground-truth.md` (S272) and `AA-1048-import.md` (S273 l3). The tech lead approved on the S273
decision page: W2 phase 1 (generate the missing held-out objects for T2, +7 versions, and T9 blog, +14 pieces; T9
social waits; about 2.9 USD) and the adjudication order `t9-heldout-first` (T9 first, held-out claims only, batches of
at most 2-3 hours of tech-lead work). This lane makes no paid call: the generation run, the freeze, the panel and
the queue batches are the orchestrator's steps (runbook in the workspace scratch folder, not in this public repo).

## Decisions

- **The generation needs no new route.** The T2 rewrite and T9 write jobs already take an explicit object (a pending
  version, a placeholder piece). The orchestrator's run script (workspace scratch, run in the api container like the
  S273 W3 trial script) does what the tenant routes do, on an audit tenant (`kind = 'demo'`, not the pilot):
  - T2: the router's pending-version INSERT and one `t2_rewrite` job per held-out tour;
  - T9 blog: a blog Subject on a held-out tour is picked (`slate.pick_subject`), its goal starts the `t8_angles` job
    (`start_goal_generation`), the recommended angle is chosen (`choose_angle`) and `start_write` + one `t9_write` job
    write the piece.
  No `trial_flags` (main path). A cost cap is checked from `shared.llm_call_log` before every enqueue and after.
- **Adjudication batches are a queue feature, not a script.** `store.enqueue` (and the route / CLI) take
  `split`, `writers` and `max_open`:
  - `split` / `writers` keep the claims of that side / those writers (`held_out`, `t9_blog,t9_social`);
  - `max_open` caps the **open** golden_claim items of the set version in the queue after the call. The tech lead
    works one batch; the next call tops the queue up with the next items once some are resolved. A second call
    while the batch is open adds nothing.
  - Work order (`store.WRITER_ORDER`): held-out first, then `t9_blog`, `t9_social`, `t2`, `a1`, then by object
    (content sha) and position, so the claims of one piece are read together.
  - Without the three fields the call is as before (every disputed and audit claim, same answer keys).
- **Batch size assumption: 200 claims.** At about 45 seconds per queue item (read the claim, the aligned source lines,
  one click) a batch of 200 is about 2.5 hours, inside D11's 2-3 hours. `max_open` is a parameter: lower it if the
  first batch takes longer.
- **One read for the queued items** of the set (`source_ref = ANY(<panel label ids>)`) instead of one EXISTS per item:
  the open count and the "already queued" check come from the same read.

## Changed

- `services/ground_truth/store.py`: `queue_candidates(split=, writers=)` (ordered), `enqueue(split=, writers=,
  max_open=)` with `already_queued`, `open_before`, `open_after`, `remaining`, `batches_left` and `filter` in the
  answer when a field is given; `WRITER_ORDER`, `SPLITS`.
- `api/routers/admin_claim_labels.py`: `EnqueueBody` (`dry_run`, `split`, `writers`, `max_open`; extra keys refused);
  the log line keeps counts only.
- `scripts/aa1048/cli.py`: `enqueue --split --writers --max-open`.
- Tests: `tests/unit/test_aa1048_store_unit.py` (order, filters, top-up), `tests/unit/test_aa1048_import_api.py`
  (route body, 422s, policy), `tests/integration/test_aa1048_ground_truth_store.py` (top-up on Postgres as cis_app).

## Tradeoffs

- Ordering inside the queue page (`writing_memory.list_items`, newest first) is not changed: a batch is inserted in
  one transaction, so the page shows only the current batch anyway. Ordering the page would touch the shared queue
  of every source for one work list.
- Train claims stay out of the queue until the held-out side is done (a gate decision reads held-out only); they are
  still in the set for the label-budget rates and can be queued later with `split: train`.

## Should know

- No migration. A plain code PR; the route keeps its policy (`ai.admin` for writes).
- Round 2 is a new set version (`CL-1 1.1.0`, `t2@2 1.0.0`): a version takes panel rows once. Measure from the
  concatenated finals of the versions (claim hashes are disjoint once round 2 leaves out the claims round 1 labelled).

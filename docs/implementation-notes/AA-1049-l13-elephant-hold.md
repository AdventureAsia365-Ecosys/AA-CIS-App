# AA-1049 — elephant-riding policy hold (lane l13, S277)

Tech lead decision S277 (11/11/2026): a Master whose product offers elephant riding must not be on sale until a
person reviews it; it goes to the Review Queue and only an approval puts it back on sale. Before this change S1 never
auto-published such a version (S271, root ADR 0020 D8), but the Masters written by the old writer before the S271
detector (and a few v2 Masters) were on sale with no review row, and `_enqueue_review` (AA-653) entered every review
row of a tour with a live Master as `dismissed`, so even a rewrite never reached a reviewer.

Two PRs (MIG-4): CIS 398 (contract: `published_tours_master_status_reason_check` gains `policy_hold`), then the code
PR that requires 398 in readiness.

## Decisions

- **The hold is a lifecycle action** (`services/tour_lifecycle.policy_hold`), not a new status: `master_status`
  inactive + `master_status_reason` `policy_hold` (CIS 398). The cascade is the pause one (served state `paused`,
  reason `master_inactive`): queued T2 / T8 / T9 jobs cancelled, a `paused` lifecycle event (TripPlanner purge through
  `run_after_commit`), the catalog views and Booking read the served state, no tenant mail (ADR 0019 d20: a paused
  tour gets no notice; the retire fan-out only acts on `retired` / `restored`). Staff get the `master.deactivated`
  notification with `reason: policy_hold`.
- **The review row is an ordinary `review_queue` row** (no new column): `escalate_detail` items with `check_id`
  `policy:elephant_riding` (the reason id the T2 policy hold already uses) and a `POLICY_REVIEW elephant_riding`
  summary prefix (`services/quality/policy_review.py`). SQL finds it with `escalate_detail @> '[{"check_id": ...}]'`
  plus `generated_content_id IS NOT NULL` (an S1 row; a T2 row carries `tenant_tour_version_id`). Evidence is codes
  only: `where` (`master` / `version` / `source`) + `field`; `source_span` is NULL, never the matched words (the
  draft of lane l12 put the matched words in `source_span`; dropped: page text in an admin JSON column is avoidable).
- **"Human approval" = a `review_queue` row of that version with `review_status = 'approved'`**. The Review Queue
  approve claims its row `approved` before `process_export`, so the lifecycle `publish` sees it. No new column, no
  flag the caller passes (a caller cannot claim an approval it does not have).
- **The guard lives in the lifecycle `publish`** (the one writer of a Master): a version with a detector hit (its
  stored text or the tour's raw source) and no approval → 409 `policy_review_required`. `process_export` turns that
  into: version `approved` → `hitl`, a pending policy review row, `PolicyReviewRequiredError` (S1 auto-publish logs a
  warning, promote answers 409, the Review Queue approve never hits it since its row is approved).
- **The approve of the held Master's own version** (`policy.held_master_version`) skips the new-version approve gate
  (score floor, grounding record, brand audit): the old-writer Masters have no grounding record and would be
  `approve_blocked`, yet the approve only puts back what was on sale before the hold. Any other version keeps the
  gate (its `policy_keep` rule already treats the policy as the human review).
- **Set Active** refuses a held Master, or a paused one that hits with no approval of its version, and the admin
  router then files the hold + review so the approve is one click away (the 409 says "Its review is in the Review
  Queue."). Pause keeps `policy_hold` as the reason. `restore_source` / `set_active` hold a Master they would switch
  back on when it hits with no approval.
- **`_enqueue_review`**: a version held only by the policy (`elephant_policy.is_policy_only_hold`, the approve gate's
  rule) enters `pending` even when the tour has a live or held Master, marked as a policy review; every other hold of
  a live tour keeps AA-653 (`dismissed`). The AA-653 supersede runs only when the new row entered `pending`, so a
  dismissed attempt never closes a pending policy review. The publish dismissal (process_export step 3c) spares a
  pending policy review of a NEWER version (a re-export of the old Master does not close it).
- **The one-time script** reads active Masters plus the ones already held for the policy, so the pin is the same
  before, during and after APPLY (resumable). The pin is `count:md5` of the sorted `tour_id:served version id` of the
  tours to hold (id + change marker, destructive-ops step 1). It prints 8-char ids and field codes, never text.
- **A person's approval is final for the hold** (review fix pass): a Master whose served version has an approved
  policy review (`policy.policy_approved`: an `approved` row with the policy marker) is `approved` in the script
  (never held, out of the pin) and `policy_hold` refuses it (409 `policy_approved`). So a DRY RUN / APPLY re-run after
  reviewers approved some tours never re-holds them, and the pin moves (an APPLY with an old pin stops). An approval
  with no policy marker (a score review, or an S271-S276 `policy_keep` approve, before the marker existed) does not
  count: the tour is still held and the DRY RUN prints it as `approved_other` so the tech lead sees how many before
  APPLY.
- **The policy supersede touches S1 rows of the master tenant only** (review fix pass): `ensure_policy_review`
  replaces older pending rows with `generated_content_id IS NOT NULL AND tenant_id = <master>`; an agency's T2 row of
  the same tour (its tenant, `tenant_tour_version_id`, e.g. the T2 elephant-riding policy hold) stays pending. The
  pending-row lookup is scoped the same way.

## Changed

- `services/tour_lifecycle/policy.py` (new): the detector reads, `human_approved`, `ensure_policy_review`,
  `held_master_version`, `PolicyReviewRequiredError`.
- `services/tour_lifecycle/service.py`: `policy_hold`; the guard in `publish` (and the lift of a hold);
  `activate_master` refusal; `pause_master` keeps the reason; `restore_source` / `set_active` hold instead of enabling.
- `services/quality/policy_review.py` (new, from the stopped lane l12 draft, reworked: codes-only evidence, filter
  value, `evidence()`).
- `services/export/handler.py`: `_refuse_policy_review`; post-commit recompute when a publish lifted a hold; step 3c
  spares a newer pending policy review.
- `api/routers/admin_pipeline.py`: `_enqueue_review` policy path; `admin_review_queue` filter `policy`, row field
  `policy_review` {kind, held, approvable, evidence}, facet `policy_reviews`; S1 run and promote catch the refusal.
- `api/routers/v1_pipeline.py`: the held-version approve; response field `policy_hold_review`.
- `api/routers/admin.py`: Set Active refusal files the hold.
- `api/core/readiness.py`: 398 required above the gap.
- Frontend Review Queue: "Elephant riding policy" filter (with count) and badges ("Elephant riding policy", "Off sale
  until approved"); Approve follows `policy_review.approvable` for an unedited policy row.
- `scripts/quality/hold_elephant_masters.py` (new).
- `tests/integration/conftest.py`: the Dev-shape `review_queue.escalate_detail` column (CIS 107 fails in the CI
  replay because of its tenant_tour_versions FK).

## Tradeoffs

- A source hit counts even when the rewrite dropped the ride: the product still offers it, so a person reviews it
  (one review per new version of such a tour). Fail safe, like the detector's negation rule.
- `activate_master` reads the detector on every Set Active of a paused Master (one extra row read); negligible.
- A rejected policy review leaves the Master held with no pending row; Set Active then files a new one (the 409 path),
  so there is always a way back through a person.
- The legacy export Lambda package (never invoked) now needs `services/quality` through the lifecycle import: the
  guard fails closed there rather than publishing unreviewed.

## Should know

- Live order: CIS 398 applied (Migration Apply, class contract) → code PR deploy (readiness requires 398) → script
  DRY RUN (count + pin to the tech lead) → `APPLY=1 PIN=<pin>` → read-only check: a second DRY RUN prints
  `to_hold=0` (active Masters with a detector hit and no approved policy review of the served version = 0), and
  pending S1 policy reviews = already_held. Tenants' pending T2 review rows are unchanged by the APPLY (count them
  before and after).
- The publish dismissal of process_export (step 3c, on main before this change) dismisses every pending row of the
  tour, tenants' T2 rows included; this lane only spares a newer S1 policy review there. Left as is (not part of
  this slice); see the lane follow-up.
- The script runs in the api container (`tools/ecs/ecsx.sh`, `cis_app`); each hold enqueues a recompute / TripPlanner
  purge job after commit, so run it when the job table is quiet.
- Detector changes (`elephant_policy.py`) change which Masters the guard refuses; the integration tests pin the
  current behaviour on invented text.

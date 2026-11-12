# AA-1048 W2 — pooled claim classes in the R1 record (lane l3-w2-pooled, S278)

Follows `AA-1048-ground-truth.md` (S272, R1 harness), `AA-1048-import.md` (S273) and `AA-1048-w2-phase1.md` (S273).
The tech lead decided at the start of S278: measure a POOLED claim class first, split it later. Root ADR 0020 gets
an amendment ("Amendment (S278): pooled claim classes", root PR of this lane).

## Decisions

- **Why a pool.** The S275 count on the adjudicated held-out labels (CL-1 1.0.0 + 1.1.0, Dev) found at most 9
  violations for any (writer, class) against the bar of 20; the unsupported-trip-claim classes of t9_blog together
  had 21 (embellish 5, inclusion 6, misbinding 9, number 1). An entailment / extraction instrument does not know the
  class at run time and its precision is already taken over every fire on a writer, so the class split is only
  needed for recall per class.
- **A pool is declared, never derived.** `services/ground_truth/pools.py` `POOLS` (id `pool.<name>`, members,
  writers, who decided). A declaration changes only in a reviewed PR; `pools_sha` is written into every record so a
  record made under another declaration is never read as this one. `pools.problems()` checks a declaration against
  the policy seam (members are classes of every pool writer, at least two members, the id is not a class id, a class
  in at most one pool per writer); a unit test keeps the shipped declaration clean.
- **First pool**: `pool.fact_unsupported` = fact.embellish + fact.inclusion + fact.misbinding + fact.number on t2,
  t9_blog and t9_social (the tech lead's example "t9_blog fact.*" minus the fact classes that are another reading:
  fact.name is caught by deterministic checks, fact.direction and fact.self_contradiction ask a different question).
  T2 is in the pool because the t2@2 labels are measured in the same run; the bar is per writer anyway.
- **The bar is the class bar applied to the pool, per writer** (`r1._pool_records`): the writer's precision and
  lower bound over every fire (unchanged), the pool's n_pos on that writer (member violations summed) >= 20, at least
  one caught, held-out split and instrument conditions. n_pos never sums across writers.
- **A pool calibrates only its members.** `r1.measure` adds `pools`, `passing_pools` (`<writer>:<pool>`), `pools_sha`
  and `calibrated` (= `pools.calibrated_keys`: passing classes plus the members of passing pools on that writer).
  `claim_shadow.CALIBRATED` holds those keys; `mode_for` is unchanged (per writer and class, override AND calibrated),
  so a fire classed outside the members keeps warning. A test drives `mode_for` from a pool record.
- **Split later**: every pool entry keeps `members_detail` (per member n_pos and tp per writer) and `split_ready`
  (members that reach the bar alone; they are then measured as their own class).
- **Recommendation** (`r1.report`) ranks instruments by the number of calibrated keys (classes + pool members), then
  the id; it was the number of passing classes.
- **No CLI change**: `scripts/aa1048/r1_calibrate.py` prints `r1.report`, which now carries the pool fields; the
  DRY RUN plan adds `pool_positives`.

## Changed

- `services/ground_truth/pools.py` (new), `services/ground_truth/r1.py` (pool records, `measure(pools=)`,
  `report(pools=)`, plan `pool_positives`), `services/quality/claim_shadow.py` (comment of `CALIBRATED` only).
- Tests: `tests/unit/test_aa1048_pools.py` (declaration, bar per writer, no lowering, train / failing instrument,
  split_ready, report and plan, calibrated keys, claim_shadow enforce map).
- `CONTEXT.md`: glossary entry "Pooled claim class".

## Outside the repo (workspace scratch, real text stays there)

- Count / live script for the orchestrator: `.tmp-session/S278/aa1048/r1_run.py` (built by `build_r1.py` from
  `r1_run.tmpl.py`; `local_smoke.py`, `read_r1.py`). It reads CL-1 1.0.0 + 1.1.0 and t2@2 1.0.0, applies the tech
  lead's corrections (`review.effective_rows`; the S275 script read the label rows only), prints n_pos per writer x
  class and per pool, the subset (writers whose pool or class reaches 20), and the plan of the instruments that fit
  the cap together. MODE=count works on the current image (an embedded copy of `pools.py`, sha checked); MODE=live
  needs this PR deployed.
- Golden bump DRY RUN (`.tmp-session/S278/aa1048/golden-bump-dry.md`): revoke t9b-002 only, 0 split moves.
- ADR draft: `.tmp-session/S278/aa1048/adr0020-amendment-draft.md` (applied in the root PR).

## Tradeoffs

- Per-member recall inside a pool rests on fewer positives (5-9 per member today) than a per-class record would;
  the record shows it and the per-class enforce override stays the tech lead's choice per member.
- Declaring the pool in code (not in `writing_policy.yaml`) keeps the W1 policy registry untouched; when pools become
  a policy concept, `pools.POOLS` is the one place to move.
- Entailment mode names no class at run time: its pool record is report-only (ADR amendment rule 3); the pinned
  shadow is extraction mode, which names the class.

## Should know

- No migration, no route, no job kind: a plain code PR. The live R1 run is not made here (2 USD cap, the tech
  lead's yes, the orchestrator runs `r1_run.py MODE=live` after this PR is deployed).
- Local smoke of the script on the panel files (NOT the Dev numbers: Dev finals carry the tech lead's decisions):
  the subset was t2 only and the plan picked llama4-maverick extraction at about 0.44 USD expected. The Dev count
  decides.

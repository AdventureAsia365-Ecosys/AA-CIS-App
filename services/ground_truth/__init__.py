"""AA-1048 (Quality W2, root ADR 0020) — claim-level ground truth and calibration tooling.

- policy.py      the ONE seam to the policy registry (claim classes, severities, policy sha)
- claims.py      claim identity (content sha, claim hash, frozen split), sentence spans
- panel.py       the 3-lens agent panel export / import format, consolidation, audit draw, contradictions,
                 agreement report, final labels and verdict_rule
- store.py       shared.eval_claim_label (CIS 388) writes and reads, the 'golden_claim' queue items
- sample_size.py Wilson lower bounds and the sample-size table per detector / class
- detectors.py   the deterministic detectors measured on labels (thresholds rows)
- mutations.py   synthetic mutation suites per writer (recall and false findings)
- r1.py          the R1 calibration harness for pinned claim instruments (DRY RUN by default)

Nothing here runs on a live path. Real tour / tenant text only ever lives in the private Dev DB and in gitignored
scratch files; tests use invented names only.
"""

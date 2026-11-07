# AA-1048 — claim-level ground truth and calibration tooling (Quality W2, free part) — lane l9, S272

Source plan: root ADR 0020, the S271 writing-quality audit (`docs/audits/2026-11-06-S271-writing-quality-audit/`:
README W2, `decisions.md` D1-D16, `data/design-review.json` `arch.roadmap[2]`, critic fixes F2, F3, F7, F8, F10,
F14). This lane builds the $0 tooling; the agent panel, the tech lead's adjudication and the paid R1 run happen after
it ships.

## Decisions

- **One table, one row per judgement** (`shared.eval_claim_label`, CIS 388): one row per panel lens and one per
  tech-lead adjudication, not one consolidated row per claim. Agreement, the audit error rate and a later relabel all
  read the same rows; the DB key `(set_id, set_version, claim_hash, labeller_kind, lens)` (NULLS NOT DISTINCT) makes a
  second judgement by the same lens impossible. No UPDATE grant: a set version is append-only (panel rows once, tech
  lead rows added as adjudication goes); a relabel is a new version.
- **Claim identity** = `sha256("<content_sha256>:<field>:<normalised span>")`, the content sha being the golden-set
  importer's canonical-JSON sha of the output. Same text = same claim on any object (two golden items with identical
  outputs share their claims; `cli export-fixture` exports them once and `cli rederive` maps labels back to every item
  by content sha).
- **Split by object**, not by claim: `held_out` when `sha256("aa1048-cl:<content sha>")` falls in the first 30% of the
  hash space. Frozen, and every claim of one object is on the same side (no train/test leak inside a blog).
- **Classes come from the taxonomy on main** through one seam, `services/ground_truth/policy.py::_load()`. The DB
  CHECK on `claim_class` is a shape (`^[a-z]+\.[a-z0-9_]{1,40}$`), not a closed list, so the W1 `writing_policy.yaml`
  (#1368) can replace the taxonomy without a migration; the import validates the class against the seam.
- **Panel lenses** `source` (fidelity), `promise` (policy D1-D15), `skeptic` (both directions). Agreed = 3 lenses,
  one class and one label, none unsure; anything else is disputed. The audit draw is a seeded-hash 20% of the agreed
  claims (critic F14), stored as panel state `audit`.
- **Release evidence** (critic F14): `panel.release_evidence` keeps tech-lead labels only, plus agreed panel labels
  when the caller says the audit draw was adjudicated and accepted (`cli finals --audit-ok`).
- **The disagreement queue is the tech lead's work list**: source `golden_claim`, `source_ref` = the claim's first
  panel row, `machine_verdict` = the panel's common label (`split` when no majority), `human_label` = `disputed` |
  `audit`. `cli enqueue` adds them (no weekly cap: the weekly KPI job never touches this source); one click in the
  Writing memory page resolves; `cli pull-resolutions` writes the tech-lead row (`judge_right` keeps the panel label,
  `human_right` flips violation/ok, `unclear` or a split gives `unsure`).
- **Sample size** is conservative: true positives = floor(observed precision x n). At P 0.92 the LLM bar (LB 0.80)
  needs 47 labelled fires (the audit's "about 47"); at exactly 0.90 it needs 70. Deterministic bar (LB 0.75, n_pos
  15) from the README "Gate honesty" line.
- **Detectors are measured by calling the production functions**, never a copy: meal/clock regex (its regex part:
  the Jev clearance after it is an LLM step), elephant, day codes (DAY_OUTPUT_EXTRA at claim level), novel numbers,
  optional-as-fixed, meta band (D5 140-155), T3 lexicon (`service_level_claims`), S1 names, F1, F12, F17, F18, and
  the three tripwires (critic F7). A tripwire that does not pass the bar gets mode `route_only` (it may send a
  sentence to the claim engine, never hold). Recorded producers (critic F10, an A3 producer's stored verdict per
  claim) are measured the same way from a `recorded` map on the export line.
- **Thresholds rows go to a file**, in `thresholds.yaml` shape (`det.<detector>.<class>`), never into
  `services/quality/thresholds.yaml` from code: filling the registry is a reviewed PR once real labels exist.
- **R1 instruments**: id `<model>@<connection>:<mode>`; defaults `gpt-6-luna-openai@openai-direct` and
  `llama4-maverick@acc2` (the acc2 non-Claude candidate: Llama 4 Maverick, enabled on acc2, 0.24 / 0.97 per M
  tokens; Nova Lite left the judge routes in W0, Kimi K3 is 14x the price). Entailment reads the claim plus its
  code-aligned evidence lines (critic F8 option one); extraction reads the field and the full source, and code
  confirms each quote is in the text before it can flag a claim. 3 votes at temperature 0 and a fixed seed;
  `max_tokens` 8192 (migration 382); a reply from another model or connection is no vote (D7: miss = unverified).
- **Cost guard**: DRY RUN by default (also installs the golden-set LLM guard so nothing can call a model); a live
  run needs `--live --confirm-spend` and a cap <= 2 USD; a call starts only when spent + 2 x its expected cost fits.
- **Golden-set bump** is a new `revoke` command of `scripts/aa729/labels.py` (the fixture tooling's home): a human
  label cannot be relabelled (`RELABEL_SOURCES`), so a stub leaves the set. Stub = reason with the `ZZ S<nnn>` test
  marker or "test label" (finds t9b-002 on 1.3.0). The report lists every label that flips: the revoked ones, the
  pass/fail counts per stratum before and after, and every remaining item whose tune/holdout split moves.

## Changed

- `api/migrations/388_eval_claim_label.sql` (migration-only PR, contract): the table, RLS (ENABLE + FORCE, X1
  parity policies, tenant_isolation), grants (cis_app SELECT, INSERT, DELETE after REVOKE ALL), and the
  `golden_claim` value in `writing_memory_disagreement_source_check`. Erasure inventory entry, conftest FORCE-RLS
  lists, `tests/integration/test_aa1048_migration_388.py`.
- `services/ground_truth/` (new): `policy.py`, `claims.py`, `panel.py`, `store.py`, `sample_size.py`,
  `detectors.py`, `mutations.py`, `r1.py`.
- `scripts/aa1048/cli.py` (export-fixture, import, report, manifest, enqueue, pull-resolutions, finals, rederive,
  detectors, mutations, sample-size) and `scripts/aa1048/r1_calibrate.py`.
- `scripts/aa729/labels.py`: `revoke` + `is_stub_label`.
- `services/writing_memory/disagreements.py` docstring and `frontend/lib/writingMemory.ts` (`Disagreement.source`
  union): the new source.
- Docs: `docs/calibration/aa1048-sample-size.md` (generated table, a unit test keeps it equal),
  `docs/calibration/aa1048-claim-panel-brief.md` (panel brief, file formats, orchestrator steps).
- Root PR: DM-7.1 retention row for `shared.eval_claim_label` and the RoPA row 9 mention.

## Measured today (synthetic mutation suites, 50 clean texts x 8 classes x 4 writers, $0)

| Class | a1 | t2 | t9_blog | t9_social | Caught by |
|---|---|---|---|---|---|
| number, structural (day), meal, clock, name | 1.00 | 1.00 | 1.00 | 1.00 | novel numbers / F1, day codes, meal/clock regex, S1 names |
| optional as fixed | 0.00 | 0.00 | 1.00 | 1.00 | optional_as_fixed / F17 (T9 seed format only) |
| service words | 0.58 | 0.46 | 0.54 | 0.50 | T3 lexicon / F17 ("luxury lodge" yes, "boutique hotel" no) |
| inclusion of a service (guide, transfers, fees) | 0.00 | 0.00 | 0.00 | 0.00 | none deterministic |

False findings on clean sentences: 0.0 for every writer (tripwires excluded: PROMISE_FRAME_RE fires on 250 of 800
clean sentences, TRIP_RE on 154, so they must stay route-only). The proof bar (>= 0.95 structural / number /
inclusion / meal) fails on **inclusion**: no deterministic detector catches an invented service; it stays with the
claim instrument. S1 / T2 have no deterministic optional-as-fixed check (the T9 one parses the T9 seed only).

## Tradeoffs

- Per-judgement rows cost 3-4 rows per claim; a few hundred claims per set, negligible, and the agreement maths
  needs them.
- Synthetic suites measure recall on clean invented text, not on real writer prose; they are a floor and a
  regression guard (`test_real_detectors_hold_their_measured_floor_on_the_suite`), not a calibration. Calibration is
  the label set.
- The extraction overlap rule (substring either way, else >= 60% shared words) can map a long quote onto two short
  claims; the measurement treats both as flagged, which lowers precision rather than hiding misses.
- R1 prices are written in `r1.PRICES` (as `scripts/aa1000_adjudicate_calibrate.py` does) so the DRY RUN needs no
  DB; a model without a price is refused.

## Should know

- **Chain**: CIS 388 (migration-only PR) -> Infra bot copy (needs the root retention PR merged first) -> Migration
  Apply (class contract) -> this code PR. No readiness raise: nothing on a live path reads the table.
- **Seam to W1**: when #1368 (writing_policy.yaml v1) merges, change `services/ground_truth/policy.py::_load()` only
  (classes, severities and the sha from writing_policy); set `POLICY_SOURCE = "writing_policy"`.
- **Real text never in the repo**: export, panel, finals, rows and R1 report files are scratch-only; the root repo
  gets `cli manifest` output (ids, counts, digest).
- **The live R1 run is not made here.** Estimated (synthetic sizes: 6,000-character blog, 8,000-character source):
  60 labelled claims on 18 objects = 468 calls, about 0.24 USD expected for both candidates and both modes (worst
  case at 8,192 output tokens: 2.95 USD, never reached in practice: the guard counts the real cost); 700 claims =
  about 1.03 USD, which the guard refuses (expected x 2 > 2 USD cap): run entailment on a subset or one mode at a
  time. `r1_calibrate` prints the plan from the real files.
- The golden bump needs the fixture from the private store (`fixture-1.3.0.json`); `revoke` then the importer DRY RUN
  / `--apply` (supersedes 1.3.0; release 0 follows automatically, other releases need a reverify, AA-900 notes).

# AA-729 golden set: version changelog (ids and counts only)

The fixture itself holds tenant pieces and Master tour text, so it never enters this public repo: it lives in the
private scratch store and the CIS bronze bucket (`scripts/out/aa729/fixture-<version>.json`) and is imported with
`services/golden_set/importer.py`. This page records what each version changed, by sample id and count. Every version is
also recorded in the fixture's own `label_history`.

| Version | Change | Samples | Approved by |
|---|---|---|---|
| 1.0.0 | Selection (AA-729): 50 S1, 10 T2, 15 T9 blogs, 15 T9 LinkedIn posts, 10 synthetic injection traps | 100 | tech lead (S245 memo) |
| 1.1.0 | Agent panel labels on the 82 samples no existing decision labelled | 100 | tech lead (S245) |
| 1.2.0 | Calibration review: 2 panel labels flipped (`t9l-001`, `t9l-003`) | 100 | tech lead (S256 plan) |
| 1.3.0 | Addendum: `t2-011..020`, `t9b-016..020`, `t9f-001`, `t9l-016..033`, panel labelled | 134 | tech lead (S259 plan) |
| 1.4.0 | Brand labels (rubric `aa1079-brand-v1`) on 74 T2 / T9 items, no sample added | 134 | 3-lens agent panel (S278) |
| 1.5.0 | Addendum (AA-1051 W5): 14 t9_blog pieces `t9b-021..034`, panel + verifier labelled | 148 | tech lead (S283 close `c-w5-gate=label6`; S284 decisions on the 14-item scope and the 2 disputed labels) |
| 1.6.0 | Addendum (AA-1051 W5, S1): 22 a1 Master pages `s1-051..072`, panel + verifier labelled; built locally as the base of 1.7.0, never imported | 170 | S285 agent panel; the one dispute (`s1-068`, CAL) by the orchestrator rule, pending tech lead review |
| 1.7.0 | Addendum (AA-1051 W5, T2): 48 tenant pages `t2-021..068` on 1.6.0, panel + verifier labelled | 218 | S285 agent panel; 10 disputes by the orchestrator rule, pending tech lead review |

## 1.7.0 (S285)

- **Why**: the W5 S1 and T2 writer proofs need the minimum GATE sample in force (settings
  `cis.quality.writer_proof_s1_min_items` 25, `cis.quality.writer_proof_t2_min_items` 20); 1.5.0 had 15 usable a1
  (16 less `s1-027`, over the judge's evidence cap) and 5 t2 GATE items.
- **Built in two merges, imported once**: 1.6.0 = 1.5.0 plus 22 S1 samples, 1.7.0 = 1.6.0 plus 48 T2 samples (every
  merge bumps the minor version). Only 1.7.0 was imported on Dev; it supersedes 1.5.0. No existing item, label or side
  changed (the t9_blog items are byte-identical to 1.5.0, which lets the W5 null run of 1.5.0 carry, CIS
  `services/writer_proof/null_carry.py`).
- **GATE positions were declared before any label** (the side is a hash of the renumbered id): S1 `s1-052`, `053`,
  `057`, `058`, `060`, `061`, `065`, `067`, `069`, `072` (stratified: Japan 3, Rajasthan 2, a source length
  contradiction 4, Optional-heavy 4, overlapping); T2 `t2-026`, `028`, `033`, `034`, `037`, `039`, `041`, `044`, `045`,
  `052`, `062`, `063`, `064`, `067`, `068` (all on the 4 audit tenants: thin source 4, 9+ days 10, a QA-failed version
  4, an elephant mention 4; the 4 elephant items are the 4 tenants' versions of one Master tour). The other new ids are
  CAL / DEV fill in picker order.
- **Labels**: the 1.5.0 protocol (three lenses, then a full-source verifier). Disputes follow the orchestrator's rule
  (the lens majority only when the verifier's sole fail ground is one it ruled a pipeline gap elsewhere in the set,
  else the verifier), pending the tech lead's review: S1 1 (`s1-068`, CAL, majority pass); T2 10, all verifier fail,
  3 of them on GATE (`t2-033`, `t2-063`, `t2-068`).
- **Non-trap GATE items per writer after the merge**: a1 26, t2 20, t9_blog 12, t9_social 11. GATE labels: a1 21 fail /
  5 pass (`s1-027` included), t2 11 fail / 9 pass.

## 1.5.0 (S284)

- **Why**: the W5 eval null run draws its fixed sample from the non-trap t9_blog items on the GATE side
  (`services/writer_proof/splits.py`); 1.4.0 had 6 and the null run needs 12 (setting
  `cis.quality.writer_proof_null_run_min_items`).
- **Where the items come from**: the 14 t9_blog pieces already frozen in the AA-1048 R2 addendum (selected by the
  claim split plan, `addendum_of` 1.3.0). No new database read. The merge uses `labels.py merge --rebase` (the base
  1.4.0 only added labels since 1.3.0; the output-identity check still refuses a piece the base holds). The addendum's
  T2 and T9 LinkedIn samples had no label and were left out (`--drop-unlabelled`).
- **Sides** (a hash of the new id, fixed for every later version): GATE `t9b-024`, `t9b-025`, `t9b-028`, `t9b-029`,
  `t9b-033`, `t9b-034`; CAL `t9b-021`, `t9b-023`, `t9b-026`, `t9b-030`, `t9b-032`; DEV `t9b-022`, `t9b-027`,
  `t9b-031`. Non-trap GATE items per writer after the merge: a1 16, t2 5, t9_blog 12, t9_social 11.
- **Labels** (the AA-729 round-1 protocol, `docs/implementation-notes/AA-729.md`): three independent judges with
  distinct lenses (source fidelity, voice and policy, the customer reader), then one verifier per sample that reads
  the whole source, checks each issue the judges named and looks for missed ones; the AA-729 rubric of the writing
  policy. A label is `agreed` when the verifier matches the judges' majority, `disputed` when it overturns it; a
  disputed sample is imported only with the tech lead's decision (`labels.py apply --include-disputed`), and `apply`
  refuses any other status. Result: 12 agreed (10 unanimous, 2 split 2 to 1 and confirmed: `t9b-022` fail, `t9b-026`
  fail); 2 disputed, both panel 2 to 1 pass and verifier fail/heavy under the unsupported-claim and suitability rules:
  `t9b-023` (a preparation claim the source does not make) and `t9b-030` (a stop order and a longer trip the source
  does not state, against a one-moment scope). With the verifier side on both: 9 pass, 5 fail. GATE: 5 pass, 1 fail
  (`t9b-029`), all unanimous and verified. Each vote and reason is in the private panel file; the decided labels and
  who approved them are in the fixture's `label_history` and label evidence.
- **Scope**: the S283 decision asked for 6 GATE items; all 14 addendum blogs were labelled because the sides are a hash
  of the renumbered id (dropping the 8 CAL / DEV items renumbers the kept 6 onto `t9b-021..026`, 2 of them on GATE).
  The tech lead approves that scope with the disputed labels.
- **After import**: the importer supersedes 1.4.0 and flags the active releases `reverify` (golden set changed); the
  W5 null run on 1.5.0 is `w5-null-t9-blog-v7-g150` and needs a reverify of t9_blog on 1.5.0 first.

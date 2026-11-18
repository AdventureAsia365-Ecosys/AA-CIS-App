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

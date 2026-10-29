# AA-789 tier 1 (S258): seed alignment

Plan: root `docs/audits/2026-10-29-S257-aa789-design/PLAN.md` (approved by the tech lead in S257), slices S1
(foundation), S2 (eval harness), S3 (classes 1-5) and S4 (classes 6, 7, 8, 10). Tier 0 (slice S5) is a separate
branch (`AA-789-s258-tier0.md`). Classes 9 (paraphrased repeat, needs the local sentence encoder) and 11 (FAQ
non-answer) are slices S6 and S7. Tier 2 (LLM) is out of scope: every LLM route is blocked today.

## Decisions
- **Package** `services/acp_content_writing/seed_align/`: `nlp.py` (the only spaCy loader, lazy), `seed_graph.py`
  (`SeedGraph` from the seed text), `checks.py` (one check per class, returns candidates), `modes.py` (per-class mode,
  routing under existing gates, the hold rule). `checks.py`, `seed_graph.py` and `nlp.py` import nothing from
  `quality_gates` or the registry (I1, a test parses their imports).
- **Three keys or abstain.** Every check needs a dependency structure, a seed relation and a closed trigger word.
  Extra abstain rules: a sentence over 40 tokens (fact classes), a noun the small model read as a verb on the path
  from the trigger to its clause ('boat rides' → 'rides' VBZ: the parse is not stable), a seed that also puts an event
  AT the 'near' place, a moment the seed only offers, a figure the seed gives.
- **SeedGraph reads text, not a schema.** It parses the T9 seed blocks (label line, `Source itinerary:`, `Status:`,
  `Naming:`, `Place background` bullets) and flat `<Place> (day N): …` lines alike. Place background bullets and
  `Why it matters` never count as planned activity (they say what a place is, not what the trip does); a label whose
  source line is an offer ('take boat rides' over 'Optional activities include …') plans nothing.
- **Modes.** `T10_SA_MODE` (all classes) and `T10_SA_MODE_<CLASS>` (one class) on the worker task definition, values
  off / shadow / repair / hold, **default off** (plan §6: off on merge, shadow by worker env). Unknown values read as
  off. `hold` reads as `repair` unless the class is in `HOLD_CAPABLE` (optional told as planned, planned told as
  optional, near told as at, world share) **and** in `HOLD_PROMOTED`, which is empty: promotion is a PR with the
  G1-G3 evidence, after the S246 freeze (~18/11). No hold path exists before that.
- **Routing.** Shadow candidates go to `outcome["seed_align"]` → `writer_input.seed_align` (per attempt, admin-only),
  never the ledger. Repair / hold candidates are appended before `merge_ledger` to the existing legacy result of their
  gate (F17_trip_fact: classes 1-4; F18_rarity: 5, 6; F13_leak: 7, 8; F13_repeat: 10) as
  `sa:<class>: <reason>: '<sentence>'`, the quote format `sentence_repair` locates. A sentence a deterministic finding
  of the same gate already quotes gets no second finding.
- **Hold rule.** `quality_gates.never_holds`: a result whose violations are all `sa:` findings never holds unless one
  of them may (`modes.finding_may_hold`). A mixed result holds on its deterministic findings, as before.
- **Brief lines.** The seed's writer-instruction lines plus `hygiene.brief_texts` (brand pack, angle brief) plus the
  audience line feed class 7 (audience range, quoted brief phrases, voice words).
- **Dependency.** `spacy==3.8.16` (released 24/08/2026; 3.8.17/18 are three days old) and the `en_core_web_sm` 3.8.0
  wheel by URL with its sha256, in `requirements.txt` so the image and CI get the same pins. numpy stays at the version
  pandas resolves (checked with numpy 1.26.4).

## Changed
- New: `services/acp_content_writing/seed_align/{__init__,nlp,seed_graph,checks,modes}.py`,
  `scripts/aa789_seed_align_eval.py` (predict for the eval set's `score.py`, `--baseline` for main's checks, census on
  a decoded AA-783 export: counts only), `tests/unit/test_aa789_seed_align.py`.
- `quality_gates.run_quality_gates`: runs `seed_align.run` and `seed_align.apply` before `merge_ledger`; the outcome gets
  `seed_align` only when some class is not off. `QualityCheckOutcome` gets the optional key. `never_holds` gets the
  `sa:` clause.
- `service.py`: each attempt's `outcome["seed_align"]` is kept in `context["seed_align"]` and stored in `writer_input`.
- `requirements.txt`: spaCy and the model. `CONTEXT.md`: one paragraph in the T10 section.

## Tradeoffs
- **Image size.** About +220 MB uncompressed (spacy 129 MB, blis 39 MB, thinc 17 MB, the 15 MB model, srsly and small
  deps). The model loads only when a class is not off, in the process that runs the gates (the worker).
- **Memory and time** (laptop, 40 stored pieces): +131 MB RSS after load, load 0.5 s, p50 15 ms and p95 0.1 s per
  piece. Decision D1 (worker 1 → 2 GiB) waits for the live RSS on the worker once shadow is on; spaCy alone may fit.
- **Recall is the price of precision.** Every check abstains on long sentences, unstable parses, paraphrased forms
  and implicit subjects. Those are tier 2's (LLM, later).
- **The eval set is the tuning set.** The checks were built against the 405-item set, so its numbers are optimistic.
  Two guards against overfitting: a census on the 40 stored S234 pieces (real copy the checks were not tuned on first;
  3 false fires found there were fixed by rule, not by example) and the synthetic negatives in the unit tests. The real
  measure is the shadow run (G2).

## Measured (P0, $0)
- Eval set (`score.py --strict-class`, contested skipped): 0 false positives on 246 clean items (Wilson 95% upper bound
  1.54%: passes the hold bar on the bound); 86 / 149 defects (0.577). On the nine built classes 86 / 122 (0.70).
  Per class: optional→planned 6/13, planned→optional 11/12, near→at 11/15, difficulty 12/14, world share 9/11,
  superlative 9/22, brief recipe 12/13, naming note 10/11, orphan opener 6/11. With the 10 contested items: 0 / 256.
- Main's deterministic checks on the same set: 46 / 149 with 11 clean items flagged (several are out-of-class
  findings the README allows). Tier 1 or main: 113 / 149.
- 40 stored S234 pieces: 24 candidates on 14 pieces (near→at 11, superlative 6, brief 2, naming 1, optional 1,
  difficulty 1, world share 1, orphan 1); one reviewer (the agent) read each against its seed: 23 true, 1 borderline
  ('Mörön … is a town that functions as a practical pause'). Second rater pending (plan D5).
- I2 on the 40 pieces: every class off gives the same outcome hash as `main`; shadow gives the same ledger as off on
  all 40; repair changes 13 ledgers and no hold decision.

## Should know
- Nothing changes on merge: every class is off until the worker env sets `T10_SA_MODE=shadow`.
- Repair mode costs money (one `t9_repair` call per affected piece) and `t9_repair` is blocked on acc3 today, so P2
  waits for the route. Shadow is $0.
- `HOLD_PROMOTED` is the one line a hold promotion changes; its test pins it empty.
- The P1 replay script for stored Dev pieces (counts only by default) is outside the repo, in the S258 lane folder;
  it needs the image with spaCy, so it runs after deploy.

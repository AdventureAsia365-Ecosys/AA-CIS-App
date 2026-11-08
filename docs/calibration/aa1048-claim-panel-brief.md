# Claim-label panel brief — CL-1, L2, t2@2 (AA-1048, Quality W2, S272)

The first pass of the claim labels is done by an **agent panel of three lenses**, not by code. This page is the brief
each panel agent reads, the file format it returns, and the steps the orchestrator runs around it. The tech lead
adjudicates what the panel disputes and a random 20% audit of what it agreed on (decision D11: about 2-3 hours over
1-2 weeks). Root ADR 0020 W2; audit critic fixes F2, F8, F10, F14.

**Privacy.** The export and the panel files hold real tour and agency text. Keep them in the gitignored scratch
folder (`.tmp-session/` of the workspace) and in the Dev DB (`shared.eval_claim_label`, CIS 388). Never paste a claim,
a source line or a reason into this repo, a PR, Linear or Jira: the repo is public and CI runs the golden-set text
guard. The root repo gets the manifest only (`cli manifest`: ids, counts, one digest).

## Label sets

| Set | Writers | Objects (root audit W2) |
|---|---|---|
| `CL-1` | `t9_blog`, `t9_social` | the S232 127 claims, a stratified sample of the S234 565 V2 flags, the 18 golden blogs |
| `L2` | `a1` (S1 v2) | 40-60 stored v2 Masters: > 10 days, meal-heavy, no day markers, elephant, Optional-heavy |
| `t2@2` | `t2` | >= 12 pairs (>= 4 pass, >= 4 fail), about 150 sentence labels |

Size each set from the sample-size table (`aa1048-sample-size.md`), not from a fixed count. A set version
(`x.y.z`) never changes once imported; a relabel is a new version.

Strata: `hard` (held or known-failure objects), `representative` (random under the current writer), `mutants`
(code-injected defects, `services/ground_truth/mutations.py`), `traps`. Split: `held_out` for about 30% of the
groups, frozen by a hash of the object's group key: its tour (`refs.tour_id`), else its item (`sample_id`,
`piece_id`, `version_id`), else its content sha. Every object of one tour (Master versions, tenant versions, the blog
and social piece) is on the same side, so shared sentences never leak from train into held-out. The import recomputes
the split and refuses a line whose `split` disagrees. Measurements that decide a gate (a detector's `passes` / mode
`enforce`, an R1 instrument's passing classes and recommendation) read the held-out side only; the train side is for
exploration and the label-budget rates.

## The three lenses

Each agent gets the whole export file and labels **every** line under its own lens. Agents do not see each other's
labels and never see the pipeline's own verdict (no gate ledger, no flags, no held state).

| Lens | Question | Leans |
|---|---|---|
| `source` | Does the source (the full `source_text`, and the aligned `evidence` lines) state what this claim says about the trip? | Fact fidelity: numbers, names, meals, services, days, times. |
| `promise` | Under the policy below, is this claim a promise about the trip, allowed background, or voice? | Policy: D1, D3, D4, D13, D14, D15. |
| `skeptic` | Argue against the obvious reading: could a customer read it as a promise the trip does not make, or is a flag here over-strict? | Both directions: hidden promises and over-strict flags. |

Read the whole `source_text`, never only the `evidence` lines (a cut source made 26 of 82 judge fails wrong in
S245). The `evidence` lines are the code's alignment of the claim to the source (critic F8): when they are wrong or
miss the supporting line, say so in the reason (`evidence: wrong` or `evidence: missed <line>`); that check is part of
the panel's job.

## Policy (decisions D1-D16, ADR 0020)

- **D1** Background about a place the source names (history, geography, culture) is allowed; it never states this
  trip's activities, services, times or views. Numbers and years in background: label `ok` with reason `warn: number
  in background`.
- **D2** The brief is an instruction, never a source. Support comes only from the source lines.
- **D3** Reflection that states no fact about the trip is voice: `ok`.
- **D4** Hypothetical framing ("imagine", "you might") is fine on its own; naming a concrete trip activity, service,
  time or view makes it a promise that needs the source.
- **D13** Rarity, uniqueness or age attached to the trip is a promise (needs the source); about a place it follows D1.
- **D14** Suitability, safety and pace only as the source or the audience fields state them.
- **D15** A staged scene with a concrete trip activity, time or view is a promise; a general feeling is voice.
- **D5** Meta descriptions 140-155 characters (a length finding is class `other.length_shape`).
- Elephant riding: the tour stays as written and goes to human review (business owner's policy); label the claim on
  its facts like any other.

## Classes

Use the policy class ids of the export line's `candidate_classes` (today the taxonomy `services/quality/taxonomy.yaml`;
the W1 `writing_policy.yaml` replaces it through `services/ground_truth/policy.py`). The common ones:

| Class | When |
|---|---|
| `fact.inclusion` | a service, meal, amenity, suitability or exclusivity the source does not offer |
| `fact.number` | a number the source does not give, or gives differently |
| `fact.misbinding` | a real source value attached to the wrong subject |
| `fact.name` | a wrong or invented name of a place, venue or route |
| `fact.direction` | an invented direction |
| `fact.embellish` | an invented superlative, rarity or editorial claim |
| `struct.day_structure` | a day the source does not have, days out of order |
| `scope.framing` | a place outside the piece's scope |
| `scope.persona_promise` | a promise about who the trip suits beyond the source |
| `other.f6` | an optional or conditional item written as a firm plan |
| `other.f7` | an invented timing or schedule |
| `other.length_shape` | a length or shape rule (meta band) |

Pick the one class the claim is about. When a claim is fine, still give the class it would belong to if it were
wrong (an `ok` under `fact.number` says "this number is supported"): precision and recall are measured per class.

## Labels

- `violation`: the claim breaks the policy for its class (unsupported fact, promise the source does not make).
- `ok`: supported, allowed background, or voice.
- `unsure`: you cannot tell from the source and the policy. Use it rather than guess; an `unsure` goes to the tech lead.

One reason per line, at most 600 characters, plain words; quote at most the few words you need.

## Files

Export (input, one JSON object per line, from `cli export-fixture` or an equivalent DB export):

```json
{"claim_hash": "<64 hex>", "set_id": "CL-1", "writer": "t9_blog", "scope": "tenant", "tenant_id": "<uuid>",
 "refs": {"sample_id": "t9b-001", "tour_id": "<id>"}, "content_sha256": "<64 hex>", "field": "content_text",
 "span": [120, 188],
 "claim_text": "...", "context": {"before": "...", "after": "..."}, "field_text": "...", "source_text": "...",
 "evidence": ["...", "..."], "candidate_classes": ["fact.inclusion", "..."], "stratum": "hard",
 "split_group": "tour_id:<id>", "split": "train"}
```

Panel output (one line per claim and lens; the claim hash is the key):

```json
{"claim_hash": "<64 hex>", "lens": "source", "class": "fact.inclusion", "label": "violation",
 "reason": "source lists dinner only; the claim adds breakfast", "labeller": "panel:<run id>"}
```

Tech-lead output (one line per adjudicated claim, the same file format; or the one-click resolution in the
disagreement queue):

```json
{"claim_hash": "<64 hex>", "labeller_kind": "tech_lead", "class": "fact.inclusion", "label": "ok",
 "reason": "breakfast is in the day 2 inclusions"}
```

A file is refused as a whole when a line names a claim hash not in the export, an unknown lens, class or label, or a
second judgement of the same claim by the same lens.

## Orchestrator steps

All commands run with the repo on `PYTHONPATH`; DB commands as `cis_app` (`DATABASE_URL`), DRY RUN unless `--apply`.
On Dev nobody holds a `cis_app` login outside the container and `scripts/` is not in the image, so the DB steps go
through the staff API (S273, `api/routers/admin_claim_labels.py`; the `cli` DB commands stay for a local test DB):
`A=https://api-cis.lumiguides.it.com/admin/writing-memory/claim-labels` and the headers `X-Admin-Secret` (secret
`aa-cis/dev/admin-secret`), `x-admin-role: admin`, `content-type: application/json`. Every write is DRY RUN unless the
body says `"dry_run": false`; answers carry counts and ids only, except `finals`.
`S` below is the scratch folder `/home/nghiep/projects/AA-Ecosys/.tmp-session/aa1048` (ignored by the root repo).
Every `--out` / `--thresholds` path inside a git work tree is refused unless it sits in that tree's ignored
`.tmp-session/` folder and is not tracked (an ignored path elsewhere in a checkout, such as `scripts/` or `*.jsonl` in
this repo, is refused too), and CI's golden-set text guard fails on any tracked claim-export or claim-label line or
pretty-printed / YAML claim file.

1. Export: `python -m scripts.aa1048.cli export-fixture <fixture.json> --set CL-1 --out $S/claims.jsonl` (every
   writer: a T9-only export of fixture 1.3.0 put 24 of 1,602 claims on the held-out side, every writer 1,289 of
   6,173; `--split held_out` keeps the held-out lines only). `cli split-plan --export $S/claims.jsonl --live
   $S/split-plan.json --ids-out $S/heldout-ids.json` (the live file: `curl $A/split-plan`) prints the held-out claims
   per writer against the R1 target and lists the existing T9 pieces / T2 versions of held-out tours to freeze next
   (`scripts/aa729/select_samples.py --ids <that list> --base-version 1.3.0`, then export the addendum the same way)
   with the writer cost of what is still missing (other objects: the same line format from the DB, with `refs.tour_id` set). Every T2 / T9 line
   carries its tour; a sample without one is refused. A fixture frozen before S272 (T9 refs without `tour_id`)
   needs `--tour-map $S/tours.json`: `{piece_id: tour_id}` from a read-only query (`acp_shared.content_piece` joined
   to `acp_shared.angle_gate_request.trip_id`). A recorded producer's verdicts go on the line as `recorded`
   (`{producer: fired}`) with `recorded_classes` (`{producer: [class, ...]}`); without declared classes the producer
   is measured for exploration only, never enforce.
2. Panel: three agents, one per lens, each with this brief and `$S/claims.jsonl`; concatenate their outputs into
   `$S/panel.jsonl`.
3. Import: `cli payload $S/panel.jsonl --export $S/claims.jsonl --set CL-1 --version 1.0.0 --out $S/payload.json`
   (validated locally; `--apply` makes a write payload), then `curl -X POST $A/import --data-binary @$S/payload.json`
   with the headers above. Run the dry payload first; a second identical write answers `unchanged`, a changed label
   in the same version is refused (409). Local test DB: `cli import ... [--apply]`.
   The import consolidates: `agreed` (3 lenses, one class, one label, none unsure), `disputed` (anything else), and a
   reproducible random 20% of the agreed claims drawn as `audit` (critic F14).
4. Queue: `curl -X POST $A/CL-1/1.0.0/enqueue -d '{"dry_run": false}'` (or `cli enqueue ... --apply`) puts every disputed and audit claim into the disagreement
   queue (source `golden_claim`, admin Writing memory page). The tech lead resolves each: `judge_right` (the panel's
   common label stands), `human_right` (it flips), `unclear` (stays unsure); or imports a tech-lead file (step 3 format).
5. Pull: `curl -X POST $A/CL-1/1.0.0/pull-resolutions -d '{"dry_run": false}'` (or `cli pull-resolutions`) writes the resolutions as tech-lead rows.
6. Report: `curl $A/CL-1/1.0.0/report` (or `cli report`): counts per label / labeller / lens / state / split, agreement per class, the panel error rate on the audit draw,
   contradictions (the bar is 0). Fix contradictions with tech-lead rows before any measurement.
7. Manifest: the `manifest` part of the report (or `cli manifest ...`) into the root repo (ids, counts, digest).
8. Measure: `curl "$A/CL-1/1.0.0/finals" | jq -c '.finals[]' > $S/finals.jsonl` (or `cli finals ... --out`; tech-lead labels; add `--audit-ok` only after the audit draw
   is adjudicated and its error rate accepted: `?audit_ok=true`). Save `cli report` output as `$S/report.json`, then
   `cli detectors --export $S/claims.jsonl --finals $S/finals.jsonl --agreement $S/report.json --split train --out
   $S/rows-train.json` (exploration, label-budget rates) and the same with `--split held_out --out
   $S/rows.json --thresholds $S/rows.yaml` (the record that may pass), `cli rederive <fixture.json> --finals
   $S/finals.jsonl` (every golden label that flips under the policy) and, after its own yes, the R1 run
   (`scripts/aa1048/r1_calibrate.py`, held-out split, DRY RUN first, cap 2 USD, `--out $S/r1-report.json`).

Labels with panel-only provenance never count as release evidence (critic F14): a gate's measurement record cites
tech-lead labels, or agreed panel labels of a set whose audit was adjudicated.

# Fresh-pool labelling brief — two-step claim instrument (AA-1048, S281)

Three panel agents (lenses) label every row of the fresh-pool sheet. Each lens works alone and never sees the stored
label or the R1 votes. This page is what each lens reads.

**Privacy.** The sheet, the sources file and the filled sheets hold real tour text. They stay in the workspace scratch
folder (`.tmp-session/`), never in this repo, a PR, Linear or Jira. The root audit gets the import counts only.

## Input

- `sheet.jsonl`: one row per sentence, with these fields:
  - `n`: the row number;
  - `piece`: the source key;
  - `writer` and `field`;
  - `before`, `sentence` and `after`;
  - four blank answer fields.
- `sources.jsonl`: the full source of each `piece`. Read the WHOLE source of a row before answering: the overview, the
  highlights, every day, the inclusions and the place background.

## Answers (fill all four for every row; keep `n` as given)

| Field | Values | Meaning |
|---|---|---|
| `label` | `violation`, `ok`, `unsure` | `violation`: the sentence promises something about THIS trip that the source does not give, or goes against it. `ok`: everything it says about the trip is in the source, follows from it in one obvious step, or is allowed background. `unsure`: you cannot decide (the claim is then left out). |
| `ok_reason` | `paraphrase`, `verbatim`, `background`, `other` | For an `ok` label only: why it is fine. `paraphrase` means other words or one obvious step. `verbatim` means the source says it nearly word for word. `background` means background about a place, craft, food or custom the source names. Leave it empty for a violation. |
| `step1` | `restatement`, `new_claim` | Independent of `label`: does the sentence add any specific not stated in the source? A restatement adds none. A new claim adds a number, name, time, order, means of travel, service, meal character, guide quality, group or crowd condition, activity, view, access, pace, effort, comfort, safety or suitability, even when that specific turns out to be allowed. Background about a named place is a restatement. |
| `reason` | text | One or two sentences naming the source line, or the missing fact. |

These rules follow the writer source-fidelity policy:
- **Allowed.** Short, well-known background about a place the source names: what it is, its era, its architecture,
  its setting. Seeing such a place's own lasting features on a visit the source gives is allowed too.
- **Not allowed.** Any claim about this trip that the source does not state:
  - activities, food and drink, services (guide, support), transport mode or travel time, clock times;
  - views or sightings, and access the source does not give;
  - accommodation features, inclusions;
  - pace, effort or suitability.

The examples below are invented:
- **Paraphrase.** The source says "walk the ridge above Brask" and the sentence says "a ridge walk high over Brask".
  The row is `ok`, `paraphrase`, `restatement`.
- **New claim.** The source has "the ferry to Olme" and the sentence says "the dawn ferry to Olme". The row is
  `violation`, `new_claim` ("dawn" is not given).
- **Background.** The source has "a stop at Varek chapel" and the sentence says "Varek chapel, carved from one block of
  basalt". The row is `ok`, `background`, `restatement`.

## Return and check

Each lens returns its filled sheet as JSON lines (`lens-<name>.jsonl`, the same rows, the same `n`). The import refuses
a sheet with a missing, duplicated or out-of-range row, a label or `step1` outside the lists, an `ok` without
`ok_reason`, or an empty reason, and it names each problem. Fix the named rows and run the import again. Never fill
placeholders to pass the check.

```bash
python -m services.ground_truth.fresh_pool import --candidates <dir>/candidates.jsonl \
    --sample-manifest <dir>/sample-manifest.json --eligible-manifest <eligible-manifest.json> \
    --sheet lens-source.jsonl --sheet lens-promise.jsonl --sheet lens-skeptic.jsonl \
    [--labels <finals.jsonl>] --out <pool.jsonl> --manifest <fresh-manifest.json>
```

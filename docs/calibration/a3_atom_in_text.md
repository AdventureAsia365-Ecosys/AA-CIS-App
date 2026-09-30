# Calibration — `a3_atom_in_text` (AA-661 / AA-694 A3-1, S204, 30/09/2026)

Question (stage `a3_atomize`, migration 188):
> *Is this place-and-activity stated in this day's itinerary text?* — state = `{day_text, atom}`

Asked per extracted atom in `_atomize_per_day()` before it is stored; a confident no drops it.

## Sample
- 9,044 live platform atoms, each matched to the exact `generated_content` version it was atomized from
  (recomputing `source_hash` over every version — a first pass that used the currently published version
  paired atoms with the wrong day text and is discarded).
- **200 atoms** (seed 697), shadow pass (`adhoc_aa694`), 0 errors. Raw data: `data/a3_atom_in_text_2026-09-30.json`.

## Finding
**127 of 200 atoms (63%) are not in their day's text.** The per-day prompt
(`atom_extraction.build_day_user_prompt`) sends the whole tour's NAME / SUMMARY / HIGHLIGHTS as context,
and the writer extracts highlight places into whatever day it is reading ("Kala Patthar — ascend at sunset"
on the departure day; "Mehrangarh Fort" on "Delhi Departure"). This inflates recurrence and "said" in
the Score and misattributes places to days.

## Labels — agent-proposed
Heuristic (the place's main words appear in the day text) plus a hand read of the 8 rows where the
heuristic and Jev disagreed — **Jev was right in all 8** (e.g. a place only mentioned, not visited). 73 × 1, 127 × 0.

## Result
| Zone | n | Correct | Bad atoms caught |
|---|---|---|---|
| **reject p ≤ 0.30** (proposed) | 127 | **127 (1.000)** | **127 / 127** |
| reject p ≤ 0.50 | 127 | 127 | 127 / 127 |

Lowest-p good atom: 0.53; highest-p bad atom: 0.21. Proposed floor **0.30**.

## Decision
- [x] Nghiệp reviewed all 47 rows of the Google Sheet "Jev calibration — atom in day text A3-1 (2026-09-30)":
  **overturned 0 / 47**. Three rows (#79 Western Hills, #91 Nikko dinner, #107 Nam Lik lunch) are kept at 1
  on trust: the Sheet showed truncated day text; all three sit at p ≥ 0.53, above the reject line.
- [x] **Enforce `a3_atom_in_text`: reject_ceiling 0.30** (Dev, S205, 30/09/2026). Reject ≤ 0.30: 127 / 127.
- [x] Prompt fix (S205): `build_day_user_prompt()` fences the tour preamble as TOUR CONTEXT and the day as the
  only section to extract from; SYSTEM_PROMPT says the same. SYSTEM_PROMPT is in the day fingerprint, so the
  next atomize run re-reads every day. The gate stays as the guard.

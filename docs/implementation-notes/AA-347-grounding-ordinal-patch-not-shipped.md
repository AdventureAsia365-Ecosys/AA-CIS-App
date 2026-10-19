# AA-347 — Patch numeric-fabrication regex gap, unpublish confirmed-fabricated tours
> Renamed 23/08/2026 (AA-441 session): this file's title/original filename said "AA-341" —
> AA-347's own Linear text confirms that was a mislabel ("4 comment bị post nhầm vào AA-341...
> không liên quan", 30/07 session). The real issue is **AA-347** (Done, 30/07/2026).
>
> **Status: the ordinal-digit patch described below was NEVER shipped to
> `services/acp_shared/grounding.py`** — confirmed live on `main` 23/08/2026, the module still
> has no `_ORDINAL_DIGIT_RE`. It appears to have only run inside a one-off audit script for this
> issue's 58-tour scan, not the shared production module both `s1_from_atom.py` and
> `acp_produce/gates.py` call.
>
> **Decision (Nghiep, 23/08/2026, AA-441 session): do NOT ship this patch now.** AA-349 is
> pending a broader redesign decision for this same module (comparing against full source text,
> not just cited atoms) — shipping this narrower patch first would mean revisiting the module
> twice. Kept as reference for whenever AA-349's redesign happens — the "safe to ship" analysis
> below (digit ordinals only, spelled-out ordinals reverted) still stands as a design input for
> that future work.

## Part B summary
- Mechanism used: same SQL + notification as `PATCH /admin/master/{tour_id}/deactivate`
  (`api/routers/admin.py:1071`) — `UPDATE gold_aa_internal.published_tours SET master_status =
  'inactive'` + `NotificationService.emit(MASTER_DEACTIVATED)` — batched into one explicit
  transaction across 39 tour_ids instead of 39 separate HTTP calls. Chosen over `/trash` (which
  also sets `deleted_at`/`deleted_by`) because the task asked for a status change only, not a
  soft-delete, and `deactivate` doesn't touch `deleted_at`.
- Downstream effect verified: `acp_contract.v_trip_registry` (migration 090) LEFT JOINs
  `published_tours` gated on `master_status = 'active' AND deleted_at IS NULL` — deactivating
  nulls every `pt.*` (fabricated/brand) field in that view (aa_name, itinerary_brand, seo_meta,
  quality_score, ...) while `rt.*` (raw source) fields stay visible. Confirmed on 2 sample tours.
  Note: the tenant-facing browse endpoint (`api/routers/v1_tours.py`) and export code
  (`services/export/handler.py`, `api/routers/v1_exports.py`) do NOT filter on `master_status` at
  all — `v_trip_registry` is the only verified gate. If those other paths are ever used to serve
  this content publicly, they would need the same guard; out of scope for this task but worth
  flagging.
- Result: 39/58 tours moved to `master_status='inactive'`, 0 rows deleted (`deleted_at` still
  NULL, `aa_name`/content intact), 39/39 `MASTER_DEACTIVATED` audit rows in
  `shared.notifications`. Single transaction, count-checked before commit, no swallowed
  exceptions. List is the patched-scan output (B1), not the prior session's memorialized 26 —
  see A3 notes above for why the count differs.

## Decisions
- Extending `find_novel_numeric_claims()` in place (services/acp_shared/grounding.py) rather than
  rewriting — the existing raw-digit path is verified 0 false positives / 15 manual checks
  (post tokenization-fix) per [[project_aa341_fabrication_scan]] and must not regress.
- Scope for this patch pass: ordinals (11th, 3rd, 21st) and basic spelled-out cardinals/ordinals
  only — these are the two forms with real evidence of missed fabrications. Ranges, bare years,
  Roman numerals are being surveyed in Part A but not necessarily patched this pass (see Should
  know).

## Changed
- `services/acp_shared/grounding.py`: added `_ORDINAL_DIGIT_RE` + `_numeric_tokens()` helper.
  Digit-form ordinals ("11th", "5th", "3rd", "1st", "17th") now normalize to their bare cardinal
  digit and are compared on both sentence and atom-text sides — this is the exact class that let
  the original "Wat Phou ... 11th century" fabrication through undetected (confirmed via live
  survey against all 58 published tours: 8 real ordinal-digit examples found, all genuine
  century/rank claims).
- Attempted the same for spelled-out ordinal words (first, fifth, eleventh, ...) — reverted.

## Tradeoffs
- Spelled-out ordinal words are NOT patched this pass. Live survey against the 58-tour dataset
  found real fabrication-relevant examples ("fifth-century fortress", "world's first declared
  sanctuary") that are still missed, but adding the word-form regex caused 3 genuine regressions
  in `test_zero_false_positives_on_real_good_content` — "at first light" and "an authentic first
  impression" are idiomatic, carry no fact at all, and got flagged as fabricated. Digit-ordinal
  data showed zero such idiom collisions (nobody writes a bare "5th" non-numerically), so it was
  safe to ship; word-ordinals need either a context gate (only trigger near "century"/"year") or
  an idiom blocklist before they can ship — deferred, asking Nghiep before building either.
- Spelled-out cardinals (four, five, one, two, seven...) were surveyed but NOT attempted at all:
  found in the same live data used non-numerically as often as numerically ("one of Laos's
  least-visited frontier regions" vs "three days cycling") — higher a priori FP risk than the
  ordinal words that already failed, not worth attempting without a design discussion first.
- Ranges ("8 to 18 kilometers"), bare 4-digit years (1943, 2005), and one Roman-numeral hit
  ("Singh II") were all already caught or not fabrication-relevant (Roman numeral was a regnal
  name suffix, not a claim) — confirmed via the same live survey, no code change needed for these.

## Should know
- No committed scan script exists from the prior AA-341 session — the original 58-tour scan was
  run ad hoc via S3-mediated ECS exec (per global CLAUDE.md pattern) and never saved to the repo.
  Re-running Part A3 means re-writing the scan script from scratch against the same query
  (`gold_aa_internal.published_tours WHERE deleted_at IS NULL` vs `raw_tours` source), not
  reusing a prior artifact.
- Part A is read-only (no LLM calls, no DB writes). Part B (unpublish) only runs after explicit
  go-ahead from Nghiep on the Part A report.
- A3 re-run count (this session's re-derived methodology): OLD-regex 38/58, NEW (patched)
  40/58 — does NOT reproduce the prior session's memorialized 26/58. That 26/58 was described as
  "confirmed" (i.e. a manually-reviewed subset), while this run's 38/58 and 40/58 are the
  automated/filtered output before manual review — not the same stage of the pipeline, so they
  are not directly comparable. Net effect of the ordinal patch alone: +2 tours, 18 sentences
  newly flagged, all real ordinal century/rank claims.
- Manual read of the 13 distinct newly-caught claims (across 8 tours) found two sub-classes, not
  one: (a) ~11/13 are clean confirmed fabrications — the century/date has zero support anywhere
  in that tour's own source (Sigiriya "5th-century", Wat Phou "11th century", Haeinsa "13th and
  15th centuries", etc.); (b) 2/13 are a residual limitation of token-set comparison, not
  fabrication in the harmful sense — "18th-century study hall" and "18th-century capital" where
  the source DOES contain the underlying year (1797, 1569 respectively) but expressed as a plain
  4-digit year, not a century phrase, so the token diff still flags it as "novel." The check has
  no century-arithmetic (it can't infer 1797 → "18th century"). Distinct from the "first light"
  idiom problem — this is a precision, not accuracy, gap. Tracked as a known limitation, not
  blocking this patch, since it undercounts false positives by treating accurate paraphrase as
  novel rather than over-counting fabrications.
- A4 (prompts.py / S1 old system prompt audit): no "enrich with cultural/historical depth"-style
  invitation language found. Rule 6 ("Do not make factual claims you cannot verify from the
  source data") and rule 5's explicit ban on inventing meal times both point the other direction
  — the prompt actively tells the model not to fabricate, it just doesn't specifically call out
  historical/date claims as a fabrication-prone category the way it does for meal times. Hypothesis
  A (prompt invites enrichment) has no textual support in this file; doesn't rule out a
  training-data/model-tendency explanation (hypothesis B, being measured separately in AA-339).

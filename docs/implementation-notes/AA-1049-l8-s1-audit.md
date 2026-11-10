# AA-1049 — S1 Master audit (lane l8, S277)

Scope: a READ-ONLY, $0 audit of EVERY active Master of the AA master tenant, run once in the CIS api container through
`tools/ecs/ecsx.sh`. The tech lead asked why only 817 tours look written by the S1 writer v2, how many Masters there
really are, what happened to the other tours, and whether the S1 writing rules were checked. The script answers this
from the live rows, with no model call.

## Decisions

- **The set is the live Masters, the version is the PUBLISHED one.** `gold_aa_internal.published_tours`
  (`tenant_id` = the master tenant, `master_status = 'active'`, `deleted_at IS NULL`) joined on
  `published_tours.generated_content_id`, with its latest `quality_scores` row and its `raw_tours` source. The
  columns come from `final_contract_store.S1_ROW_COLUMNS` / `S1_ROW_FROM` (the read every contract exit makes), so the
  audit judges exactly what the contract would judge. Ordered by `gc.tour_id` (a stable key, lesson S252).
- **Writer label.** `metadata.writer_version` when set (the v2 writer stamps `v2`), else `model_editorial`, else
  `unknown`. Group `v2` vs `older`: every summary count is split by this group.
- **The contract verdict is recomputed today, with the brand words.** `final_contract.check_s1` over
  `s1_input_from_row(row)` with `brand_forbidden_words` set from the master tenant's active brand rule (the same row
  `export_aa1049_s1.BRAND_SQL` picks). `s1_input_from_row` alone leaves the brand list empty, so a brand word would
  pass the stored-row contract; the audit fills it in. The stored verdict (`metadata.final_contract.verdict`) is
  reported next to it.
- **The S1 writing rules, run again on the stored text (all deterministic).** `graph.validate_node` (the codes the
  job gate reads; the DFS keyword check is off because `seo` is empty), `brand_audit_node.pre_audit_checks` (the
  deterministic brand pre-audit), `s1_checks.run_checks` (the S1-first code checks: days, figures, names, meta,
  truncation, inclusions) and the numeric half of grounding (`check_grounding(use_jev=False)`). Codes and fields only,
  never the sentence.
- **No model call, never a silent pass.** `grounding.judge_units` is patched to a recorder that answers no verdict, and
  `LLMClient.generate` / `LLMClient.__init__` raise. Every Master lists `not_run`: `jev_grounding`,
  `llm_brand_audit`, `s1_judge` always; `jev_meal_claims` when the pre-audit wanted Jev for a meal / clock claim (its
  code is then listed under `needs_model`, not as a confirmed hit). A model call attempt is an `errors` entry.
- **The S1 train compose diff.** For each Master: the stored page is fed to `s1_train.compose_page` the way
  `s1_writer_v2_live.write` calls it (tour with the agency fields, `build_fact_table`, `day_plan`, the meta problems of
  the stored meta, the writer itinerary objects split back from the stored `Day N — Title` blocks, like the replay
  cassette). Reported per field: `day_headings`, `fact_lines`, `day_prose`, `day_count`, `seo_title`, `seo_meta`
  (counts only), `slug` = `not_applicable` (an S1 Master has no slug column; the word-boundary rule is the
  `seo_title` fit, see `AA-1049-l4-s1-train.md`). `applies` is true only for a v2 Master (the train never composes an
  older version); the older ones are measured anyway and kept apart in the summary.
- **Population block for the "817" question.** Counts of the master tenant's `raw_tours` by deleted / source status /
  lifecycle stage / Master status and reason / pipeline status / latest version status and writer, and the tour ids of
  live sources without an active Master. Counts and ids only.
- **Output.** One plain `AUDIT_SUMMARY` line (counts), then two gzip+base64 blocks (`s1_master_audit.jsonl`: one line
  per Master; `s1_master_audit_summary.json`). The decode + Markdown step is a separate local file
  (`audit_s1_masters_local.py`) so the container script holds no decode call (the PII hook refuses a decode on the way
  to the database).

## Changed

- New: `scripts/quality/audit_s1_masters.py` (container), `scripts/quality/audit_s1_masters_local.py` (local decode +
  README), force-added (`scripts/` is gitignored, like the other quality scripts).
- Tests: `tests/unit/test_aa1049_s1_master_audit.py` (invented fixtures), `tests/integration/test_aa1049_s1_master_audit_db.py`
  (the three queries run read-only on the migrated schema).

## Tradeoffs

- **Today's code over stored text, not the code of the day each version was written.** That is the question ("would
  it pass today"), but a code that fires today and not in the stored `failure_codes` may be a rule added since, not a
  writer fault: the summary lists `new` and `cleared` codes apart.
- **Positional day alignment in the compose diff.** Composed days are in plan order, stored days in writer order; the
  diff compares block by block. A reordered page therefore counts every moved block, which is what a recompose would
  change.

## Should know

- Live steps: the orchestrator runs the ecsx command, then the local decode, which writes the per-Master JSONL into the
  gitignored `.tmp-session/` and the public-safe README under `docs/audits/2026-11-11-S277-s1-master-audit/`.
- `ecsx` ships one file: the container script imports only the image's `services` / `shared` packages.

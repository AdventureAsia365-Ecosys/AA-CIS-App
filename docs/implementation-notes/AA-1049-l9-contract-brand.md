# AA-1049 — stored-row S1 contract checks the brand words; validate reads a None text field as missing (lane l9, S277)

Two small bugs the S1 Master audit (lane l8, `AA-1049-l8-s1-audit.md`) found.

## Decisions

- **One brand-word resolver.** `services/content_generation/forbidden_words.tenant_brand_forbidden_words(conn,
  tenant_id, brand_name=None)` calls `admin_pipeline._resolve_brand_rule` (the AA-198 / AA-700 resolver every writer run
  uses), so there is no second copy of the brand-rule SQL. It is imported lazily through the module attribute, the same
  way `s1_batch` does (`api.routers` is a higher layer), so the many tests that patch
  `api.routers.admin_pipeline._resolve_brand_rule` also cover this lookup.
- **Which brand.** The version's own `metadata.brand_name` first (the name the writer run stamped, and what
  `_revalidate_tour` resolves), then the tenant's default resolution (`'default'` row, else the newest active row) when
  that name has no active row any more. The words are the ACTIVE list today, not the version the page was written
  under (`metadata.brand_rule_id`): the contract asks whether the stored text is acceptable now.
- **Where it plugs in.** `S1_ROW_COLUMNS` now reads `gc.tenant_id`. `final_contract_store.brand_words_of_row(conn, row)`
  resolves the words for one row; `record_s1_exit` (every stored-row exit: approve, promote / auto_publish via
  `process_export`, human_edit, the legacy approve) passes them to `s1_record_from_row(...,
  brand_forbidden_words=...)` -> `fc.s1_input_from_row(row, brand_forbidden_words=...)`. The re-validation exit already
  had the words (it runs the graph with the brand row; `attach_s1` stores that record) and is unchanged.
- **Report script.** `scripts/quality/final_contract_report.py` resolves the words once per distinct
  `(tenant_id, metadata.brand_name)` (`brand_words_by_key`) in the same read-only transaction and passes the map to
  `summarize(..., brand_words=...)`; without the map it behaves as before (core list only).
- **Report mode never fails an exit.** A brand lookup error is logged (`final_contract_brand_words_failed`) and the
  record is still written with the AA core list alone (the old behaviour). A row without a tenant does no lookup.
- **validate_node.** A text field present with `None` (a NULL column read back, or a model that returned null) reads as
  missing through `graph._text` (""), for `subtitle`, `summary`, `seo_title`, `seo_meta`, `itineraries` and the DFS
  keyword check; `generated` / `tour` that are `None` read as `{}`; a `None` item in `highlights` is skipped in the
  generic-phrase scan. The returned `generated` keeps the `None` (validate does not rewrite stored values).

## Changed

- `services/quality/final_contract.py`: `s1_input_from_row(row, *, brand_forbidden_words=None)`.
- `services/quality/final_contract_store.py`: `tenant_id` column, `brand_key`, `brand_words_of_row`,
  `brand_words_by_key`, `s1_record_from_row(..., brand_forbidden_words=None)`, `record_s1_exit` resolves the words.
- `services/content_generation/forbidden_words.py`: `parse_forbidden_words`, `tenant_brand_forbidden_words`.
- `services/content_generation/graph.py`: `_text` helper and the None-safe reads in `validate_node`.
- `scripts/quality/final_contract_report.py`: brand words per brand key.
- Tests: `tests/unit/test_aa1049_l9_contract_brand_words.py` (the brand-only word fails on main and passes here; the
  None subtitle crashed on main), one new case in `tests/integration/test_aa1047_final_contract_store.py` (a real
  `shared.tenant_brand_rules` row through the resolver, the record and the report).

## Tradeoffs

- **Lazy import of a router function from a service** rather than moving `_resolve_brand_rule` into `services/`: moving it
  touches every importer (scripts, tests that patch it, four services) for no behaviour gain; the lazy import is the
  existing pattern (`s1_batch`, `acp_shared/debate`, `writing_memory/shadow`). A later move can keep the alias.
- **`highlights: None` keeps `HIGHLIGHTS_NOT_LIST`** (it never crashed, and the code is a soft format code); only text
  fields read as missing.
- **The verdict of a stored version can change** at its next exit: a Master whose page holds a word that is only on the
  tenant's brand list now records `FORBIDDEN_WORD` (block, verdict `review`). Report mode only: nothing is unpublished.

## Should know

- Live check after deploy: approve one review-queue item on Dev, then read its record:
  `SELECT metadata->'final_contract'->>'exit' AS exit, metadata->'final_contract'->>'verdict' AS verdict,
  metadata->'final_contract'->'findings' AS findings FROM silver_aa_internal.generated_content WHERE id = '<version>'`.
  Proof the brand list was read: no `final_contract_brand_words_failed` warning in the api / worker log for that
  version, and, for a page that holds a word of the master tenant's active brand list, a `FORBIDDEN_WORD` block finding.
- The S1 Master audit (lane l8) fills the brand list itself (`BRAND_SQL`); it can switch to `brand_words_of_row` once
  this merges.

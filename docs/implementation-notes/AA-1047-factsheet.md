# AA-1047 (lane factsheet) — one FactSheet, its persistence and the W1 input measurements

Part B of AA-1047 (Quality W1, root ADR 0020): the FactSheet module, the declared budget diff, persistence (CIS 386),
the T2 raw-source fields and the input measurements. Parallel lanes on the same issue: policy registry
(`AA-1047-policy.md`, may use CIS 387) and `final_contract` on the exits. This lane does not change an exit path,
the rubric or a policy module.

Design: root `docs/audits/2026-11-06-S271-writing-quality-audit/` (README W1, `data/design-review.json`
`arch.roadmap[1]`, critic fixes F1, F6, F9, F10, F13).

## Decisions

- **Module `services/quality/factsheet.py`, two builders, one `FactSheet`.** The route builder (T9) wraps what
  `service.grounding_text_from` joined; the tour builder (S1, T2) wraps `s1_facts.build_fact_table` +
  `s1_writer_v2.render_fact_sheet`. `render()` returns the legacy text byte for byte: `grounding_text_from` now
  delegates to the route builder; the S1 writer still calls `render_fact_sheet` itself (moving that function would
  touch the writer prompt module for no gain; the tour sheet calls the same function, and the parity test pins it).
- **Line ids.** Route lines keep the claim_rules ids (`S1`, `P2`, `F1` …, from `number_source_lines` over the
  rendered text): the F9 v2 extraction already cites them, so no second numbering. Tour lines are
  `<section>.<n>` over the fact table (`days.2`, `included.4`), 1-based per section, in the fact table's order.
- **Provenance per line (ADR 0020).** Route: seed lines S / L / P / O = `llm_derived` (every seed line is A3 or
  place-card output); a Facts entry = `human_verified` when it is a platform fact (staff-written, AA-814) and
  `tenant_asserted` when it is the agency's (or the owner is unknown: the weaker reading); BRAND / AUDIENCE =
  `tenant_asserted`; `DESTINATION` = `source_raw` (raw_tours.country); `TRIP` = `llm_derived` (the Master's name).
  Tour: every line `source_raw` by default (`provenance=` lets a later caller mark a human-edited source).
  `external` exists in the vocabulary; no builder emits it yet (DFS data does not reach a FactSheet today).
- **Anchors (F10).** `anchor_check(evidence, label, raw_text)`: anchored when the atom's evidence (else its label) is
  a folded substring of its tour's raw source, or when ≥ 60% of its content words (and at least 2) occur there;
  None without a raw source. The route builder takes `block_anchored` per seed block; the T9 live path does not pass
  it yet (it would need the raw rows of every route tour at write time), so live T9 sheets carry `anchored = None`
  and the measurement computes the share directly over the atoms.
- **Exclusions are recorded.** Route: `excluded_facts` (the facts the facts budget left out) -> reason
  `facts_budget`. Tour: figures beyond `MAX_FIGURES` that the FACT SHEET never shows -> reason `figures_cap`. Neither
  changes the text.
- **Budget order (declared diff, W1 item 2), separate commit.** `FactSheet.render_budgeted(max_chars)` is the text
  for a reader with a size cap: over the cap it drops whole lines, never cuts one, in a fixed order (other trips O
  and audience A first, then place background P; last line first inside a tier) and records each with reason
  `reader_budget`; S, F, T, L and B are never dropped (an `over_cap` entry says the text is still too long). A
  header left with no bullet goes with them. Tour sheets are never budgeted (their reader, the S1 writer, has no cap).
  This is the order of the design ("S,F,T,L > P > O"); B (the brand's own words) is kept with the first tier because
  F1 reads the brand's numbers there. No live reader passes a cap yet: the eval judge's 16000-character cut is the
  intended first one (policy lane, `evidence_view`). The T9 facts budget itself is unchanged (same kept facts, same
  order); what it leaves out is now handed back (`fetch_facts_for_writing(excluded_out=)`), kept on the context
  (`facts_excluded`, compact: id, scope, chars) and recorded in the piece's FactSheet with reason `facts_budget`.

- **Persistence (CIS 386, contract, MIG-4).** `acp_shared.fact_sheet`: one row per (tenant, workflow, fs_sha), so
  a retry, a resumed job or a second piece on the same inputs reuses the row; `fs_sha` = sha256 of the canonical
  record plus the rendered text. No UPDATE grant (a sheet never changes), no FK (the owning objects are pruned and
  erased on their own windows). `lines` and `render_text` are tagged `pii:free_text` (agency facts, brand and audience
  lines are tenant content), like the cassette's reply text. RLS like the CIS 360 tables (ENABLE + FORCE, the X1
  parity policies, tenant_isolation for aa_app_user with no grant). Erasure: `delete` (inventory entry).
  `generated_content.fs_id` and `tenant_tour_versions.fs_id` (uuid, no default); a T9 piece keeps `fs_id` and `fs_sha`
  in `writer_input` (no DDL). `tenant_tour_versions` is absent from the CI replay (CIS 001 is skipped; 297 / 300 fail
  there): 386 adds that column only when the table exists, so the replay still builds the table for the tests.
- **Where the writers store and stamp.** Best-effort everywhere, each statement in its own savepoint: S1
  (`admin_pipeline._stamp_fact_sheet`, right after the version INSERT, raw_tours row), T2 (`t2_rewrite_job.run`: the
  sheet is stored before the rewrite and stamped after the save, so a policy hold is stamped too; job result `fs_id`),
  T9 (`run_write_background`, right where the gates' SOURCE is built; `writer_input.fs_id` / `fs_sha`). A missing
  table or column (code deployed before 386) backs off for 5 minutes. `FACTSHEET_PERSIST=off` switches it off;
  unit tests run with it off (`tests/unit/conftest.py`, marker `real_factsheet_store`).
- **T2 source query.** `_PT_SQL` gains `rt.exclusions`, `rt.group_size`, `rt.best_time_to_go` (it already read the
  src_* fields, inclusions and price for the elephant detector), so the T2 sheet is built from the same raw source as
  S1's. `source_tour_dict` (what the T2 writer reads) is unchanged.
- **Input measurements: one script, four passes** (`scripts/quality/aa1047_input_measure.py`, `MEASURE=all |
  anchors | pointers | excluded | drift`). Read-only (a `readonly` transaction, SELECTs only, no whole-row select),
  aggregates and ids only, database errors printed by class. Definitions:
  - *unanchored atoms*: active platform atoms (`v_active_tour_atoms`) whose evidence (else label) fails
    `anchor_check` against their tour's raw source; atoms of a tour without a raw row are `unknown`, not counted;
  - *pointer failures*: `verify_pointers` of the tour FactSheet of every active raw source
    (`source_status = 'active' AND deleted_at IS NULL`); failing tour ids, at most 20;
  - *excluded facts per object*: per T9 piece of the last `PIECE_DAYS` days, the facts the facts budget leaves out
    today for its tenant and destination (the stored seed is post-budget, so the drop is recomputed on today's
    facts), and the lines `render_budgeted(16000)` would drop (the eval judge's cut); per S1 source, figures over the
    FACT SHEET cap;
  - *master_drift*: `s1_checks.run_checks` of every active Master (aa_* fields) against its raw source; blocking =
    at least one finding of a fact class (number, name, inclusion, day structure); human-edited Masters are a source
    (provenance human_verified) and are counted apart. The W1 bar: tune the provenance rules before W3 when the rate is
    above 15% (`tune_provenance_before_w3`).

## Changed

- `services/acp_content_writing/service.py`: `grounding_text_from` = `grounding_factsheet(...).render()`;
  `grounding_factsheet` exposes the sheet; `resolve_facts` stores `facts_excluded` and `fact_scopes` on the context.
- `services/acp_content_writing/facts.py`: `fetch_facts_for_writing(..., excluded_out=None)`.
- `services/quality/factsheet_store.py` (new); `api/routers/admin_pipeline.py`, `services/jobs/t2_rewrite_job.py`,
  `services/acp_content_writing/service.py` call it.
- `scripts/quality/aa1047_input_measure.py` (new; shipped by the root ECS exec tool, imports the deployed code at
  /app). `scripts/` is gitignored, so the file is force-added (`git add -f`) like its scripts/quality neighbours
  (review fix: the first commit left it untracked and the unit test would not import on a clean checkout).
- `api/routers/_tenant_projection.py`: `fs_id` in `TENANT_HIDDEN_VERSION_FIELDS` and, as a whole key, in
  `TENANT_FORBIDDEN_KEY_RE` (review fix). The tenant version detail selects `ttv.*`, and the tenant pool
  `aa_app_user` has a table-level grant on `tenant_tour_versions`, so the projection is what keeps the admin
  FactSheet id out of `/v1/tours/versions/{id}`; the AA-796 poison rows now carry `fs_id`.
- `api/migrations/386_fact_sheet.sql` (migration-only PR), `services/erasure/inventory.py`,
  `tests/integration/conftest.py` (386's FORCE RLS table in the post-244 / post-245 lists).

## Tradeoffs

- The parity test keeps a frozen copy of the pre-W1 `grounding_text_from` and runs it against the builder over a
  grid of 1512 generated input combinations plus fixtures (seeds with route blocks, `[Moment id=…]` heads, place
  background, other trips, thin notes, empty and trailing-newline parts). A hypothesis-style fuzzer would add a
  dependency for little more coverage.

## Should know

- Fixtures use invented places only (Zemlia, Varn Valley, Lumen Gorge …): CI's golden-set text guard.
- **Ship order.** Migration-only PR first (386 + its test + inventory + conftest lists), Migration Apply, then this
  code PR. The code is safe before the apply too (the store backs off on UndefinedTable / UndefinedColumn), but the
  stamps only land after it.
- **Readiness.** `REQUIRED_CIS_SCHEMA` is not raised: nothing fails without 386 (same as the cassette, 385).

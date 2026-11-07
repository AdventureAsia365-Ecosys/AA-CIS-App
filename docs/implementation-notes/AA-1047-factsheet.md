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

## Changed

- `services/acp_content_writing/service.py`: `grounding_text_from` = `grounding_factsheet(...).render()`;
  `grounding_factsheet` exposes the sheet.

## Tradeoffs

- The parity test keeps a frozen copy of the pre-W1 `grounding_text_from` and runs it against the builder over a
  grid of 1512 generated input combinations plus fixtures (seeds with route blocks, `[Moment id=…]` heads, place
  background, other trips, thin notes, empty and trailing-newline parts). A hypothesis-style fuzzer would add a
  dependency for little more coverage.

## Should know

- Fixtures use invented places only (Zemlia, Varn Valley, Lumen Gorge …): CI's golden-set text guard.

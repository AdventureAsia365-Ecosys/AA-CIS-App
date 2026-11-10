# AA-1063 — S276 lane l8: the source of a T2 claim in the review view

## Finding (why 310 of 417 t2@2 1.0.0 items had no source)

- The t2@2 claims come from two exports of round 2: `t2-130-held` (the base golden fixture 1.3.0, samples
  `t2-012` … `t2-020`) and `t2-add-held` (the round-2 **addendum**, made by `select_samples --ids` against 1.3.0).
- The addendum numbers its own samples from 1 (`t2-001` … `t2-011`) and was never merged into a fixture or imported
  into `shared.eval_golden_item`. Its sample ids collide with the base fixture's `t2-001` … `t2-011`, which are other
  T2 versions (other tours).
- The review view's lookup (`review.contexts` → `match_golden`) reads the golden items of the claim's sample id and
  keeps the one whose panel-visible output has the claim's content sha. For the 11 addendum samples it reads the
  base fixture's items, no sha matches, so `found=false` (310 claims: 31+29+18+29+46+17+21+30+15+13+61). The 107
  claims of `t2-012` … `t2-020` match (found=true).
- Checked offline on the round-2 scratch files (counts only): every addendum T2 output hashes to the claims' content
  sha, and every line carries a uuid `version_id`, a `tour_id` and the tenant.
- Worse than "not found": the pre-filter's `golden_id` fallback (sample id only, same tenant) would hand an addendum
  claim the base item of the same sample id when the tenant is the same: the source of another tour.

## Decisions

- **The T2 path reads the agency version the claim was cut from**, by the claim's stored `version_id`, only when the
  version row's tenant is the claim's tenant (`gold_aa_internal.tenant_tour_versions`, SQL filter on the claim
  tenants plus a per-claim check). Its object is `{name, subtitle, summary, highlights, itineraries, seo_title,
  seo_meta, trip_type}` of `rewritten_content`, exactly the projection `select_samples.freeze_t2` froze, so its
  content sha is comparable with the claim's.
- **The source is the T2 writer's source**: the Master tour of the version (`published_tour_id`), read with the T2
  job's own `_PT_SQL` and `source_tour_dict` and flattened like `labels._source_text` (`review.source_text` on
  `{"tour": …}`): the same bytes the panel read for a frozen T2 sample (freeze_t2 stores `_prepare`'s tour_dict).
- **Kinds**: `t2_sha` (the version's object still has the claim's content sha) and `t2_id` (the version is found, its
  text changed since, e.g. a tenant edit). Order per claim: golden_sha, live_sha, t2_sha, golden_id, live_id, t2_id,
  live_wi. The pre-filter's `sources` counts them like the other kinds.
- **golden_id must name the same object**: a golden item matched by sample id is a candidate only when its
  `refs.version_id` / piece id do not contradict the claim's (the addendum collision above). `golden_candidates`
  now also returns `refs`.
- **No tour-only fallback**: a claim whose version row is gone (tenant erased it) gets no source; the Master of the
  tour alone says nothing about what that tenant's writer was given.
- **The review view uses the same resolver** (`resolve_sources`) for the list's context and `GET .../context`, so the
  page, the pre-filter and the agent panel see one source. The context answer gains `source_kind`; for a T2 claim
  also `master_changed` (the Master version was regenerated or edited after the agency version was written: the
  source shown may not be the one the writer saw) and `raw_facts` / `raw_facts_kind` (the version's stored
  FactSheet, `acp_shared.fact_sheet` by `fs_id` of the same tenant, else the sheet rebuilt from the raw tour now).
  The raw facts stay out of `source_text`: the panel judged against the Master only, and the pre-filter's subject key
  hashes the source bytes.

## Changed

- `services/ground_truth/review.py`: `resolve_sources` (the old `claim_sources` body plus the T2 path),
  `claim_sources` wraps it (unchanged shape for the pre-filter), `t2_versions`, `t2_masters`, `raw_facts`,
  `context_from`; `contexts` / `attach_claims` / `item_context` resolve through it; `golden_candidates` returns refs.
- Route `GET /review/{id}/context` and `GET /review` pass the app pool (the T9 live rebuild acquires connections).
- Writing Lab: the full-source panel names the source kind, warns when the Master changed after the version, and
  shows the raw facts (collapsible), EN and VI messages.

## Tradeoffs

- The Master is read as it is now, not as it was when the version was written (no snapshot of the writer input is
  stored for T2). `master_changed` says when that differs; the 11 addendum versions were frozen on 09/11 from the
  same rows, so the panel read today's Master too unless an S1 rerun replaced it since.
- One `_PT_SQL` read per distinct Master (the T2 job's own query, not a copy): a few reads per page.

## Should know

- After deploy: re-fetch `/review/{id}/context` for the 310 items; expect `found=true`, `source_kind` `t2_sha`.
  A `t2_id` means the agency text changed after the export; `master_changed=true` means the Master changed after the
  version. A pre-filter plan call on t2@2 1.0.0 (`{"writers": ["t2"]}`, no Jev call) now counts `t2_sha` in
  `sources` where it counted `unmatched` (or a wrong `golden_id`).
- The addendum itself is still unmerged: merging it renumbers its samples (`t2-021` …), so the claims' sample ids
  would still not name its golden items. The version id is the stable key; the review does not depend on the merge.

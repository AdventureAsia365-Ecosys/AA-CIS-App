# Clean rerun runbook (AA-653)

Reset everything derived from `raw_tours`, then rebuild the platform wave by wave, Bhutan first.
Written S207 (01/10/2026) against Dev `:410`, migration 195.

## 0. Preconditions
- Admin audit fixes live (AA-702): S1 lists active sources only, `v_trip_registry` active only,
  notifications work, External Spend has one key per tenant.
- Jev questions as configured on Jev Decisions (13 enforce, 10 shadow) — see `docs/architecture/at-series-v2-design.md`.
- **RDS manual snapshot** taken right before the reset (`aa-cis-dev-db-pre-rerun-YYYYMMDD`).
- DataForSEO balance checked (External Spend → DataForSEO) and enough for the wave (see §3).

## 1. Reset (once)
Script: [`clean-rerun-reset.sql`](clean-rerun-reset.sql). Part 1 is a read-only count; part 2
deletes in one transaction, child → parent, and only commits when every deleted table is 0 and
`raw_tours` is still 793.

| | Tables |
|---|---|
| **Delete** | publish_log, content_piece, angle_gate_option/request, subject, debate_brand_fit_cache, tenant_atom_state, route_pick · atom_ranking, atom_embedding, atom_matches, atom_segment_member/alias, route, hub, atom_segment, atomize_day_fingerprint, s1_from_atom_runs, tour_atoms · review_queue, quality_scores, seo_context, tenant_tour_versions, published_tours, generated_content |
| **Reset** | `raw_tours.pipeline_status → 'ingested'` (rows, `source_status`, `lifecycle_stage` untouched) |
| **Keep** | raw_tours/raw_sources/upload_staging · tenants, brand rules, tenant config, competitor cache, facts, destinations · **search_demand, segment_research_log, question_embedding** (paid research cache) · all logs (llm_call_log, dfs_call_log, decision_log, job, pipeline_runs, notifications, audit_log) · Jev + model config · all `tripplanner.*` (no FK into CIS tours) |

## 2. Waves
Order: **Bhutan (34)** → Mongolia 17, Sri Lanka 20, South Korea 30, Taiwan 36, Thailand 43, Laos 50,
Vietnam 1 → Nepal 70, Japan 80 → China 119 → India 236 (active sources, 01/10/2026; total 736).

Each wave:
1. **S1** — S1 Rewrite page, filter the country, "Settings default" model. Watch Jobs.
2. **Review Queue** — approve/reject; Master Content shows the wave's masters.
3. **A3 atomize** — Social Content → Atomize, run for the wave's tours (job).
4. **Segment research** — admin research for the wave's places/markets, with the job budget set.
5. **Score / Route / Hub** — recompute; check 02–04 tabs.
6. **Slate** — one test tenant (WanderLux) for the wave; T8–T10 on 2–3 pieces.
7. **Verify pack** (below) + Playwright pass of the touched pages.
8. **Jev** — new verdicts on Jev Decisions; calibrate shadow questions from this wave's data.
9. TripPlanner re-extraction — after the CIS rerun, not per wave (TripPlanner is parked).

### Verify pack per wave
- Masters: `published_tours` for the country = approved count; 0 masters on superseded sources.
- Atoms: every master has atoms; 0 atoms on a day the source doesn't have (A3-1 gate).
- Segments: no cross-country segment (AA-695); landing questions only in the segment's country.
- Costs: External Spend per stage for the wave vs the estimate below; DFS balance after.
- UI: Dashboard, Master Content, Social Content 01–05, Jobs, Jev Decisions load without errors.

## 3. Cost estimate (from llm_call_log / dfs_call_log, 01/10/2026)
| Per tour | LLM | DataForSEO |
|---|---|---|
| S1 (generate + judge + fix + nudge + audit) | ~$0.07 | **~$0.18** (search volume + one ideas task + SERP) |
| A3 atomize | ~$0.05 | — |
| Segment research | small | mostly cached (search_demand kept) |

- **Bhutan wave:** ≈ $2.4 LLM + $6 DFS + $1.7 atomize ≈ **$10**.
- **Full catalog (736):** ≈ $90 LLM + **≈ $130 DFS for S1 SEO** + research.
- DFS balance is **$48** — the full rerun needs a top-up, or S1 SEO mode "Minimal" for part of the
  catalog (decision for Nghiệp before wave 2).

## 4. Rollback
Restore the pre-rerun snapshot to a new instance and swap the secret, or re-run S1 from raw — no
raw data is touched by the reset.

# Ownership, frequency and cross-tenant mechanisms

> Detail page of [CONTEXT.md](../../CONTEXT.md): who owns each content object, how often it is computed and every cross-tenant mechanism. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Ownership / frequency / cross-tenant table (AA-540)

Every step in the A0→A3→T5-T11 chain, answered from real code/schema reads (no inference) —
this is the table that resolves Open Question 1 below.

| # | Step | Owner | Computed | Cross-tenant mechanism |
|---|------|-------|----------|-------------------------|
| 1 | Master Content: Raw Tour→Published Tour (A0-A4) | **Admin** | Once per tour, platform-wide | **Yes — the strongest form: one shared row set.** `silver_aa_internal.raw_tours`/`generated_content`, `gold_aa_internal.published_tours` all carry `tenant_id`, but for Master Content it is always the `aa_internal` sentinel (`_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"`, `api/routers/admin_pipeline/_shared.py`) — every real tenant reads the exact same rows, not a copy. |
| 2 | Atom (atomize @ A3) | **Admin** | Once per published tour | **Yes.** `owner_scope='platform'` (`services/export/handler.py:44-65` `_run_a3_atomize_background()` → `services/acp_produce/tenant_pipeline.py::run_t5_atomize("platform", ...)`). Every tenant's Segment build reads the SAME `acp_contract.tour_atoms` rows (`services/acp_contract/segment_matching.py:395-412`, `WHERE ta.owner_scope = 'platform' AND ta.tour_id IN (... that tenant's own tenant_tour_versions ...)`). The skip-cache `acp_contract.atomize_day_fingerprint` (migration 128, no `tenant_id` column at all) is keyed on `generated_content.id` for A3 atoms (`services/export/handler.py:246`, `version_id=str(row["id"])`) — also platform-wide now, correctly so (column name is a stale holdover from the pre-AA-526 per-tenant path, where it held a real `tenant_tour_version_id`). |
| 3 | Segment | **Admin (platform)** | Once for the whole Master Content pool, re-matched when a tour's atoms change | **Yes — shared rows (AA-545, migration 146).** `acp_contract.atom_segment` has no `tenant_id`; new `segment_id = sha256(place\|verb)`. Every tenant's Slate reads the same Segment set. Was per-tenant from AA-509 (01/09) to AA-545 (06/09) — history in `docs/adr/0001-*.md` / `0003-*.md`. Cached `questions_count` (163) and `contested` (165) columns are platform-wide too. |
| 4 | Research/DFS (Search Demand) | **Admin-owned data, admin-triggered only** (AA-646: `POST /admin/segment-research/run`; no tenant action buys DataForSEO — T2 rewrite only reads the cached `seo_context` row, KAN-90 follow-up 29/09/2026) | Once per `(keyword, market)` / `(place, market)` — NOT per tenant | **Yes — the clearest, most consequential cross-tenant mechanism in the pipeline.** `acp_contract.search_demand` (migration 130:20-27) and `acp_contract.segment_research_log` (migration 130:40-38) have **no `tenant_id` column at all**. `_cached_volume()`/`_store_volume()` (`services/acp_contract/segment_research.py:229-252`) key purely on `(keyword, market)`; `_stale_markets()` (`:440-450`) and `run_segment_research()`'s own skip check (`:476-484`, `if not stale: return`) key on `(canonical_place, market)`. Confirmed real: if tenant A already researched "Kyoto"/`japan` inside `FRESH_FOR` (182 days, `:69`), tenant B's entire LLM ReAct loop for the same place+market is skipped — not just the DataForSEO call, the Bedrock cost too. Deliberate (migration 130:13-19: "a keyword's search volume in a market is a fact about the outside world, not tenant content"), not an oversight. |
| 5 | Score (Atom Ranking) | **Admin (platform)** | One pass per buyer market (6 markets) over the whole Segment pool | **Yes — shared rows (AA-545).** `acp_contract.atom_ranking` primary key `(market, tour_id, segment_id)`, no `tenant_id`. A tenant with several markets merges the matching rows at read time in the Slate. 4-axis rank-sum since AA-610. |
| 6 | Route / Hub | **Admin (platform)** | Route: rebuilt whole per tour (old rows superseded). Hub: persists across rebuilds | **Yes — shared rows (AA-545).** `acp_contract.route` identity `(tour_id, first_day, last_day)` and `acp_contract.hub`, both without `tenant_id`. |
| 7 | Slate (Subject) | **Tenant** | Per tenant | **No.** `acp_shared.subject.tenant_id UUID NOT NULL` (migration 133:19). The similarly-named but unrelated `acp_contract.route_pick` (the Route-pick snapshot, renamed from `acp_contract.subject` at migration 132 — "a different, unrelated concept," migration 133:8) was also `tenant_id UUID NOT NULL` (migration 131:74, pre-rename); it was dropped by CIS 296 (AA-850). |
| 8 | Goal / Angle / Write / Gate / Review / Publish | **Tenant** | Per tenant, per request/Piece | **Mostly no, one deliberate exception.** `acp_shared.angle_gate_request.tenant_id` (migration 113:36), `content_piece.tenant_id` (migration 115:36), `publish_log.tenant_id` (migration 116:27) all `NOT NULL`. Exception: the T10 cannibalization Gate (F10) deliberately reads EVERY other tenant's approved Pieces — `find_similar_pieces(cross_tenant=True, ...)`, `_CROSS_TENANT_QUERY` has no `tenant_id` filter at all (`services/acp_shared/piece_similarity.py:46-56, 67-95`), consumed by `gate_cannibalization()` at the 0.92 cosine-similarity threshold. This is a cross-tenant **read/compare**, not a cache — the embedding itself is never shared: `compute_embedding()` (`services/acp_shared/content_embedding.py`) makes one fresh Bedrock call per Piece, every time, for every tenant, with no caching layer at all. |
| 9 | Facts Entry | **Both** (one table, two scopes) | Platform scope: once, by admin. Tenant scope: per tenant | **Yes for `scope='platform'` rows, by design.** `acp_shared.facts` (migration 145:33-46), `scope` column (`'platform'`\|`'tenant'`), `tenant_id IS NULL iff scope='platform'` (CHECK, migration 145:44-46). RLS (migration 229, AA-817, for the tenant pool role `aa_app_user`; the owner/main pool is exempt): `facts_platform_read` FOR SELECT `USING (scope = 'platform')` — platform rows are readable by every tenant — and `tenant_isolation` FOR ALL `USING`/`WITH CHECK (scope = 'tenant' AND tenant_id = NULLIF(app.tenant_id, '')::uuid)` — a tenant reads and writes only its own rows and can never write, delete or re-scope a platform fact (145's FOR ALL policy allowed that). |
| 10 | Writing memory (Writing Rules, Pinned Exemplars, Releases; ADR 0010) | **Both** (one model, two scopes) | Platform scope: per monthly Release, approved by Nghiệp. Tenant scope: per tenant Release, one human click | **Tenant scope: no** — every `cis_app` read filters `tenant_id = $1` explicitly and the tables carry the `tenant_isolation` RLS policy (FORCE RLS), following the `acp_shared.facts` pattern; a CI test proves tenant A's rules never resolve for tenant B. **Platform scope: yes, by design** — a platform rule may be drawn from ≥ 3 distinct tenants' evidence (D9) and then applies to every tenant's writer, but platform rows never hold tenant text. Tables are CIS-owned in `shared`: `writing_signal` exists (CIS 277, AA-916; isolation tests `tests/integration/test_aa916_migration_277.py`, `test_aa817_rls_tenant_isolation.py`) and `writing_rule` (CIS 280, AA-917; `tests/integration/test_aa917_migration_280.py`: no tenant-pool privilege, a `tenant_id = $1` read never resolves another tenant's rule), and the golden set's tenant items and results `eval_golden_item` / `eval_item_result` (CIS 284, AA-919; `tests/integration/test_aa919_migration_284.py`), the releases and shadow outputs `writing_memory_release` / `writing_memory_shadow` (CIS 287, AA-919 WM-18; `tests/integration/test_aa919_migration_287.py`: tenant A's release never resolves for tenant B), and the pinned exemplars `writing_exemplar` / `writing_memory_release_exemplar` (CIS 313, AA-921 WM-27; `tests/integration/test_aa921_exemplars.py`: as cis_app, tenant A's draft pins only A's examples and resolving for tenant B returns none of them; the link trigger refuses a cross-tenant link). |

**Retired predecessor (AA-915 → AA-923).** The AA-132 lessons loop (`shared.pipeline_lessons`) was
frozen by AA-915 behind `PIPELINE_LESSONS_ENABLED` and its code is gone since AA-923 (WM-34): no
reader, no writer, the S1 system prompt carries no lessons block, and `flag_fix_node._write_lessons_safe`
is a logging no-op kept only because the AA-729 harness patches it by name. The N8 output rules
(`acp_shared.acp_output_rules`, `/v1/rules`, `api/services/acp_post_processor.py`,
`services/acp_produce/rule_adapter.py`) went in the same change. Both tables move to the `retired`
quarantine schema by a later contract migration and are dropped after 14 days (DM-3.9); a unit test
(`tests/unit/test_aa923_retired_lesson_paths.py`) keeps every file from naming them again.
Learned rules reach a writer only through a writing-memory release (ADR 0010).

### Cross-tenant mechanisms — full inventory (AA-540's specific ask)

Since AA-545 the dividing line is simple: **everything up to Route/Hub is shared platform data;
everything from the Slate onward is per tenant.** The shared parts:

1. **The Master Content pool** (row 1) and **the Atom pool** (row 2) — the shared source every
   tenant reads from directly.
2. **Segment, Score, Route, Hub** (rows 3/5/6) — platform-wide rows, no `tenant_id`.
3. **Search Demand + its research-freshness log** (row 4) — a shared cache keyed on
   `(keyword, market)` / `(place, market)`, now also holding SERP domains (AA-631).
4. **Facts Entry, `scope='platform'` rows only** (row 9) — shared by explicit design, RLS-enforced.
5. **Writing memory, platform scope (ADR 0010)** (row 10) — platform Writing Rules may be drawn
   from ≥ 3 tenants' evidence and apply to every tenant's writer; they never hold tenant text, and
   each needs Nghiệp's approval. Tenant-scope rows are isolated.

One former **latent** cross-tenant path, retired: `shared.pipeline_lessons` (AA-132) has no
`tenant_id` and was written from the shared S1/T2 flag_fix node, so a working loop would have let
one tenant's T2 failures steer the global S1 prompt. Frozen by AA-915; its code was removed by
AA-923 (WM-34) and the table goes to quarantine.

Plus one **cross-tenant read (not a cache)**: the F10 cannibalization Gate (row 8) compares a new
Piece's embedding against every other tenant's approved content to block near-duplicates.

Per-tenant tables added 15-25/09 that sit next to the shared data:
`acp_shared.debate_brand_fit_cache` (keyed by tenant, AA-631) and
`shared.unmapped_market_requests` (AA-629).

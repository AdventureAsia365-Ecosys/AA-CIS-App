# T-series — logic, DFS, LLM, and the multi-session plan (AA-741)

Status: **working plan** (S229, 12/10/2026). Owner: Kiro + Nghiệp.
Everything here was read from code and the Dev DB (read-only), not assumed. This is the single
source of truth for the T-series audit (AA-741) and the follow-up work; update it as items ship.

Related: AA-741 (audit), AA-696/699/700/701 (T-series Jev), AA-668/670/671/672 + AA-759/760/761/762
(portal), AA-756 (Jev calibration), ADR 0003 (one durable job model), ADR 0005/0006 (model gateway).

---

## 1. How the tenant flows actually work

### T2 — tenant tour rewrite (job `t2_rewrite`)
- Rewrites AA **Master** content (`published_tours.aa_*`) in the tenant's brand voice, with the
  tenant's own-market SEO keywords. It does NOT rewrite the raw tour.
- Writer prompt = Master text + brand (`system_prompt` into the system prompt; `style_guide`,
  `core_idea`, `customer_*`, `voice_examples`, `good_examples` into judge/prompt) + up to 5
  keywords + 3 PAA + `rewrite_language`.
- SEO source order: tenant-market keywords from `acp_contract.search_demand` → else the US
  `seo_context` row → else no keywords (logged `t2_seo_context_missing`). Recorded in
  `job.result.seo_source`.
- Judge scores brand-fit vs the tenant brand; pass ≥ 7.0 → `ai_generated`, else `needs_review`.
  Up to 3 writes. T3 then checks new numbers against the Master and repairs up to 2 times.
- The tenant's rewritten text is NOT used downstream. T7–T9 run on **platform atoms**; T2 only
  decides which tours are candidates (`tenant_tour_versions` membership).

### T7→T11 — content writing
- **T7 Slate:** candidates = platform segments/routes of tours the tenant rewrote, ranked by a
  plain rank-sum (demand + recurrence + PAA questions + "said" length). Channel bars (blog needs
  demand ≥ 1000 and ≥ 3 questions; social needs said ≥ 150). Debate cuts rule-contested items and
  low brand-fit.
- **T8 Angle:** tenant picks 1 of **8 goals** (see §5), gets 3 angles (Sonnet), ranked by whether
  they answer the PAA.
- **T9 Write:** writer prompt = angle + goal + channel style + atom/route text + facts
  (platform + this tenant) + brand rubric + CTA + keywords. `max_tokens=4096`, not tuned per
  channel. Blog returns a JSON envelope `{seo_title, meta_description, slug, body}`; the other 7
  channels return plain text.
- **T10 Gates:** CTA, grounding, banned patterns, cannibalization (F10, cross-tenant), length,
  SEO surface, F8 framework (warn), F9 (CTA/fact block + style warn), plus blog-only atom density /
  structural variance / FAQ dedup. Up to 2 repairs; still failing → `held` (visible, publish
  gated); one buffer-retry per request. Tenant sees `ready_state` + `flags`, never gates/retries.
- **T11:** export text (every channel) or HTML (blog: full document or `<article>` fragment);
  publish to WordPress.

## 2. DFS (DataForSEO) — tenants never buy; the market skew is an ops gap, not a bug

Two different jobs, deliberately different:
- **`s1_seo_prefetch`** (per-tour, for Master) picks **one** market via `resolve_buyer_market`
  (singular; US ranks first). Correct by design — Master is shared AA content keyed to one
  reference market. NOT a bug.
- **`segment_research`** (per-market demand/PAA for atoms → feeds tenant T7/T8/T9) takes a
  **list** of markets and fans out across them. This is the per-market source for tenants.

Research coverage today (`acp_contract.search_demand`): US 1,602 / UK 1,484 / AU 1,214 (ran all
September), but **DE/FR/NL only 96 each, from 1–3 Sep** (one early trial, never re-run). DFS
supports all six (in `DFS_LOCATION_MAP`). So the fix is operational — run `segment_research` for
DE/FR/NL (G2) — not a code change.

Tenants trigger no DFS spend: 0 tenant-tagged DFS rows since 29/09 (AA-646). A tenant market with
no research falls back to US keywords (T2) or fails the demand bar (T7 blog).

## 3. LLM — all via the gateway; per-tenant cost is under-counted (fix = G1)

Every model call goes through `shared/llm_client` with a route from `shared.llm_role_config`; no
direct SDK calls, no hard-coded model ids in code paths. But many support calls log
`tenant_id=None`, so per-tenant cost (Billing) is too low:

| stage | tenant-tagged / total calls |
|---|---|
| s1_judge | 3 / 8,393 |
| s1_brand_audit | 0 / 37 |
| s1_itinerary_nudge | 0 / 5,940 |
| t10_judge | 0 / 23 |
| f10_embed | 0 / 2,861 |
| t2_generate / t9_write | 27/27, 7/7 (OK) |

T-series also reuses A1 stage keys (`s1_judge`, `s1_brand_audit`…), so T cost mixes with A1 and
admin cannot tune/shadow T judges separately (fix = G4).

Cost note: the earlier "$0.03–0.06 per piece" was from 7 short social test writes only. A blog
piece costs more (longer output + T8 + T10 judges + possible repair/buffer-retry). Real numbers
come from the pilot (G3); do not quote a per-piece figure until then.

## 4. Architecture gaps vs ADR 0003

- T7 Slate runs an LLM judge **inside `GET /v1/slate`**, synchronously (blocks the event loop on
  a cache miss), and writes `acp_shared.subject` in a GET. (G5)
- T8 angle generation runs the model **inside the HTTP request** (502 on parse failure). (G5)
- T9 pre-flight runs one Jev call per fact inside the request before enqueuing. (G5)
- Held-piece buffer retry runs inline in the same `t9_write` job and creates a second piece; a
  crash mid-retry can leave it `processing`. (G5)

## 5. Open questions

- **Goal count:** code has **8** goals (`services/acp_angle_gate/goals.py`), mapped from Ms. Thư's
  "Bảng 1". If the source table has 10, two are missing — confirm with Nghiệp / the source table,
  then reconcile in the T8 audit.

## 6. Tenant isolation (verified)

Per-tenant: `tenant_tour_versions`, `angle_gate_request`, `tenant_brand_rules`, tenant facts,
`content_piece`, within-tenant similarity, and (after G1) `llm_call_log`.
Shared by design: platform atoms/segments/routes/ranking, platform facts, per-market
`search_demand`/`seo_context`, the debate contested cache.
One cross-tenant content path: F10 holds tenant B's piece if it is ≥ 0.92 similar to an approved
piece of tenant A (B is not told the reason).

---

## 7. Multi-session backlog

Ordered by dependency: fix measurement → pilot for real numbers → re-layer → full run → report →
UI. Each item is one PR or one session.

### Stage 1 — measure correctly (before the pilot)
- **G1 [backend] tenant_id on every LLM call.** Set `tenant_id` in the gateway via a ContextVar
  (like `job_id`); remove `tenant_id=None` at the 9 call sites (judge, brand_audit, flag_fix ×2,
  itinerary_nudge, grounding, source_facts, t10_judge, f10_embed). Test: one T2 + one T9 log a
  tenant on every row. $0, 1 PR.
- **G2 [ops, Nghiệp runs] `segment_research` for DE/FR/NL.** DFS budget $5/run hard-stop; Kiro
  gives an estimate, Nghiệp triggers. Without it, the Europe-brand comparison is meaningless.

### Stage 2 — pilot (~$1)
- **G3 [audit] pilot 3 tours × 4 tenants + one slate→angle→blog→social per tenant.** Measure real
  cost per stage, **blog vs social separately**, incl. T8+T9+T10+retry; measure T7/T8 request
  latency (evidence for G5); collect ground-truth labels during the run.

### Stage 3 — re-layer
- **G4 [backend] separate T-series stage keys** (`t2_judge`, `t2_brand_audit`, `t7_debate_judge`…)
  so T cost/routing split from A1. Before the full run.
- **G5 [backend] move T7/T8 + T9 fact-select to the worker** (jobs `t7_slate`, `t8_angles`; split
  buffer-retry into a child job). Ends event-loop blocking and the T8 502. Pair with AA-668.
- **G6 [backend] consolidate judge clients; move T11 dispatch into `services/acp_publish`;** clean
  stale model docstrings.

### Stage 4 — full run + report
- **G7 [audit] full 48 tours (12 × 4 tenants)**, S218 method.
- **G8 [audit] T-series report + mechanism table + roadmap**; open a follow-up issue per
  "not fit" mechanism.

### Stage 5 — tenant management + portal
- **G9 [admin] expand Tenant 360** — it is the **inline panel inside `/admin/tenants`** (AA-666),
  not a separate page. Add to its tabs: Cost ($/stage vs plan revenue → margin, per-tenant budget,
  $/tour, $/piece); Quality (pass/held per gate, tenant edit rate, brand distinctness, per-brand
  golden set); Markets (research coverage per market, `unmapped_market_requests` queue).
- **G10 AA-759 PortalShell → G11 AA-760 page audit + state fixes → G12 AA-668 Writer panel (with
  G5) → G13 AA-761 Account → G14 AA-671 Marketplace → G15 AA-762 Team & roles + AA-703 login**
  (after the identity ADR + SES).

### Dependencies
```
G1 ─┐
G2 ─┴→ G3 (pilot) ─→ G4 ─→ G7 ─→ G8
                      └→ G5 ⇄ AA-668 (G12)
G9 parallel after G3 (needs real cost numbers)
Portal: G10 → G11 → G12 → G13 → G14 → G15
```

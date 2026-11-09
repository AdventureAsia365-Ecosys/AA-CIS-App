# AA-1055 (PR 2, buy side) — one SERP per place, keyword map, USD cap, Standard queue, recompute after research

Lane l2-dfs-buy, S274. Implements "PR 2" of the S273 DataForSEO design (root repo
`docs/audits/2026-11-08-S273-session/dfs-cost-audit/design.vi.md`, sections 2, 4, 5, 6 and 7, direction A approved by
the tech lead in S273) and item 1 of the S273 system picture (`system-picture/system-picture.vi.md` section 5:
research must reach Score / Questions / the Blog bar without a manual recompute). Ships as two PRs: the migration
CIS 393 alone (contract, MIG-4), then this code PR, which raises readiness to 393.

## What was built

**Migration CIS 393** (`api/migrations/393_place_keyword_demand_tiers.sql`, contract).
- `acp_contract.place_keyword`: the stored keyword map, one row per (subject_kind, subject, keyword). `subject_kind`
  `place` (subject = `atom_segment.canonical_place`) or `country_activity` (subject `<country>|<activity>`, a tour-level
  phrase with no place; country and activity required). Jev zone / mode / probability and the proposing prompt's
  version are kept; a rejected keyword stays so it is never proposed or bought again.
- `acp_contract.search_demand`: `related_searches`, `paa_retrieved_on`, `serp_location`. Backfill: rows holding a bought
  SERP (non-empty PAA or domains) get `paa_retrieved_on = retrieved_on`, `serp_location = market`.
- `acp_contract.segment_research_log.tier` (`volume` | `serp`, default `volume` = the backfill); primary key becomes
  (canonical_place, market, tier).

**Research (`services/acp_contract/segment_research.py`, `segment_research_batch.py`).**
- Scope: Segments with an atom of an active platform Master (`v_active_tour_atoms`), minus `resolve_exclusions`
  (transit / unnamed place, same rule and Jev pick as the recompute). A dry run reads stored Jev picks only
  (`cached_only=True`), so a preview never pays for a verdict.
- Phase A (map): a place with a map of the current `PROMPT_VERSION` reuses it (no LLM, no Jev). Otherwise one LLM
  call per 15 places, Jev per keyword, and the map is stored per chunk right away. A stale place with no usable map
  is stale in every requested market (its keywords are new), even where its volume tier is fresh.
- Phase B (volume): per stale market, read the fresh store, buy the rest in tasks of up to 1,000 cleaned keywords
  (`clean_keyword`: accents folded, <= 10 words, <= 80 chars, not only digits). A failed task is split in two down to
  125 keywords, but only when DataForSEO refused the task: a queued task it accepted (and charged) that then failed or
  timed out (`QueuedTaskFailed`) is never split nor posted again in the run. Each place's `volume` log row is written
  as soon as all its keywords are answered in that market.
- Phase C (SERP): one SERP per place, US / English, for its best keyword with volume > 0 in any market. Skipped (and
  logged fresh) when that keyword's US row holds a fresh SERP with both PAA and related searches. A place whose
  keywords all have volume 0 is logged fresh (nothing to buy) only when the US volume of EVERY keyword is known (a
  fresh US store row, or answered this run); otherwise it waits (`serp_waiting_volume`), stale.
- Phase D (keyword suggestions): off by default (`use_suggestions=False` in the route, the job and the function).
- No log row on error, abort or budget stop (AA-647): the next run resumes there.
- Bounds: `max_usd` (the selection's capped DFS estimate) replaces the 200-place cap; the default and ceiling is what
  the DFS budget still allows, never above `MAX_USD_PER_RUN` (50, also when the budget row sets no limit);
  `max_places` stays as an optional extra limit; the route refuses `max_usd` > 50. The job narrows the run's DFS budget
  to `max_usd` (`narrow_to`: hard per-run cap = min(max_usd, row cap)), so the check before every paid post stops the
  run at the approved, balance-checked figure, whatever the estimate missed.
- Estimate (`estimate_plan`): `dfs_usd` (real: map size or 2.565 keywords/place, SERP for 77.3 % of places) and
  `dfs_usd_cap` (4 keywords/place, a SERP for every place). The route and the job (`before_buy`) check the DFS
  balance against `dfs_usd_cap`, not against the whole remaining budget.
- Recompute: a run that stored new demand enqueues ONE debounced platform recompute (`WAVE_DEBOUNCE_S`, folded into a
  queued one) for the Segments of the places it changed (`enqueue_recompute_for_places`, never raises). More than
  `IMMEDIATE_RECOMPUTE_MAX_SEGMENTS` (300) Segments outside the night window are left to the nightly landing sweep
  (result `recompute_deferred: "landing_sweep"`), unless `LANDING_SWEEP=off`.
- DFS ledger rows carry `stage=a3_search_demand` and `meta {"job": "segment_research", "queue": ...}`.

**Standard queue (`services/seo_intelligence/dfs_queue.py`).** `task_post` (up to 100 tasks, paid), `tasks_ready` and
`task_get` (free) for search volume and organic SERP (advanced). Picked by `DFS_RESEARCH_QUEUE` (env, default `live`)
or the job payload `dfs_queue`; `live` keeps today's path. Same client checks (admin switch, circuit, budget per task,
one ledger row per post with DataForSEO's cost). A task that errors or times out fails that task only
(`QueuedTaskFailed`, with its task id); a failed free poll (`tasks_ready` / `task_get` transport error) is asked
again on the next poll. `serps(..., on_result=)` reports each task as soon as it is collected, and research stores
the SERP and writes the place's log row from that callback, so a worker restart during the wait keeps every SERP
already answered. Posts are
serialised behind a lock (budget check, paid post, charge), waits run in parallel: every SERP request of a run is
posted before the first poll, and the volume tasks of a market are posted together. A budget stop or a fatal answer
after some posts stops posting but still collects the tasks already paid for (stored and logged); the rest fail and
the run's guard aborts.

**Landing sweep (`services/acp_contract/landing_sweep.py`).** The worker calls it hourly; it acts only between 17:00
and 23:00 UTC (00:00–06:00 ICT), once per 20 h, never beside a queued / running recompute. Candidates: live platform
Segments (not excluded, stored picks only) that never landed (`questions_count IS NULL`) or landed before their own
place's newest research row. One debounced platform recompute with those `segment_ids` (never-landed first, at most
5,000). Switch: `LANDING_SWEEP=off`.

**Free copy of S1's October SEO data (`services/acp_contract/seo_context_copy.py`, `scripts/aa1055/seo_context_to_store.py`).**
Keyword ideas with volume -> volume rows; the seed's PAA + related -> its SERP columns. Merge only (never over a newer
volume or SERP). Dry run by default, `APPLY=1` writes.

**Trial (`scripts/aa1055/dfs_queue_trial.py`).** One volume task (50 stored US keywords) and one SERP task through the
queue; prints DataForSEO's cost per task, the wait, and how many volumes equal the stored live value. Writes nothing to
the store; the posts are in `shared.dfs_call_log` (stage `adhoc_aa1055`).

**Readers.** `FRESH_FOR` 182 -> 365 days (volume); `SERP_FRESH_FOR` 365 days. Admin overview / SEO / Tenant 360
count the `volume` tier only. `precompute_question_landings` also reads rows that hold a SERP but no volume yet.

## Decisions
- One SERP per place is stored on the keyword's US row, not a separate table: every reader (landing, T9 FAQ, top-up)
  already reads `search_demand` by keyword.
- A SERP-only row has `search_volume NULL` and `retrieved_on = epoch`, so the volume pass treats it as stale and the
  ranking (`search_volume IS NOT NULL`) does not read a fake 0.
- An empty PAA box in a new SERP keeps the stored PAA (it says nothing against it); related searches are always
  replaced (NULL = never stored, [] = a fresh answer with none).
- `prompt_version` = `a3_propose@<sha12 of the prompt>`: editing the prompt re-proposes every place, no manual bump.
- `country_activity` rows: the migration accepts them; nothing writes them yet (PR 3 of the design, S1 store read).
- Queue unit prices stay at the live price in `DFS_CALL_ESTIMATE_USD` until the trial measured them: an estimate is a
  pre-check and must not undercut the real price.
- The landing sweep runs in the worker loop (no new job kind, no Infra deploy-guard change); its recompute is the
  existing `recompute` kind.

## Changed vs the issue
- `max_places` is optional (no longer mandatory) and has no 200 ceiling; `max_usd` is the bound.
- The loop strategy (`strategy="loop"`) is refused (route schema, `run_segment_research`, job): its estimate counted
  volume tasks only while each place could still buy a SERP per market and a suggestion. `_research_place` and
  `_VolumeBatcher` stay (unit-tested, cited by the gateway token-budget allow-list) until a follow-up deletes them.

## Fix pass (independent review, S274)
Six findings, all real, all fixed:
1. Volume-fresh place without a map logged "serp: nothing to buy" for keywords never bought -> no-map places are
   stale in every requested market (`_plan`), and "nothing to buy" needs every keyword's US volume known.
2. Standard-queue SERPs were stored only after the whole wait -> `on_result` callback stores and logs per task.
3. Research recompute ran in working hours whatever its size -> more than 300 Segments wait for the night sweep.
4. `max_usd` bounded the selection, not the spend; no limit row = unbounded -> budget narrowed to `max_usd`,
   fallback `MAX_USD_PER_RUN`.
5. `strategy="loop"` bypassed the cap and the suggestions default -> refused.
6. Queue split re-bought accepted (charged) tasks -> `QueuedTaskFailed` is never split.

## Tradeoffs
- The 300-Segment threshold for an immediate recompute is a judgement, not a measurement: a scoped recompute of a few
  hundred Segments lands in minutes (Jev and embeddings mostly cached); a whole-country run waits for the night.
- A timed-out queued task is not polled again by a later run (its id is only logged): its keywords are bought again
  next run. Rare (timeout 1 h) and cheaper than keeping a task ledger.
- The sweep could ask for a nightly recompute for a Segment whose disputed moment has no stored Jev pick (the rule
  says "not excluded", the live decide() would exclude it): bounded to once per 20 h at night, and the recompute
  stores that pick, so it stops the next night.
- `select_within_usd` takes the longest affordable prefix in place-name order; an expensive place early in the order
  is not skipped in favour of cheaper later ones (simple, predictable, resumable).

## Should know
- The first night sweep after deploy lists up to 5,000 Segments (S273: 1,272 never landed + 7,786 landed before the
  newest demand) and enqueues one platform recompute for them: Jev landing verdicts and question embeddings are
  mostly cached, but read its `llm_call_log` cost the next morning. `LANDING_SWEEP=off` (task env) stops it.
- A line-numbered allow-list entry (`tests/unit/test_s271_gateway_token_budget.py`, `segment_research.py:<line>`)
  moves with any edit above the research prompt; it moved from 483 to 484 here.
- Order: the migration PR, Migration Apply (contract; the old `ON CONFLICT (canonical_place, market)` stops working
  after it, so no `segment_research` job between the apply and this deploy — admin-triggered only), the
  `bot/db-schema-reference` PR, then this PR.
- After deploy: run the free copy (dry run, then `APPLY=1`), then the trial (~0.1 USD). The trial decides whether R1/R2
  use `"dfs_queue": "standard"`; lower the queue prices in `shared/cost_guard.py` in a follow-up PR with the measured
  cost.
- Admin API body: `{"markets": ["US"], "max_usd": 5, "tiers": ["volume", "serp"], "dfs_queue": "standard"}`.

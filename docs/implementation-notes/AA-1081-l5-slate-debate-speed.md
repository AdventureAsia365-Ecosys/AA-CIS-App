# AA-1081 (lane l5 of S278) — T7 slate run time (Debate judge concurrency, cache kept for unchanged Segments)

Parallel lane note (the first AA-1081 slice, Blog topic coverage, is `AA-1081.md`).

Live evidence (S278, 4 `t7_slate` jobs on the audit tenants, CIS 318a7982): 756-894 s per job against the S276 p50
of 232 s. Almost all of it is `t7_debate_judge` run one call after the other: 319-326 ok calls per job at ~2.2 s each
(sum 635-766 s), ~0.037 USD per job, plus 25-36 refused shadow calls on acc3. The same tours were judged for these
tenants in S275, yet no call was served from the cache.

## Decisions

- **Two passes in `debate.assess_candidates()`.** Pass 1 walks the candidates in order on the one connection
  (contested, cache read, the evidence read for a miss) exactly as before. Pass 2 runs the misses: the Jev pre-filter,
  then (grey) `score_candidate_fit` in a worker thread, its `record_call(**billed_fields)` row and the cache write
  (`_judge_brand_fit`), at most N at once (`asyncio.Semaphore` + a `ThreadPoolExecutor` of N threads). Each signal is
  stored at its candidate's index, so the signal list (and the rank / cut / Slate built on it) is identical to a
  serial run; the unit test compares serial vs 8-wide on a judge whose replies finish out of order.
- **The setting is an env flag, `T7_DEBATE_CONCURRENCY`** (integer, read per call; unset / not a number / < 1 = 8,
  > 32 = 32; 1 = the old serial run). Same kind of switch as `T7_DEBATE_MODE`; not a key in
  `services/quality/thresholds.yaml`, which holds calibrated score cut-offs under change control, not throughput knobs.
  No task-definition change is needed: 8 is the default the moment the code deploys.
- **The gateway still bounds every call.** Each thread runs with a copy of the caller's context
  (`contextvars.copy_context().run`), so the job's tenant (`call_log.current_tenant_id`) and capacity line reach
  `LLMClient`; the per-connection `max_concurrency` / `rpm_limit` semaphore and the TPM reservation
  (`breaker.limit` / `capacity.reserve`, thread safe) and the tenant AI budget check (`tenant_budget.check`, per call)
  apply exactly as before. A call that would exceed a limit waits there or is refused by the gateway; Debate counts
  a refusal as it always did (the candidate passes, `error`).
- **One connection, one statement at a time.** The cache write goes through `conn` under an `asyncio.Lock`;
  `record_call`, `record_billed_failure` and Jev's `decide()` open their own short connections (as before), so up to
  N extra short connections run at once during the pass.
- **Failure rule unchanged.** A failing judge call (malformed reply, refusal, budget stop) makes that one candidate
  pass with `brand_fit_source="error"` and its billed cost row; the other calls go on.
- **Why the cache missed: NOT confirmed live yet (no DB access from the lane).** A brand-fit cache row is deleted
  in one place only, the A3 atomize clear (AA-645 step 0, T13, `export.handler.clear_brand_fit_cache`; plus tenant
  erasure); `segment_gc` keeps every Segment that has a cache row. So three other causes leave the old rows in place
  and are told apart by what is still in the table:
  1. **brand version** — the key is `(tenant, segment_id, brand_version)`; a new active brand row (a new
     `version` in `shared.tenant_brand_rules`, the row `_resolve_brand_rule` picks) makes every lookup miss while the
     old rows stay under the old version. That miss is correct behaviour and this fix would not change it.
  2. **Segment id churn** — the slow run judged ids the earlier run never saw: a Segment minted after it, or one an
     alias merge made canonical (AA-857 / AA-988 re-mint). Also correct behaviour.
  3. **atomize clear** (the code defect fixed here) — `_a3_atomize_chain` dropped the rows of **every** Segment of
     the tour (all tenants, plus the Routes through them) after **any** atomize that was not `skipped`. Day
     fingerprints are keyed per content version (`atomize_day_fingerprint.tenant_tour_version_id`), so a new
     published version re-reads every day (status `success`) even when the text gives back the same atoms (atom ids
     are content hashes of the place and action). A recompose of the served Masters (path A-new) or a rerun between
     S275 and S278 therefore empties the cache of those tours' Segments for every tenant.
  4. otherwise **not cached before** — the earlier run never stored a score (not a candidate then, call failed).
  The check is `scripts/quality/diag_aa1081_debate_cache_miss.py` ($0, read-only session, asyncpg + stdlib only so it
  runs as one file through the S3-mediated ECS exec as `cis_app`). Per tenant of the slow run it takes the window from
  the tenant's previous succeeded `t7_slate` to the slow run, and for each Segment the slow run judged (its cache rows
  written since the run start) names ONE cause in the order above: an older row of the tenant under another
  brand version; the Segment created or merged into in the window; a tour member of it whose `a3_atomize` job in the
  window has `brand_fit_cache_cleared` > 0; else not cached before. It also prints the brand rows and current
  version, the cache rows per brand version, the `t7_debate_judge` calls per job and the window's atomize totals.
  Whatever the verdict, the atomize clear is a defect on its own: the rule meant "the atom text changed", the code
  checked "atomize ran". If the verdict is `brand_version` or `new_or_merged_segment`, the S278 slowness was correct
  re-judging, and the concurrency half of this slice is what fixes the run time.
- **Confirmed cause (orchestrator fills in after the check):** _pending_ — record the summary `by_cause` per tenant
  and the verdict here before merge.
- **The fix: clear only Segments that changed.** `export.handler.segment_states()` reads, for the tour's Segments,
  the label (`canonical_place`, `canonical_action`: the judge's topic) and every member atom id (any tour: the judge's
  evidence) before and after the segment stage; `changed_segments()` names the Segments whose label or member set
  differs, or that are missing from either read; only those lose their cache rows. The before-read is taken after
  atomize but before the segment stage, which is where the memberships change (new atoms join, the member rows of
  dead atoms are dropped, AA-988), so it also covers atoms an earlier failed attempt of the same job wrote
  (`AtomizeIncomplete` raises before the segment stage). The check now runs on every atomize, also `skipped` (two
  small reads); if a state read fails, every Segment of the tour is cleared, the old behaviour.
- **What still invalidates (correctly):** a brand version change (new key), a Segment whose member atoms changed
  (re-judged on the new evidence), a relabel.

## Changed

- `services/acp_shared/debate.py`: `debate_concurrency()`; `assess_candidates(concurrency=)` in two passes;
  `_judge_brand_fit()` (one cache miss, the LLM call site, registered in
  `tests/unit/test_aa927_llm_call_site_registry.py`); `_brand_fit_signal()`; log line `debate_judged`
  (candidates, judged, concurrency).
- `services/export/handler.py`: `segment_states()`, `changed_segments()`, `_SEGMENT_STATE_SQL`; the atomize chain
  clears only the changed Segments; outcome gains `brand_fit_segments_changed` (and `brand_fit_cache_cleared` is now
  always present, 0 when nothing changed).
- `scripts/quality/bench_aa1081_debate_concurrency.py` (new, $0, force-added): the production `assess_candidates`
  over 320 invented misses with a fake judge.
- `scripts/quality/diag_aa1081_debate_cache_miss.py` (new, $0, read-only, force-added): the cache-miss cause check
  above; tests `tests/unit/quality/test_aa1081_cache_miss_diag.py`, `tests/integration/test_aa1081_cache_miss_diag_sql.py`.
- Tests: `tests/unit/test_aa1081_debate_speed.py`, `tests/integration/test_aa1081_segment_states_sql.py`; the
  expected atomize outcome in `test_aa526_a3_atomize_trigger.py` and the skipped-atomize test in
  `test_aa645_step0_publish_atomize.py` follow the new keys.

## Tradeoffs

- Width 8 by default: 320 misses at 2.2 s go from ~12 min to ~1.5-2 min. The OpenAI direct connection's own
  `max_concurrency` / TPM rows decide whether 8 really run at once; wider is a single env change, not a deploy.
- Threads, not an async gateway call: `score_candidate_fit` and `LLMClient.generate` are synchronous (the judge client
  is shared with the S1/T2 graphs); moving it off the event loop also stops the job's loop from being blocked for
  ~2 s per call (the old NOTE in the code).
- The cache-invalidation fix keeps the explicit T13 clear instead of adding an evidence hash to the cache key: a hash
  column would be a contract migration (ALTER of an existing table, MIG-4 chain) for the same result.
- A budget stop mid-pass: up to N calls are already in flight when the tenant budget refuses; each still makes its own
  per-call check, so the overshoot is at most N judge calls (~0.0001 USD each).

## Should know

- Benchmark (`PYTHONPATH=. python3 scripts/quality/bench_aa1081_debate_concurrency.py --candidates 320 --widths
  1,4,8,16`, reply time 2.2 s scaled by 0.01, local): width 1 = 737 s projected (the S278 jobs: 756-894 s), 4 = 184 s,
  8 = 98 s, 16 = 51 s; the signals are identical at every width. The projection leaves out the per-call
  `record_call` / Jev connection set-up, which now overlaps too.
- The first `t7_slate` after this deploy still re-judges what the earlier clears emptied (the cache is lazy); the
  second refresh with unchanged Segments should show ~0 `t7_debate_judge` calls. A rerun / recompose that really
  changes atoms still re-judges the changed Segments only.
- Before / after evidence lives in the jobs: `shared.job.result` of `a3_atomize` (`brand_fit_cache_cleared`,
  `brand_fit_segments_changed`) and of `t7_slate` (`segments_seen`), and the `debate_judged` log line of the worker.
- Running the cause check (orchestrator, read-only): upload `scripts/quality/diag_aa1081_debate_cache_miss.py` to the
  scripts bucket, presign, then ECS exec on the api task:
  `python3 /tmp/s.py --run-start <first S278 t7_slate created_at, ISO>` (tenants default to every tenant with a
  `t7_slate` job since then; `--tenant <uuid>` narrows). Read `summary.by_cause` and each tenant's `verdict`,
  `brand_version_changed`, `current_brand_version` vs `cache_by_version`, and `judge_calls` of the earlier slate.

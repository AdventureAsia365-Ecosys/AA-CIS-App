# AA-105 + AA-113 — S2 Deterministic Confidence Scorer + Circuit Breaker

## Decisions

- `compute_confidence()` takes 4 signals: keyword_count, competitor_count, cache_hit_rate, gsc_data
- `cache_hit_rate` mapped from binary `dataforseo_cache_hit` bool (0.0=fresh, 1.0=cached) — only one DataForSEO call exists in the graph, so rate is always binary
- `gate1_override` and `confidence_dimensions` stored inside `s2_visibility_report` JSONB dict — avoids a new DB migration; `RunContext` and `S2StagePayload` schemas untouched
- circuit breaker fires at START of expand_scope if `expand_attempts >= 1` (pre-check, no API waste); also fires at END of first expand if `new_count < threshold` after API call
- `competitor_count` fetched from `acp_silver_s2.competitor_inputs` always (even on cache hit) so the score reflects the actual configured competitor set, not a stale zero

## Changed

- `synthesize.py`: removed `confidence_score` from LLM prompt entirely — LLM no longer produces a score; confidence comes only from `compute_confidence()`
- `synthesize.py`: `visibility_reports.confidence_score` is now 0.0–1.0 (not 0–100 as before — the old LLM value was divided by 100 before writing); the new scorer produces 0.0–1.0 directly
- `apify.py`: queries `competitor_inputs` even on cache hit to populate `competitor_count` in state
- `expand_scope.py`: increments `expand_attempts` in state on first call; returns `gate1_override='manual_required'` if result still below threshold

## Tradeoffs

- Binary cache_hit_rate (0.0 or 1.0) is imprecise but honest — there is only one DataForSEO call per run. A more granular rate would need to count cached vs. fresh tool calls across all nodes; that adds cross-node state plumbing not justified yet.
- Storing gate1_override inside s2_visibility_report is slightly implicit but avoids a migration. A future migration can promote it to a dedicated column.
- expand_scope circuit breaker fires after ONE attempt (expand_attempts >= 1) not two — this is intentional. The initial dataforseo call already tries the primary seed; expand_scope is already the second attempt.

## Should know

- Pre-existing test failures in test_s2_idempotency.py and test_s2_semaphore.py (5 tests) — they call `run_s2` without `s1_run_id` which is required. These were failing on develop before this PR.
- GSC stub always returns `gsc_data_present=False` until OAuth is wired — confidence score will never include the +0.10 GSC bonus until AA-XXX implements GSC OAuth.
- Gate 1 auto-approve test (`test_gate1_override_blocks_autoapprove`) uses `patch("services.acp.s2.router.get_run_context_validated")` — works because the import is at module level in router.py.

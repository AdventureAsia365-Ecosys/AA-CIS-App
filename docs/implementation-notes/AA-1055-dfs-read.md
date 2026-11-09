# AA-1055 (PR 1, read side) — T2 keyword fit, Slate MAX demand + miss reasons, one DataForSEO buyer

Lane l1-dfs-read, S274. Implements "PR 1" of the S273 DataForSEO design (root repo
`docs/audits/2026-11-08-S273-session/dfs-cost-audit/design.vi.md`, section 7) and its decision 7 (approved in S273).
No migration, no DataForSEO / LLM call, no new job kind.

## What was built

**T2 (tenant tour rewrite).**
- `services/seo_intelligence/keyword_fit.py` (new) holds the S1 writer v2 keyword filter `relevant_keywords` (moved,
  not copied: `s1_writer_v2.relevant_keywords` is now the same function object, so the S1 v2 live path, the AA-645
  harness and the patch in `test_aa900_llm_access_denied` keep working), `is_english` and `writer_keywords`
  (= none for a non-English rewrite, else `relevant_keywords`, at most 5).
- `t2_rewrite_job.t2_seo(..., tour_dict=, rewrite_language=)`: after the existing reads (admin US row, then the
  tenant-market cache) the keyword list, flat and nested, is replaced by `writer_keywords` against the Master text the
  rewrite starts from. PAA is kept. `diag["seo_keywords"] = {"in", "kept", "language_blocked"}` lands in job.result.
  Both T2 orchestrators (graph path and the opt-in linear one) and the writing-memory shadow pass the language.
- `graph.validate_node`: for a T2 draft (`is_tenant_rewrite`) DFS_INTENT_UNDERUSED is judged on `writer_keywords`
  only, so it fires only when a fitting keyword existed and the writer ignored it. A1 / S1 is unchanged.

**T7 Slate.**
- `_SEGMENT_CANDIDATES_SQL`: `demand_volume` = MAX over the tenant's markets (NULL only when no market has a number),
  new `demand_market` (the market giving it; ties by the tenant's market order), `questions` = MAX over its markets.
  `total_rank` (the order) and `said` are unchanged. `Candidate.demand_market` (default None).
- `bar_misses` / `bar_status` / `bar_status_counts`: why a candidate misses a Bar — `no_data`, `below_bar`,
  `few_questions`, plus `few_said` for the social Bar. `propose_slate` returns `bar_status` per Channel with a Bar in
  the t7_slate job result.

**One buyer.** `tests/unit/test_aa1055_dfs_single_buyer.py`: AST scan of api / services / shared / scripts; every
`DataForSEOClient(...)` must be in the allow-list (segment research, admin SERP top-up, and the two S1 paths marked
pending for PR 3); a free `DataForSEOClient().fetch_balance()` is allowed anywhere; a stale allow-list entry fails
(the list only shrinks); the tenant paths (v1 routers, acp_content_writing, acp_shared except the top-up, the
t2/t7/t8/t9 jobs, t2_* modules) never import the client module nor call a buying entry point.

**Before / after script.** `scripts/aa1055/slate_bar_compare.py` (read only, READ ONLY session): per tenant and
Channel, Bar counts per reason under the old rule (best-ranked market) and the new rule (MAX), plus the Subjects held.

## Decisions
- T2 keyword limit 5 (design: 3-5). T2's prompt and `seo_keywords_used` already slice `[:5]`; S1 v2 stays at 3.
- The language gate removes keywords only; PAA questions are kept (English questions still give a German writer
  topics; the design only asked to block keywords). A place name is not kept as a keyword substitute (design: "có
  thể"); left for PR 3's `demand_profile`.
- `is_english`: empty / None = English (every rewrite defaults to en-US); primary subtag `en`, or the word English.
- Filtering sits in `t2_seo` (one place every T2 caller reaches) AND in validate_node (belt and braces: a T3 repair
  round or a replay that builds its own seo dict is judged on the same rule).
- Slate `questions` = MAX across markets. atom_ranking counts questions over every market already (design section
  1), so in practice the value is the same in every market; MAX avoids a no-data market's 0.
- The miss reason is NOT written into `cleared_bar_reason`: only cleared candidates are stored, and that column is
  returned to the tenant by `GET /v1/slate` (tenant sees content only). The reason lives in the job result (admin).
- Constant `BAR_FEW_ASKED` (value "few_questions"): a `*QUESTION*` constant name with an `a_b` value is read as a
  Jev key by `services/quality/lint.py` (`test_aa926_registry` failed on the first name).
- The guard keeps S1's two buy paths in an explicit PENDING list rather than failing today: PR 3 makes S1 read the
  store only and must delete those entries (the stale-entry test forces it).

## Changed vs the issue
- Nothing dropped. Added `diag["seo_keywords"]` (T2 job result) and `bar_status` (t7_slate job result) so the change
  can be measured live without a new endpoint.

## Tradeoffs
- MAX demand makes Slate more generous for a multi-market agency (a Segment with 1,000+ searches in any of its
  markets clears Blog). This is the design's intent; the compare script measures it per tenant.
- The compare script copies both SQL rules so it gives the same output on the old and the new image; unit tests
  keep its "after" SQL equal to the module's and its bar rule equal to `slate.bar_status`.
- The WM-20 release-0 replay script still calls `t2_seo` without a language (it must also run on older images):
  a non-English job replays with keywords. Replay-only, documented here.

## Should know
- Slate output changes after deploy only when a t7_slate proposal runs (stale after its window or a refresh).
- Existing T2 versions are not rescored; new rewrites get the new rule.
- No new tenant-visible field: `GET /v1/slate` shape unchanged.
- `.gitignore` ignores `scripts/`: the compare script and its `__init__.py` are force-added (`git add -f`, like
  `scripts/aa1048/*`). The unit and integration tests load it by path, so an untracked copy passes locally and
  fails on a clean CI checkout; the fix pass re-ran the AA-1055 tests from a `git archive HEAD` export (91 passed).

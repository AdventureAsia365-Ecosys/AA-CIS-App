# AA-351-03/05 — GPT-4.1 as an alternative F8/F9 judge (trial, feature-flagged)

Task: `docs/claude_tasks/AA-351-03-gpt41-judge-f8-f9.md`. Branch
`feature/aa-351-gpt41-judge-trial`. Follows `docs/implementation-notes/AA-351-gpt56-judge-
trial.md` (AA-351-02, GPT-5.6 Sol — still blocked on AWS access as of this session).

## UPDATE 17/08/2026 (AA-351-05) — credits restored, real F8/F9 data + S1 judge recovery confirmed

Task: `docs/claude_tasks/AA-351-05-run-comparison-credits-restored.md`. No branch (docs-only,
main). Nghiep confirmed via OpenAI Billing Console: new Credit Grant $10.00 (16/08/2026, expires
09/2027), balance $5.10/$15.00 available. Re-ran with real credits — this session spent ≈$0.07
total (Part A + Part B combined), well inside the $5.10 available.

**Part A — S1 production judge (`judge_node.py`) recovery: CONFIRMED, not assumed.** Called the
exact client construction `judge_node.py` uses in production
(`openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])`, model `gpt-4.1`) from inside the live ECS
Dev container. Real success: HTTP 200, `model: gpt-4.1-2025-04-14`, latency 2.522s, usage
85 in / 53 out tokens, response parsed cleanly as the judge's expected JSON shape
(`brand_fit_score`/`cross_brand_distinct`/`mission_present`/`feedback`). Not a 429 quota error —
credits are real and the judge path works end-to-end again.

**Part A2 — outage-window estimate: attempted, inconclusive, not a real bound.** Queried the 200
most recent `silver_aa_internal.generated_content` rows for presence of `metadata->'judge'`
(the block `admin_pipeline.py::_build_metadata` only writes when `judge_brand_fit is not None`).
105/200 rows in that sample lack it, spanning `created_at` from 2026-05-22 to 2026-08-13 — i.e.
**this signal does NOT isolate the credit-outage window**: `judge_node.py`'s own
`has_brand_signals` guard skips the judge (same "no judge block written" outcome) for any tour
with no brand differentiation profile, completely independent of OpenAI credits, and that skip
condition is scattered across 3 months of the sample, not clustered near a plausible
credit-exhaustion date. Reporting this honestly as **not usable** to bound AA-419's scope
estimate — a real answer needs either the OpenAI dashboard's own usage/error history (Nghiep-side,
not available to this session) or a DB query that also joins tenant brand-profile completeness to
separate the two "no judge block" causes, which this task did not build.

**Part B — F8/F9 real comparison, same 9 pieces as the Nova Pro baseline (run
`88f094b1-3e0a-4b28-9abb-205cb7d21287`, 2026 W2), pinned explicitly (not "latest completed run")
so this is the same content AA-351-02 measured:**

- **F9: Nova Pro 1/9 (11%) vs GPT-4.1 9/9 (100%).** Every one of the 8 pieces Nova Pro failed
  (all `GENERIC_AI_WORDING`/`SUMMARY_OFF_BRAND`/`BODY_EXPERIENCE_DETAILS_TOO_GENERIC`-type flags,
  with specific quoted phrases) GPT-4.1 passed clean, zero violations.
- **F8: Nova Pro 5/6 vs GPT-4.1 5/6** — identical outcome, same single piece failed for both.
- **Real cost/call** (from actual `usage` fields, not estimated): Nova Pro $0.002272/call
  (28,298 in / 3,577 out tokens across 15 calls), GPT-4.1 $0.004658/call (27,254 in / 1,920 out
  tokens across 15 calls) — **≈2.05x** Nova Pro, in line with the ≈2.5x paper estimate from
  AA-351-03 (GPT-4.1 uses noticeably fewer output tokens per call, which pulls the real multiplier
  below the raw per-token rate ratio).
- **Real latency/call**: GPT-4.1 averaged 1.45s, Nova Pro 1.73s — GPT-4.1 slightly faster, not
  slower, on this sample.
- **0 errors, 0 `judge unavailable` fallbacks on either side** — both backends fully functional
  for all 30 real calls this session made (15 F9 + 6 F8, ×2 models, minus the 3 pieces with no
  social framework).

**held_reason consistency (AA-382's actual question) — NOT answered by this data, and the raw
pass-rate gap is a yellow flag, not a green one.** AA-382's question is whether a judge keeps
re-flagging a *new* phrase every *repair round* even after the previous flagged phrase was fixed
— i.e. consistency across multiple judging passes of evolving content. This trial ran each judge
exactly **once** per piece, on the original (already-scored-by-production) content — it does not
touch repair rounds at all, so it cannot confirm or rule out GPT-4.1 fixing the moving-target
problem. What it DOES show: GPT-4.1 passing 9/9 pieces that Nova Pro found real, quotable,
specific violations in in 8/9 cases reads at least as plausibly as **GPT-4.1 judging more
leniently** (near rubber-stamp on this sample) as it does "GPT-4.1 is a more accurate judge."
Nothing in this session's data distinguishes those two explanations — an actual test of AA-382's
question would need the SAME piece re-judged across ≥2 real repair rounds under each backend,
which this harness does not do. Recommend NOT reading the F9 pass-rate gap as a resolved answer to
AA-382 without that follow-up.

**Not done, deliberately, per task scope**: no change to production `JUDGE_MODEL` (still unset in
the ECS task def — Nova Pro remains default everywhere). No re-judging of historical published
content. No further balance-burning calls beyond what the 9-piece comparison needed.

## ⚠️ Headline finding — NOT what this trial set out to measure

**The OpenAI API key this app uses has zero credits.** Every real GPT-4.1 call this
session made — through the exact same client construction production already runs —
failed with `Error code: 429 - insufficient_quota / credit_balance_exhausted: "You have
no credits remaining."` This is not a bug in this trial's code (confirmed: the request
authenticated correctly — a wrong/invalid key gives a 401, not a 429 quota error — and
the 9 real gate calls all reached OpenAI and were rejected only for billing reasons).

**This means GPT-4.1 is currently non-functional everywhere in production it's used**,
not just in this trial:
- `services/content_generation/judge_node.py` — S1's brand-fit judge, called with
  `model_tier="gpt-4.1"` which `shared/llm_client/client.py::generate()` routes as
  **direct GPT-4.1, no Bedrock fallback** (`"Direct GPT-4.1 — no Bedrock fallback
  (explicit choice)"`, `client.py`'s own comment). Every real S1 pipeline run's
  brand-fit judge call is failing right now.
- The failure is **silent and non-blocking by design** (`judge_node.py`'s own
  docstring: *"Any failure is non-blocking: any GPT error or parse failure logs and
  leaves validate's quality_score untouched"*) — S1 content has been shipping without
  brand-fit judging, with only a `judge_failed_graceful` warning log line as evidence,
  since whenever the credits ran out (unknown from this session — not investigated,
  out of scope for this issue).
- `services/content_generation/brand_audit_node.py` and `services/acp_s4_social/
  llm_client.py` both also read `OPENAI_API_KEY` directly — same exposure, not
  individually re-verified this session (confirmed only by grep, not by triggering a
  real call).
- `shared/llm_client/client.py`'s T3 (GPT-4.1 last-resort fallback, used when both
  Bedrock Sonnet and Haiku — native and both satellites — fail) is also unusable until
  credits are restored, though T3 firing at all is already rare.

**This needs Nghiep's attention independent of the AA-351 judge trial** — recommend
adding credits at https://platform.openai.com/settings/organization/billing/ (the exact
URL OpenAI's own error message gives) as a priority ahead of any judge-model decision
below, since it's a live, silent quality-pipeline gap in production S1, not a trial
question.

## Decisions

- **Reused `judge_node.py`'s exact client construction, not `LLMClient.generate()`'s
  full T1-T3 chain.** `invoke_judge_gpt41()` calls `openai.OpenAI(api_key=os.environ.get
  ("OPENAI_API_KEY"))` directly — same SDK, same env var, same auth as production
  already runs for S1's judge. Deliberately NOT going through `LLMClient.generate
  (model_tier="gpt-4.1")`, because that class's `"gpt-4.1"` tier is designed as one
  option in a multi-tier fallback chain (or, for judge_node.py's own usage, a direct
  call with no Bedrock attempt) — this backend needs a single deterministic call every
  time it's selected, the same "no fallback, just this one backend" shape
  `invoke_judge_gpt56()` (AA-351-02) already established for its own satellite call.
  Consistent structural choice across all 3 alternative judge backends now in this file.
- **`temperature=0`**, matching the deterministic-judge convention both Nova Pro and
  GPT-5.6 already use (their `inferenceConfig.temperature: 0`) — OpenAI's Chat
  Completions API takes it as a top-level kwarg instead of a nested config object.
- **Extended (not rewrote) `aa351_compare.py`.** Added one env var, `COMPARE_MODELS`
  (comma-separated, e.g. `nova_pro,gpt41`) — unset preserves the exact original
  hardcoded `["nova_pro", "gpt56"]` behavior byte-for-byte, so AA-351-02's own
  reproducibility isn't disturbed. Output S3 key also branches on this (
  `aa351_compare_results_<models>.json`) so this run's output never overwrote
  AA-351-02's original `aa351_compare_results.json`.
- **Real cost impact of this trial's failed calls: effectively $0.** OpenAI does not
  bill for a request rejected at the quota-check stage before any tokens are
  generated — confirmed by the harness's own captured `input_tokens`/`output_tokens`
  fields being stale (see "Known harness limitation" below), which is itself consistent
  with the call never reaching generation.

## Changed

1. `services/acp_produce/judge_client.py`
   - `invoke_judge()`'s `model` routing extended: `model == "gpt41"` now routes to the
     new `invoke_judge_gpt41()`. `"gpt56"` and the Nova Pro default are both completely
     untouched — confirmed by a dedicated regression test (see below).
   - New `invoke_judge_gpt41()` — direct OpenAI API call, GPT-4.1, same
     `{text, model_used, provider, input_tokens, output_tokens}` return shape the other
     two backends use, so `gates.py`'s parsing code needs zero changes for any of the 3
     backends.
   - New constant `GPT41_MODEL = "gpt-4.1"`.
2. `tests/unit/test_aa351_judge_gpt41.py` (new) — 7 tests: default-stays-Nova-Pro,
   explicit `model=`/env var routing to gpt41, a regression check that gpt56 routing
   still works unaffected by the new branch, direct-OpenAI-not-Bedrock assertion,
   usage/text parsing, and the same "never imports generation/writer modules"
   structural isolation re-check (ADR-2026-014/027 L3) AA-351-02 already established the
   pattern for.
3. `docs/claude_tasks/AA-351-03-gpt41-judge-f8-f9.md` (new) — task prompt, committed
   first per the Implementation Notes Pattern.
4. `s3://aa-cis-bronze-{acc2-id}/scripts/aa351_compare.py` — extended with
   `COMPARE_MODELS` (see Decisions above). Not part of this repo's git history (same as
   AA-351-02's original upload) — a throwaway ops harness, not a deployed app module.

**Not changed**: `gates.py` (all 3 call sites), `pipeline.py`, `repair.py`, the F8/F9
rubric content, `_JUDGE_SYSTEM_PROMPT`, `FRAMEWORK_RUBRICS`, `BRAND_SEO_FAILURE_CODES`,
`GENERIC_AI_WORDING_ANCHOR`, and `invoke_judge_gpt56()` — nothing about what gets judged
or how changed, only which model can optionally do the judging.

## Real run — what happened

Ran the extended `aa351_compare.py` (`COMPARE_MODELS=nova_pro,gpt41`) against the exact
same 9 real pieces from run `88f094b1-3e0a-4b28-9abb-205cb7d21287` (2026 W2) AA-351-02
used for its Nova Pro baseline — same read-only re-judge-via-real-gate-functions
methodology, no new N7 run, no writer cost. Executed via the S3-mediated ECS exec +
daemonize pattern (interactive SSM sessions reliably drop mid-run, per
`ecs-exec-long-sync-daemonize` — same as AA-351-02 hit).

**Nova Pro side: reproduced cleanly, consistent with AA-351-02's original numbers**
(same run, same content, same gate functions — expected to match exactly): F9 1/9 pass,
F8 5/6 pass (facebook+tiktok subset).

**GPT-4.1 side: 0/9 real judge calls succeeded** — all 9 pieces' `f9_gpt41` (and 6/6
`f8_gpt41`) came back as `{"passed": false, "violations": ["judge unavailable: Error
code: 429 - ...insufficient_quota...credit_balance_exhausted... — manual check"]}`. This
IS the correct, intended graceful-degradation behavior (`gates.py`'s existing
`except Exception as e: violations.append(f"judge unavailable: {e} — manual check")`) —
confirms the new backend's error handling integrates correctly with the real gate
functions, even though it produced no usable comparison data.

**Known harness limitation, found and left as-is (not worth fixing for a script that
produced no usable data to salvage):** the compare script's `_wrapped()` timing/token
capture (`_last_call` dict) only updates on a SUCCESSFUL `invoke_judge()` return — an
exception path (every gpt41 call here) leaves `_last_call` holding the PREVIOUS
successful call's stats, so the `latency_s`/`input_tokens`/`output_tokens` fields under
every `f9_gpt41`/`f8_gpt41` entry in the results JSON are actually stale copies of that
piece's own `f9_nova_pro` numbers, not real GPT-4.1 numbers (there are none — the calls
never got token-billed). Flagging this so nobody later reads those fields as real GPT-4.1
latency data.

## Comparison table — all 3 alternative judge backends (updated 17/08/2026, AA-351-05)

| | Nova Pro (production default) | GPT-5.6 Sol (AA-351-02) | GPT-4.1 (AA-351-05, real data) |
|---|---|---|---|
| **Status** | ✅ real data (3 independent runs, same numbers) | ⚠️ still blocked — AWS Support case 178689930800206 open, pending AWS reply | ✅ real data — credits restored 16/08/2026, verified live |
| **F9 pass rate** (9 channels) | 1/9 (11%) | not measured | **9/9 (100%)** — see leniency caveat below |
| **F8 pass rate** (fb+tiktok, 6 channels) | 5/6 (83%) | not measured | **5/6 (83%)** — identical outcome, same piece failed both |
| **held_reason consistency** (AA-382's core question) | not yet re-tested across repair rounds in this trial | not measured | **not answered** — this trial is single-shot per piece, not across repair rounds; the 9/9 F9 pass rate is at least as consistent with "judges more leniently" as "judges more accurately" — see AA-351-05 section above, do not treat as resolved |
| **Real latency/call** | 0.8–3.1s (measured, see AA-351-02's table); this session's 15-call subset averaged 1.73s | not measured | **1.45s avg** (15 calls, real) — slightly faster than Nova Pro on this sample |
| **Real cost** (per real rate cards, verified not guessed) | ~$0.80/1M in, $3.20/1M out (AA-404 CloudWatch-derived); real **$0.002272/call** this session | $5.50/1M in, $33/1M out (Sol, standard tier, accepted rate card) — ~6x Nova Pro | $2.00/1M in, $8.00/1M out; real **$0.004658/call** this session (≈2.05x Nova Pro on actual usage, both backends' real token counts differ from the raw rate-card ratio) |
| **Blocker type** | none | AWS-side (Bedrock model-access authorization gap, console/runtime state mismatch — see AA-351-02's evidence block for an AWS Support case) | none — resolved (was OpenAI billing, credits added by Nghiep 16/08/2026) |
| **Next unblock step** | n/a | Wait on AWS Support case 178689930800206 | n/a — unblocked; open question is AA-382 consistency-across-repair-rounds, not access |

**Net result: 2 of 3 backends now have real comparison data.** GPT-5.6 Sol remains the only
still-blocked backend (AWS-side, out of this session's control). GPT-4.1's F9/F8 numbers are
real, but the headline 9/9 pass rate should be read as "needs a leniency check," not as a
finished answer to AA-382 — see the leniency caveat above before using this table to argue for
a `JUDGE_MODEL` change.

## Verify

1. `pytest tests/unit/ -q` → **1347 passed** (was 1338 after AA-351-02; +7 new AA-351-03
   `gpt41` tests, plus the existing gpt56/Nova Pro tests all still pass unaffected). 0
   regressions. `flake8` clean on all changed files.
2. Comparison table above — GPT-5.6 column still pending (unchanged from AA-351-02);
   GPT-4.1 column attempted for real, blocked on billing, documented rather than
   fabricated.
3. Not deployed to Dev this session — feature-flagged, zero production risk either way,
   and there was no real comparison data to validate live against. Will deploy as part
   of normal PR merge; nothing about this change needs an isolated live-verify step
   beyond the unit tests (same reasoning AA-351-02 used for merging its own PR #173
   without deploying separately).
4. This report.

## Should know

- No production behavior changed by this PR — `JUDGE_MODEL` is still not set in the ECS
  task definition; every `gates.py` call site still defaults to Nova Pro exactly as
  before AA-351 started.
- The Dev ECS container's on-disk `judge_client.py` was temporarily overwritten (same
  S3-mediated exec pattern AA-351-02 used) to run this trial against the real acc2/OpenAI
  paths without deploying an unmerged branch — does not persist past the next real
  deploy, same caveat AA-351-02's notes already gave.
- `openai` package import happens inside the function (`import openai` inside
  `invoke_judge_gpt41()`), matching `invoke_judge_gpt56()`'s equivalent inline import of
  `get_satellite_client` — neither pulls its extra dependency into `judge_client.py`'s
  module-level import graph unless that specific backend is actually selected.

## Next steps (for Nghiep to decide — not done here)

1. ~~Add OpenAI credits~~ — **DONE 16/08/2026** (Nghiep). Verified live 17/08/2026 (AA-351-05
   Part A) — S1's `judge_node.py` is confirmed working again with real credits.
2. ~~Re-run `aa351_compare.py`/equivalent once credits exist~~ — **DONE 17/08/2026** (AA-351-05
   Part B), real F8/F9 numbers now in the comparison table above.
3. **GPT-5.6 Sol** — still blocked, AWS Support case 178689930800206 open, waiting on AWS reply.
   Unrelated to the OpenAI credit issue.
4. **AA-382's actual question (held_reason consistency across repair rounds) is still open** —
   this session's data does not answer it; see the leniency caveat in the comparison table. If
   GPT-4.1 as judge is worth pursuing further, the next real test is re-judging the SAME piece
   across ≥2 repair rounds with each backend, not another single-shot pass-rate comparison.
5. **Do not change the production `JUDGE_MODEL` default** regardless of what any future
   comparison shows — reserved for Nghiep, unchanged from AA-351-02's own constraint.
6. **AA-419** (S1 judge silent-failure bug) — Part A of AA-351-05 confirms recovery; the
   outage-window scope estimate (Part A2) is inconclusive per the caveat above and needs either
   OpenAI's own usage-history dashboard or a better DB query (joined to brand-profile
   completeness) to actually bound. Comment posted on AA-419 with this data; status left for
   Nghiep to decide.

## Merge + deploy (16/08/2026, on Nghiep's go-ahead — "ci green thì merge luôn")

PR #175 merged (squash, commit `a2ff3dd`) once all 5 required CI checks passed. `Deploy
Dev` workflow (run [31960090188](https://github.com/AdventureAsia365-CIS/AA-CIS-App/actions/runs/31960090188))
ran clean on the push: ECR build+push, ECS deploy (`Wait stable` + `Smoke test` both
passed), Lambda deploy, Vercel — all 4 jobs green. Confirmed live:
- `aws ecs describe-services`: task def `aa-cis-dev-api:106`, rollout `COMPLETED`,
  desired=1/running=1.
- Running task's image digest — `sha256:e5d289c5...fdb5ef` — matches `ecr:latest`
  exactly, tagged `dev-a2ff3dd3...` (the merge commit hash).
- `curl https://api-cis.lumiguides.it.com/health` → `200 {"status":"ok",...}` in 0.79s,
  task `healthStatus=HEALTHY`.

This is a feature-flagged, additive change (`JUDGE_MODEL` still unset in the ECS task
def) — the deploy carries zero production behavior change; it's confirmed live purely so
the `gpt41` backend is available to re-trigger the comparison the moment OpenAI credits
are restored, without needing another deploy cycle first.

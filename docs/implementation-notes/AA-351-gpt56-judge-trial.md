# AA-351 — GPT-5.6 Sol as an alternative F8/F9 judge (trial, feature-flagged)

Task: `docs/claude_tasks/AA-351-02-gpt56-judge-f8-f9.md`. Branch
`feature/aa-351-gpt56-judge-trial`. Follows `docs/claude_audit/AA-351-gpt-bedrock-3accounts.md`
(survey) and `docs/implementation-notes/AA-382-repair-rubric-context.md` (real evidence of Nova
Pro's F9 "moving target" flagging).

## Decisions

- **Sol only, not Terra/Luna.** The task assumed all 3 GPT-5.6 variants shared one agreement —
  confirmed FALSE this session: `list-foundation-model-agreement-offers` returns 3 distinct
  `offerId`s with materially different rate cards (Sol $5.50/$33, Terra $2.20/$13.20, Luna
  $0.22/$1.32 per 1M input/output token, standard tier). Surfaced this to Nghiep before accepting
  anything irreversible; Nghiep chose Sol only (flagship, to test whether GPT-5.6 as a *family* has
  judge potential before considering a cheaper tier). Terra/Luna agreements were never accepted —
  `openai.gpt-5.6-terra`/`-luna` remain inaccessible on all 3 accounts.
- **`global.` inference-profile prefix, not `us.`** — matches the existing Claude satellite
  convention (`bedrock_satellite.py`'s `INFERENCE_PROFILE_SONNET`/`HAIKU`, AA-397) and is ~10%
  cheaper per the rate card's `*_global_standard` dimensions.
- **Reused the acc3 satellite session, not a new AssumeRole path.** `invoke_judge_gpt56()` calls
  `shared/llm_client/bedrock_satellite.py::get_satellite_client("bedrock-runtime", account="acc3")`
  — the exact same AssumeRole/session-cache mechanism the Claude satellite writer already uses.
  One fewer IAM chain to reason about, and it means this trial's judge calls go through the same
  production credential path a real deploy would use (relevant for the "cost via real production
  path" verification below).
- **Converse API, not `invoke_model`.** Confirmed by the AA-351 survey's own real invoke tests:
  GPT-5.6 on Bedrock only supports `INFERENCE_PROFILE` + Converse, unlike Nova Pro (raw
  `invoke_model` with a JSON body) and Claude satellite (also `invoke_model`, Anthropic body shape).
  `judge_client.py` now documents all 3 distinct request/response shapes this repo talks to.
- **Selection: `JUDGE_MODEL` env var, read per-call inside `invoke_judge()`, plus an explicit
  `model=` kwarg that overrides it.** Reading per-call (not caching at import) lets a single
  process toggle between judges without restarting — required for the "same content, re-judged
  twice" comparison methodology below. The `model=` kwarg exists so the comparison script (and any
  future caller) can select a backend explicitly without mutating process-wide env state; every
  existing call site (`gates.py`'s F8/F9/F9-social, 3 call sites) passes neither, so it is
  unaffected — default is `"nova_pro"` unless `JUDGE_MODEL` is set in the environment (it is **not**
  set anywhere in the ECS task def; production behavior is unchanged by this PR).
- **Comparison methodology deviates from the task's literal instruction, deliberately, and is
  documented here rather than asked about.** The task asked to trigger a brand-new N7 week, then
  (if the architecture allows) re-judge the same generated content twice. It does not — but the
  same "same content, 2 judges" comparison is achievable more cheaply and more rigorously by
  re-invoking the real gate functions (`gate_brand_seo_audit`, `gate_brand_seo_audit_social`,
  `gate_framework`) directly against **already-generated real pieces** (`acp_deliver.pieces`,
  read-only, no writes) from the most recent completed N7 run, toggling `JUDGE_MODEL` between calls.
  This is strictly better than triggering fresh E2-E5 generation for this purpose: it avoids
  re-paying writer cost, avoids ~60+ minute N7 run time (AA-382's real run took 66 min including 2
  infra incidents), and is a cleaner "same content" comparison than 2 separate N7 runs would be
  (no risk of the writer's own run-to-run variance confounding the judge comparison). The gate
  functions themselves are called unmodified — the prompts sent to both judges are therefore
  byte-identical except for the model routing, satisfying the task's "same rubric" requirement by
  construction rather than by manual verification.
- **F8 comparison scoped to facebook/tiktok, not blog.** `gate_framework()`'s framework key for
  blog pieces (`Brief.framework`, resolved from `FRAMEWORK_TABLE[(funnel_stage, "blog")]`) is not
  persisted on the `acp_deliver.pieces` row and depends on `funnel_stage`, which isn't recoverable
  from a single-row read either — reconstructing it accurately for already-generated pieces isn't
  cleanly possible. facebook/tiktok frameworks ARE deterministic (`FRAMEWORK_TABLE[("ANY", channel)]`
  — `hook_story_cta`/`hook_beats_payoff`, fixed regardless of the tour's funnel stage), so F8 is
  compared on those 2 channels only. F9 (the actual AA-382 motivating problem) has no such gap and
  is compared on all 3 channels (blog + facebook + tiktok).

## Changed

1. `services/acp_produce/judge_client.py`
   - `invoke_judge()` gained an optional `model: str | None = None` param, defaulting to
     `os.environ.get("JUDGE_MODEL", "nova_pro")` when not passed. `model == "gpt56"` routes to the
     new `invoke_judge_gpt56()`; anything else (including the default) is the original, byte-for-
     byte unchanged Nova Pro path.
   - New `invoke_judge_gpt56()` — GPT-5.6 Sol via the acc3 satellite, Converse API. Returns the
     same `{text, model_used, provider, input_tokens, output_tokens}` shape the Nova Pro path
     returns, so `gates.py`'s parsing code needed zero changes.
   - New constant `GPT56_SOL_INFERENCE_PROFILE`.
2. `tests/unit/test_aa351_judge_gpt56.py` (new) — 7 tests: default-stays-Nova-Pro, explicit
   `model=` routing (both directions), env var routing, `model=` overriding env var, Converse-not-
   invoke_model assertion, usage/text parsing, and a re-run of the existing "never imports
   generation/writer modules" structural isolation check (ADR-2026-014/027 L3) to confirm the new
   backend doesn't weaken it.
3. `docs/claude_tasks/AA-351-02-gpt56-judge-f8-f9.md` (new, gitignored `docs/` force-tracked per
   convention) — the task prompt, committed first per the Implementation Notes Pattern.

**Not changed**: `gates.py` (all 3 call sites), `pipeline.py`, `repair.py`, the F8/F9 rubric
content, the `_JUDGE_SYSTEM_PROMPT`, `FRAMEWORK_RUBRICS`, `BRAND_SEO_FAILURE_CODES`,
`GENERIC_AI_WORDING_ANCHOR` — nothing about what gets judged or how changed, only which model can
optionally do the judging.

## AWS actions taken (irreversible, real-money implications — logged for audit)

- **`aws bedrock create-foundation-model-agreement --model-id openai.gpt-5.6-sol` on acc3
  (`[acc3 id]`, profile `nghiep_aa365`) ONLY**, 16/08/2026. Verified scope via
  `list-foundation-model-agreement-offers --model-id openai.gpt-5.6-sol` on all 3 accounts
  immediately after: acc1/acc2 unaffected (offer still listed as un-accepted; no agreement created
  there). Terra/Luna were never accepted on any account.
- No change to acc2 (production) or acc1 in this session.

## Verify

### Harness validated (real Bedrock calls, Nova Pro side) — ✅ done

The comparison script (`aa351_compare.py`, run inside the ECS Dev container via the S3-mediated
exec pattern — daemonized per the `ecs-exec-long-sync-daemonize` pattern, since a foreground exec
reliably drops with `Cannot perform start session: EOF` partway through a multi-call run) re-judged
all 9 real pieces from the most recent completed N7 run (`88f094b1-3e0a-4b28-9abb-205cb7d21287`,
2026-07 W2 — the same run AA-382's own verify section used) through the unmodified
`gate_brand_seo_audit`/`gate_brand_seo_audit_social`/`gate_framework`, Nova Pro only
(`JUDGE_MODEL` forced to `nova_pro`, GPT-5.6 half skipped pending access — see below).

**Sanity check passed**: re-judging each piece's already-final `body_tagged` reproduced the
original pipeline's final status for all 9/9 pieces (8 held → F9 fail on re-judge, 1 passed → F9
pass on re-judge) — confirms the harness calls the real gate functions correctly and Nova Pro's
scoring is at least self-consistent against a fixed, already-repaired text (no repair loop
involved here, unlike AA-382's live multi-round case).

| piece | channel | orig status | F9 (re-judged) | F9 latency | F8 (re-judged) | F8 latency |
|---|---|---|---:|---:|---:|---:|
| slot_9afc9ee...:blog | blog | held | fail | 2.23s | — | — |
| slot_9afc9ee...:blog#facebook | facebook | held | fail | 1.56s | fail | 0.92s |
| slot_9afc9ee...:blog#tiktok | tiktok | held | fail | 2.03s | pass | 1.36s |
| slot_a5a7a000...:blog | blog | held | fail | 2.22s | — | — |
| slot_a5a7a000...:blog#facebook | facebook | held | fail | 1.18s | pass | 1.35s |
| slot_a5a7a000...:blog#tiktok | tiktok | held | fail | 1.45s | pass | 1.28s |
| slot_b6f66f8b...:blog | blog | held | fail | 3.12s | — | — |
| slot_b6f66f8b...:blog#facebook | facebook | **passed** | **pass** | 0.79s | pass | 1.44s |
| slot_b6f66f8b...:blog#tiktok | tiktok | held | fail | 1.14s | pass | 1.55s |

Nova Pro F9 (9 channels): 1/9 pass (11%) — matches AA-382's reported baseline for this exact run
exactly. Nova Pro F8 (facebook+tiktok subset, 6 channels): 5/6 pass (83%).

### GPT-5.6 side — ⚠️ BLOCKED, not yet run (~50 min of polling, gave up for this session)

`create-foundation-model-agreement` returned success immediately, but real `converse()` calls
against `us.openai.gpt-5.6-sol` on acc3 kept returning the exact same
`AccessDeniedException: openai.gpt-5.6-sol is not available for this account` the pre-accept
survey saw, across **~50 minutes of polling** (6 attempts @30s, 5 @90s, 10 @2min — final direct
check at the ~50 min mark still denied). Cross-checked `service-quotas list-service-quotas
--service-code bedrock` on acc3 partway through: zero GPT/OpenAI quotas provisioned even after
acceptance — the survey's own heuristic ("quota appears once access activates") says access has
**not** activated, despite the agreement call succeeding. Ruled out a client-side mistake: same
inference profile ARN the model catalog itself returns via `list-inference-profiles`, same
account, agreement confirmed accepted via `list-foundation-model-agreement-offers` (and confirmed
NOT accepted on acc1/acc2, so this isn't an account mix-up).

The error message itself now reads *"For additional access options, contact AWS Sales at
https://aws.amazon.com/contact-us/sales-support/"* — unchanged before/after accepting the
agreement, which suggests this specific denial reason is not simply "agreement not yet accepted"
(that case usually resolves once `create-foundation-model-agreement` succeeds) but something
requiring an AWS-side action beyond what any CLI call on this account can trigger. Not concluding
GPT-5.6 access is unavailable outright — this model's `startOfLifeTime` is 2026-08-13 (3 days old
at survey time, so onboarding automation may simply be immature) — but 50 minutes is well past
normal propagation delay for Bedrock model access, and I stopped polling rather than continue
indefinitely.

**Comparison against GPT-5.6 is not done and this report does NOT contain GPT-5.6 numbers.**
Filling those in requires either (a) more elapsed time and a retry, or (b) contacting AWS Sales
per the error message, then re-running `aa351_compare.py` (already uploaded to
`s3://aa-cis-bronze-{acc2-id}/scripts/aa351_compare.py`, already proven correct end-to-end
against Nova Pro) without `DRY_RUN=1`. See "Next steps" below.

### Recheck 16/08/2026 ~23:30 UTC+7 (task AA-351-04) — subscription confirmed ACTIVE in console, invoke STILL denied

Nghiep confirmed via email + the Bedrock console's "Manage Subscriptions" page that the GPT-5.6
Sol subscription (Amazon Bedrock Edition) shows **Active**, agreement
`agmt-c7xh4ar8elze4dp2pbq9z4hpj`, service start 2026-08-16 22:23 UTC+7 — i.e. the account-level
state AWS shows the human is different from what the runtime API enforces. Retried immediately
(~1h after the original acceptance, ~1h07m after the console-reported service start):

- `aws bedrock-runtime converse --model-id us.openai.gpt-5.6-sol --profile nghiep_aa365
  --region us-west-1` → still `AccessDeniedException: openai.gpt-5.6-sol is not available for
  this account. ... contact AWS Sales ...` — byte-identical message to every prior attempt.
- Also tried `global.openai.gpt-5.6-sol` (the inference-profile prefix this repo's own
  `invoke_judge_gpt56()` uses) — same denial.
- `list-foundation-model-agreement-offers --model-id openai.gpt-5.6-sol` on acc3 still lists the
  offer (`offer-gnqokrqqvdbgw`) as available to accept — this API does not distinguish
  accepted/active from not-yet-accepted (confirmed both before AND after acceptance across two
  sessions now), so it is **not a useful signal either way** for this specific denial; not worth
  querying again as evidence.
- `service-quotas list-service-quotas --service-code bedrock` on acc3: still 0 GPT/OpenAI quota
  entries.
- Identity re-verified: `sts get-caller-identity` → account `[acc3 id]` (acc3), user
  `nghiep_aa365_admin_acc3` — same account the subscription confirmation applies to, no mix-up.

**Evidence for an AWS Support ticket, if Nghiep wants to file one:**
- Agreement ID: `agmt-c7xh4ar8elze4dp2pbq9z4hpj`
- Account: `[acc3 id]` (acc3)
- Model: `openai.gpt-5.6-sol` (tried both `us.` and `global.` inference-profile prefixes)
- Request ID (most recent failed Converse call): `55efe945-3b44-408f-b234-79470e4fae15`
- Timestamp: 2026-08-16 16:30:27 UTC (2026-08-16 23:30:27 UTC+7)
- Error: `AccessDeniedException: openai.gpt-5.6-sol is not available for this account. You can
  explore other available models on Amazon Bedrock. For additional access options, contact AWS
  Sales at https://aws.amazon.com/contact-us/sales-support/`
- Symptom: console shows the subscription as Active (agreement ID above, service start
  2026-08-16 22:23 UTC+7); the Bedrock runtime API denies access with the exact same error text
  it gave before the subscription existed. This is either a longer-than-expected propagation gap
  between the Marketplace subscription system and Bedrock's runtime authorization, or a real
  provisioning bug on AWS's side for this specific (very new, 3-day-old at survey time) model.

**Not retrying further in this session** — per the task's own instruction, stopping here rather
than continuing to poll indefinitely. GPT-5.6 comparison data remains unavailable; Nova Pro data
above stands as the only real comparison point so far.

### Not done (blocked by the above)

1. GPT-5.6 pass rate for F8/F9 on the same 9 pieces.
2. `held_reason`/`flagged_phrases` consistency comparison (AA-382's core question — does GPT-5.6
   avoid Nova Pro's "flags a new phrase every round even after the old one was fixed" pattern).
3. Real cost-per-call comparison (Nova Pro's real $/call is known from AA-404's CloudWatch data —
   ~$0.80/1M in, $3.20/1M out, ≈$0.0017/call average; GPT-5.6 Sol global tier is $5/1M in, $30/1M
   out from the accepted rate card — roughly **6x** more expensive per call at the SAME token
   volume, before even measuring whether GPT-5.6 needs more or fewer input tokens for the same
   rubric).
4. Real latency comparison (Nova Pro's real numbers are in the table above: 0.8-3.1s/call).

## Should know

- No production behavior changed by this PR — `JUDGE_MODEL` is not set in the ECS task
  definition, so every gates.py call site still defaults to Nova Pro exactly as before.
- The judge_client.py file currently running inside the Dev ECS container was temporarily
  overwritten (via the S3-mediated exec pattern, downloading this branch's version directly onto
  `/app/services/acp_produce/judge_client.py`) to validate the harness against the real acc3 IAM
  chain without deploying an unmerged branch. This does **not** persist past the next real deploy
  (any push to `main` overwrites the container filesystem via a fresh image), and does not affect
  the live FastAPI process's already-imported modules (a separate `python3` process was used for
  the test, not a reload of the running server) — but it means the Dev container's on-disk
  `judge_client.py` will not exactly match `main`'s until the next deploy. Flagged here so nobody
  is surprised finding it during an unrelated Dev investigation before this PR merges.
- 21 pre-existing F8/F9 unit tests (`test_aa298_judge.py`) + 7 new (`test_aa351_judge_gpt56.py`) +
  full unit suite (1338 tests) all pass — 0 regressions.
- `bedrock:CreateFoundationModelAgreement`/`ListFoundationModelAgreementOffers` were run with the
  `nghiep_aa365` admin profile directly (not through the ECS task role) — this is a one-time,
  human-initiated account action, not something the app itself ever needs to call.

## Follow-up (16/08/2026, AA-351-03) — GPT-4.1 tried while GPT-5.6 stayed blocked

See `docs/implementation-notes/AA-351-gpt41-judge-trial.md` for the full writeup — short
version: `invoke_judge_gpt41()` was added (same feature-flag pattern, `JUDGE_MODEL=gpt41`),
code-complete and unit-tested, but the real comparison run also came back with 0 usable
data points — this time because the OpenAI API key has **zero billing credits**, a
finding with production impact beyond this trial (S1's brand-fit judge, `judge_node.py`,
is silently failing every call right now for the same reason). Judge comparison across
all 3 alternative backends (this file's Nova Pro baseline + GPT-5.6 + GPT-4.1) is still
just the 1 real data point — see that file's comparison table.

## Next steps (for Nghiep to decide — not done here)

1. **Retry access** — re-run `aws bedrock-runtime converse --model-id us.openai.gpt-5.6-sol
   --profile nghiep_aa365 --region us-west-1 ...` (or ask me to) after more time has passed. As of
   the 16/08/2026 ~23:30 UTC+7 recheck (task AA-351-04), the console-confirmed-Active subscription
   (agreement `agmt-c7xh4ar8elze4dp2pbq9z4hpj`) still does NOT unblock the runtime API — same
   denial, ~1h07m after the console's own reported service start. Given the console/runtime state
   mismatch, this now looks more like a genuine AWS-side provisioning gap than propagation delay.
   **Recommended next action: file an AWS Support case** using the evidence block in the "Recheck"
   section above (agreement ID, account, request ID, timestamp, exact error) rather than continuing
   to poll blind.
2. **Once access works**: re-run the already-uploaded `aa351_compare.py` on the ECS Dev container
   WITHOUT `DRY_RUN=1` (same S3-mediated exec + daemonize pattern documented above) — it will
   automatically re-judge the same 9 pieces with both Nova Pro and GPT-5.6 Sol and upload
   `scripts/aa351_compare_results.json`. I can pull and summarize that immediately once it exists.
3. **Do not change the production `JUDGE_MODEL` default** regardless of what the GPT-5.6 numbers
   show — that decision is explicitly reserved for Nghiep per the task's own constraint, and cost
   alone (GPT-5.6 Sol is ~6x Nova Pro's per-token rate even before accounting for any prompt-size
   difference) is a real factor to weigh against any quality improvement.
4. If Nghiep wants Terra or Luna tried instead/also, their agreements still need to be accepted
   separately (see Decisions above) — not done in this session.

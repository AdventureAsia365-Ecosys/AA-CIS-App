"""AA-917 — the writing-memory thresholds and caps (app ADR 0010, S245 decisions D9 / E4, cost-cuts #6).

One module so the miner (WM-12), the R0 replay filter (AA-918, "k equals the mining threshold") and the admin page
read the same numbers. Plain constants: changing one is a reviewed code change, like the decisions behind them.
"""
from __future__ import annotations

import os

# ── cluster thresholds (D9; counted in code, $0, before any model call) ────────────────────────────────────────────
TENANT_MIN_SIGNALS = 2           # a tenant rule: >= 2 edits ...
TENANT_MIN_PIECES = 2            # ... on >= 2 pieces of that tenant
PLATFORM_MIN_TENANTS = 3         # a platform rule drawn from tenant evidence: >= 3 distinct tenants
PLATFORM_MIN_REVIEWERS = 2       # A1 / platform-scope human edits: >= 2 distinct reviewers
A1_GATE_REPEAT_SHARE = 0.05      # A1 system gate failures: on >= 5% of the A1 pieces ...
A1_WINDOW_DAYS = 30              # ... of the last 30 days
MIN_SUPPORT = 2                  # no cluster below 2 signals on 2 pieces, whatever the scope
SIGNAL_WINDOW_DAYS = 90          # the miner reads pass signals of the last 90 days

# ── the run trigger (cost-cuts #6) ──────────────────────────────────────────────────────────────────────────────────
MINE_MIN_NEW_PASS = 10           # an automatic run needs >= 10 new pass signals since the last run
MINE_MAX_AUTO_RUNS_PER_MONTH = 2
MINE_MAX_RUNS_PER_MONTH = 3      # 2 automatic + the run before the monthly release close (manual)
# The automatic chain (daily fact filter -> writing_memory_mine) is OFF until Nghiep approves paid unattended runs
# (S250 review): with the flag unset the fact filter only reports the trigger state; a manual run from the admin page
# still works. Set WRITING_MEMORY_AUTO_MINE=true on the worker to switch it on.
AUTO_MINE_ENV = "WRITING_MEMORY_AUTO_MINE"

# ── the drafter (WM-12 scope 3) ─────────────────────────────────────────────────────────────────────────────────────
MINE_STAGE = "writing_memory_mine"
MINE_ROLE = "judge"
MINE_MAX_CALLS = 50              # LLM calls per run
MINE_CALL_ESTIMATE_USD = 0.02    # pre-call budget check (GPT-5.6 Luna: ~2k in / 300 out ~ $0.001; deliberately high)
DRAFT_MAX_EXAMPLES = 8           # examples shown to the drafter; the rest of the cluster is held out for PREDICT
PREDICT_MIN_EXPLAINED = 2        # a rule must explain >= 2 held-out edits of its cluster
MIN_CLUSTER_FOR_DRAFT = 1 + PREDICT_MIN_EXPLAINED   # 1 shown + 2 held out: smaller clusters wait (no paid call)
EXCERPT_CHARS = 400              # per side of an example (around the change)

# ── budget (E4) ─────────────────────────────────────────────────────────────────────────────────────────────────────
MONTH_CAP_USD = 25.0             # learning-loop LLM spend on Dev per calendar month (UTC)
RUN_CAP_USD = 1.0                # one mining run (also the spend_budget row of CIS 280)
# Every learning-loop stage counts against the month (mine now; eval / shadow with AA-919; the WM-13 one-off).
# AA-900: `adhoc_aa900` is the llm_call_log label of an R2 run started for a prompt / model / route change (the eval
# runbook, CIS docs/llm/eval-runbook.md); it counts against the same month cap.
MEMORY_STAGES = ("writing_memory_mine", "writing_memory_eval", "writing_memory_shadow", "adhoc_wm13", "adhoc_aa900")

# ── eval: the R2 runner (AA-919 WM-19) and the targeted shadow (WM-21) ─────────────────────────────────────────────
EVAL_STAGE = "writing_memory_eval"     # the R2 judge stage (CIS 291) and the llm_call_log label of every R2 call
EVAL_ROLE = "judge"
# AA-900: the llm_call_log labels an R2 run may write under. `writing_memory_eval` (default) for a release's evidence
# run from the Releases tab; `adhoc_aa900` for the eval of a prompt / model / route change PR (stage = adhoc_<issue>,
# handbook §5.7). Only the label changes: routes, caps and verdict rules are the same.
CHANGE_EVAL_STAGE = "adhoc_aa900"
EVAL_LOG_STAGES = (EVAL_STAGE, CHANGE_EVAL_STAGE)
SHADOW_STAGE = "writing_memory_shadow"  # the llm_call_log label of every shadow call (no judge, no route of its own)
R2_RUN_CAP_USD = 10.0            # E4 / D3: <= $10 per R2 run; a run may be given a LOWER cap, never a higher one
SHADOW_RUN_CAP_USD = 7.0         # qlint: allow a dollar cap (cost-cuts K3: T2 50 x $0.10 + 20 x $0.10), not a score
R2_PROBE_SIZE = 3                # WM-19 scope 1: a 3-item probe first
MIN_KAPPA = 0.6                  # WM-19 scope 4: the judge counts toward stops only at kappa >= 0.6 ...
MIN_KAPPA_PAIRS = 10             # ... measured on >= 10 labelled controls (fewer: informative only)
MIN_KAPPA_PER_LABEL = 3          # ... with >= 3 pass AND >= 3 fail labels among them (S255: a one-class label set
#                                  gives kappa 1.0 to a judge that never had to find a failure, or swings on one item)
NOISE_K = 1                      # quality-system design 4.2: one shared noise_k = 1 until a tier has >= 20 items
MAX_ERROR_SHARE = 0.2            # more than 20% of the attempted items in error: the run is invalid
# Pre-call reservation per item (the cap check reserves this before each item; actuals come from llm_call_log).
# AA-729 actuals: regenerate ~$0.05 a sample (full writer path); judge GPT-5.6 Luna ~ $0.002 a call. Deliberately high.
R2_ITEM_ESTIMATE_USD = {"a1": 0.15, "t2": 0.15, "t9_blog": 0.08, "t9_social": 0.05}
JUDGE_CALL_ESTIMATE_USD = 0.01
SHADOW_INPUT_ESTIMATE_USD = {"a1": 0.15, "t2": 0.15, "t9_blog": 0.06, "t9_social": 0.03}
# R2 always runs these tiers when the release touches S1 or T2 (WM-19 scope 7)
R2_FORCED_TIERS = {"a1": ("fact_dense", "long_9d"), "t2": ("fact_dense", "long_9d")}
# WM-21 scope 1 (cost-cuts K4 E2): the targeted shadow sizes
SHADOW_SIZE_DEFAULT = 20         # A1, T9 blog, LinkedIn (and the other social channels)
SHADOW_SIZE_WIDE = 50            # T2 and Facebook: the golden set cannot gate them (holdout T2 = 3 items, no Facebook)
SHADOW_SIZE_REPAIR_HINT = 10     # repair_hint-only release: <= 10 inputs where its gate fired
SHADOW_PROBE_INPUTS = 2          # a tenant release: + 1-2 inputs of another tenant (the isolation probe)
SHADOW_WINDOW_DAYS = 90          # "most recent" real inputs
# AA-920 WM-26: the post-activation check of an active release with content ($0, no model call)
POST_ACTIVATION_N = 20           # "pass" once this many writes carry the release and none violates
POST_ACTIVATION_SCAN_MAX = 200   # writes read per release per check (the newest since the activation)
POST_ACTIVATION_EVERY_S = 3600   # the api sampler queues one check job per hour while such a release is active

# ── the rule store (CIS 280; the DB enforces the same numbers) ─────────────────────────────────────────────────────
RULE_TTL_DAYS = 60
RULE_MAX_WORDS = 25
SUMMARY_MAX_WORDS = 125
RULE_MAX_CHARS = 1000
REPLACEMENT_MAX_CHARS = 200
MATCH_PHRASES_MAX_CHARS = 400   # all phrases together (char_length(array_to_string(match_phrases, '')))
SUMMARY_MAX_SENTENCES = 5
PLATFORM_TOKEN_CAP = 400
TENANT_TOKEN_CAP = 250
MAX_MATCH_PHRASES = 5


def auto_mine_enabled() -> bool:
    """True only when the worker env opts in (default off: no unattended paid run)."""
    return os.getenv(AUTO_MINE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


__all__ = [n for n in dir() if n.isupper()] + ["auto_mine_enabled"]

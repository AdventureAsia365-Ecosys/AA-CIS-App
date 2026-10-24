"""AA-917 — the writing-memory thresholds and caps (app ADR 0010, S245 decisions D9 / E4, cost-cuts #6).

One module so the miner (WM-12), the R0 replay filter (AA-918, "k equals the mining threshold") and the admin page
read the same numbers. Plain constants: changing one is a reviewed code change, like the decisions behind them.
"""
from __future__ import annotations

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
MEMORY_STAGES = ("writing_memory_mine", "writing_memory_eval", "writing_memory_shadow", "adhoc_wm13")

# ── the rule store (CIS 280; the DB enforces the same numbers) ─────────────────────────────────────────────────────
RULE_TTL_DAYS = 60
RULE_MAX_WORDS = 25
SUMMARY_MAX_SENTENCES = 5
PLATFORM_TOKEN_CAP = 400
TENANT_TOKEN_CAP = 250
MAX_MATCH_PHRASES = 5

__all__ = [n for n in dir() if n.isupper()]

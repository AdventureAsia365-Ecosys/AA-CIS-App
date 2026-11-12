"""AA-1051 (Quality W5, root ADR 0020 D12) — the $0 code of the small paid writer proofs.

  splits       the frozen DEV / CAL / GATE side of each golden item (CIS 400, shared.eval_golden_split)
  prereg       the pre-registration file of one proof (metrics, fixed sample, absolute margin, cap) and its lock
  power        the power check of a pre-registration from the R1 vote variance ($0)
  spend        the D12 caps and the spend gate (CONFIRM + a cap, read from the ledger)
  decision     the pre-registered verdict of a proof from its result rows
  cost_report  cost and requests per object, from shared.llm_call_log
  runner       the CLI: plan (DRY RUN), draft, run (queues the pinned R2 runs), report

No module here calls a model: a live proof queues the R2 runner (services/writing_memory/r2.py) under stage
adhoc_aa1051, whose writers and judge call the Model Gateway and write one ledger row per call.
"""

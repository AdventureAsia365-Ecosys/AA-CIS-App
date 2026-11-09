"""AA-1026 D5 — re-embedding a vector column into another embedding space (shared.embedding_space, CIS 328/329).

``sources`` says how to rebuild the text of each row of a registered vector column; ``reembed`` fills the column's
shadow (``<column>__shadow``) in batches through the gateway, resumable, with a cost cap and a dry run. The job kind
is ``services/jobs/embedding_reembed_job.py``; the operator steps (prepare, fill, swap, rollback, drop) are the root
runbook ``docs/ops/runbooks/embedding-re-embed.md``.
"""

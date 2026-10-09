"""AA-735 nac 3 — the declared stage registry for the platform recompute (ADR 0003 layer B + C).

`run_stages(names, scope, pool=, progress=)` runs a named list of declared stages; the two lists
`TOUR_STAGES` / `PLATFORM_STAGES` mirror the two hand-written chains in services/export/handler.py
this package replaces. See `stages.py` for the per-stage write-strategy declarations.
"""
from .stages import (  # noqa: F401
    PLATFORM_STAGES,
    TOUR_STAGES,
    VALID_WRITE_STRATEGIES,
    RecomputeContext,
    RecomputeScope,
    Stage,
    all_stages,
    get_stage,
    run_stages,
)

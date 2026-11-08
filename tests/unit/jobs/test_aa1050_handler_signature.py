"""AA-1050 review guard: every registered job kind's handler is ``async def <name>(ctx)``.

A helper once landed between ``@job_kind(...)`` and ``async def run(ctx)``, so the decorator registered the helper
(``(pool, **kw)``) as the T2 handler: every job "succeeded" doing nothing. A handler takes exactly one positional
parameter (the JobContext), no ``*args`` / ``**kw``, and keyword-only parameters only with a default (test
injection, e.g. ``s3=None``); the check runs over every loaded kind.
"""
import inspect

from shared.jobs.registry import kinds
from shared.jobs.worker import load_kinds


def test_every_registered_handler_takes_only_the_job_context():
    load_kinds()
    bad = {}
    for name, kind in kinds().items():
        fn = kind.handler
        params = list(inspect.signature(fn).parameters.values())
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        extra = [p for p in params if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
                 or (p.kind == p.KEYWORD_ONLY and p.default is p.empty)]
        if not inspect.iscoroutinefunction(fn) or len(positional) != 1 or extra:
            bad[name] = f"{fn.__module__}.{fn.__qualname__}{inspect.signature(fn)}"
    assert not bad, f"job kinds whose handler is not `async def handler(ctx)`: {bad}"

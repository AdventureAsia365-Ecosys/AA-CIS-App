"""AA-1002 (gateway slice S6) — the cost ``pricing.calc_cost`` gives for the shapes ``shared.llm_call_log`` records.

Shapes: every ``model`` label the gateway writes (catalog keys of the Dev catalog of 29/10 in
``snapshots/aa1002_s3_routes_fixture.json``, the legacy Bedrock ids and short keys, the satellite / Anthropic API
labels, an unknown model) times the token shapes of the log (plain, cached prefix read, cache write, a prompt above
100K tokens, zero), each with the catalog loaded and with the catalog unreachable (COST_TABLE / Sonnet fallback).

The golden ``snapshots/aa1002_s6_pre_s6_costs.json`` was written by running THIS file against the pre-S6 checkout
(branch feat/aa-1002-gateway-s5-scopes, pricing.py unchanged since AA-1002 S2)::

    python -I tests/unit/aa1002_s6_cost_capture.py <checkout root> > tests/unit/snapshots/aa1002_s6_pre_s6_costs.json

``test_aa1002_s6_pricing.py`` checks that S6's ``calc_cost`` (now a wrapper over ``pricing.quote``) gives the same
number for every shape. Imports nothing S6 added.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "snapshots" / "aa1002_s3_routes_fixture.json"

LABELS_EXTRA = (
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet-4-6", "haiku-4-5", "satellite-sonnet-4-6", "satellite-haiku-4-5", "satellite-gpt-5.6-luna",
    "anthropic-api-sonnet-5-5",
    "anthropic-api-haiku-5-5",
    "gpt-4.1", "mystery-model-x",
)
# (tokens_in uncached, tokens_out, cache_read, cache_write)
TOKEN_SHAPES = (
    (1200, 350, 0, 0), (40, 600, 5200, 0), (300, 120, 0, 4800), (130_000, 900, 0, 0), (60_000, 400, 50_000, 0),
    (0, 0, 0, 0), (7, 1, 3, 2),
)


def shapes(fixture: dict) -> list[tuple]:
    labels = sorted({m["model_key"] for m in fixture["models"]} | set(LABELS_EXTRA))
    return [(label, *t) for label in labels for t in TOKEN_SHAPES]


def capture(calc_cost, set_catalog) -> dict:
    """{"catalog": {"label|in|out|cr|cw": cost}, "no_catalog": {...}} with ``calc_cost(model, in, out, cr, cw)``."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    out: dict = {}
    for state in ("catalog", "no_catalog"):
        set_catalog(fixture if state == "catalog" else None)
        out[state] = {"|".join(map(str, s)): calc_cost(s[0], s[1], s[2], cache_read=s[3], cache_write=s[4])
                      for s in shapes(fixture)}
    set_catalog(None)
    return out


def catalog_setter():
    """Loads the fixture models into the in-process catalog cache (or empties it: the 'unreachable' state)."""
    from shared.llm_client import catalog
    from shared.llm_client.catalog import CatalogModel

    def set_catalog(fixture):
        catalog.invalidate()
        if fixture is None:
            catalog._models = {}
            catalog._loaded_at, catalog._failed_at = 0.0, time.monotonic()      # a failed load: no catalog
            return
        catalog._models = {m["model_key"]: CatalogModel(**{**m, "callable_via": tuple(m["callable_via"])})
                           for m in fixture["models"]}
        catalog._loaded_at, catalog._failed_at = time.monotonic(), 0.0
    return set_catalog


if __name__ == "__main__":
    import structlog

    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret-ci-only-not-real")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
    from shared.llm_client.pricing import calc_cost
    print(json.dumps(capture(calc_cost, catalog_setter()), indent=1, sort_keys=True))

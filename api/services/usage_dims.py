"""AA-1045 — the dimensions of one paid call, as SQL over each CIS ledger: ONE definition for the hourly usage rollup
(``shared.ai_usage_hourly``, CIS 372), the explorer's raw per-tour read and the Calls tab's drill-down filters, so a
cell of the explorer and the Calls list it opens count the same rows. Pure: no database, no I/O.

Ledgers (``source``) and what one dimension means in each (an expression of that ledger's own columns):

  dimension    llm (shared.llm_call_log)           jev (decision_log)   dfs (dfs_call_log)   geocoder (geocode_call)
  app          coalesce(app, quality_signal.app,   'cis'                coalesce(app, 'cis') coalesce(app, 'cis')
               'cis')
  provider     the spend-budget provider:          'jev'                'dfs'                'geocoder'
               bedrock* -> bedrock, else lower()
  account      coalesce(connection_key, account)   'typesafe'           'dataforseo'         provider (geoapify, …)
  model        model                               model                endpoint             endpoint
  stage        stage                               stage                stage                stage
  role         role                                'decide'             'data'               'data'
  tenant       tenant_id                           tenant_id            tenant_id            tenant_id
  status       coalesce(status, 'ok')              JEV_STATUS_SQL       coalesce(status,'ok') status, else derived
  error_class  error_class                         NULL                 NULL                 NULL
  traffic      experiment | shadow | live          live                 live                 live

Jev's own ``typesafe`` rows of the LLM log are left out of ``llm`` (Jev is the ``jev`` source, as in the Calls tab).
Derived at read time (never stored): ``family`` (the model catalog's family of ``model``) and ``product``
(``product_sql``: the stage's product, api/services/llm_route_chains.product_of; ``crm`` for a ``crm_*`` stage).

Fallback (``FALLBACK_SQL``): an answered LLM row (``route_watch.OK``) on a fallback rung (``route_watch.FALLBACK_RUNG``,
the one rung rule of the AI gateway route trace, AA-1042: ``fallback_used``, ``attempt_no`` > 1 or a gateway route
position > 0). Imported, never copied, so the explorer's fallback rate and the route alerts count the same rows.

Latency histogram: ``LATENCY_BOUNDS_MS`` (19 bounds, 20 buckets): bucket 0 is [0, 100) ms, bucket k [bound k-1,
bound k), bucket 19 [300 000 ms, ∞). ``width_bucket(latency_ms, bounds)`` gives the bucket.
"""
from __future__ import annotations

from typing import Optional

from shared.llm_client import route_watch

SOURCES: tuple[str, ...] = ("llm", "jev", "dfs", "geocoder")
TRAFFIC: tuple[str, ...] = ("live", "shadow", "experiment")
STATUSES: tuple[str, ...] = ("ok", "refused", "error", "billed_unusable", "truncated")

TABLE = {"llm": "shared.llm_call_log", "jev": "shared.decision_log", "dfs": "shared.dfs_call_log",
         "geocoder": "geo.geocode_call"}
TIME = {"llm": "created_at", "jev": "created_at", "dfs": "created_at", "geocoder": "called_at"}
# Rows of the ledger that belong to the source (Jev's typesafe rows of the LLM log are the jev source's).
SOURCE_WHERE = {"llm": "coalesce(provider, '') <> 'typesafe'", "jev": "true", "dfs": "true", "geocoder": "true"}

# api/services/ai_gateway.py JEV_STATUS_SQL (kept in step by tests/unit/test_aa1045_usage_dims.py).
_JEV_STATUS = None


def _jev_status() -> str:
    global _JEV_STATUS
    if _JEV_STATUS is None:
        from api.services.ai_gateway import JEV_STATUS_SQL
        _JEV_STATUS = JEV_STATUS_SQL
    return _JEV_STATUS


FALLBACK_SQL = f"({route_watch.OK} AND {route_watch.FALLBACK_RUNG})"

LATENCY_BOUNDS_MS: tuple[int, ...] = (100, 250, 500, 1000, 1500, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000,
                                      45000, 60000, 90000, 120000, 180000, 300000)
HIST_BUCKETS = len(LATENCY_BOUNDS_MS) + 1   # 20, the CHECK of CIS 372

# The stored dimensions (the rollup's key columns after hour and source), in the unique key's order.
STORED_DIMS: tuple[str, ...] = ("app", "provider", "account", "model", "stage", "role", "tenant", "status",
                                "error_class", "traffic")
# Every dimension the explorer groups and filters by (stored, plus source and the two derived ones).
DIMENSIONS: tuple[str, ...] = ("source", "app", "product", "provider", "account", "model", "family", "stage", "role",
                               "tenant", "status", "error_class", "traffic")
# The rollup column of a stored dimension.
ROLLUP_COLUMN = {"source": "source", "app": "app", "provider": "provider", "account": "account", "model": "model",
                 "stage": "stage", "role": "role", "tenant": "tenant_id", "status": "status",
                 "error_class": "error_class", "traffic": "traffic"}


def dim_sql(source: str, dim: str) -> Optional[str]:
    """The SQL of a stored dimension (or ``source``) over one ledger's columns; None when the ledger has no such value
    (a filter on it leaves that ledger out). ``tenant`` is text."""
    if dim == "source":
        return f"'{source}'"
    if source == "llm":
        return {
            "app": "coalesce(app, quality_signal ->> 'app', 'cis')",
            "provider": ("CASE WHEN lower(coalesce(provider, '')) LIKE 'bedrock%' THEN 'bedrock' "
                         "ELSE coalesce(nullif(lower(provider), ''), 'unknown') END"),
            "account": "coalesce(connection_key, account)",
            "model": "model", "stage": "stage", "role": "role", "tenant": "tenant_id::text",
            "status": "coalesce(status, 'ok')", "error_class": "error_class",
            "traffic": ("CASE WHEN coalesce(is_experiment, false) THEN 'experiment' "
                        "WHEN coalesce(is_shadow, quality_signal @> '{\"shadow\": true}') THEN 'shadow' "
                        "ELSE 'live' END"),
        }.get(dim)
    if source == "jev":
        return {"app": "'cis'", "provider": "'jev'", "account": "'typesafe'", "model": "model", "stage": "stage",
                "role": "'decide'", "tenant": "tenant_id::text", "status": _jev_status(), "error_class": None,
                "traffic": "'live'"}.get(dim)
    if source == "dfs":
        return {"app": "coalesce(app, 'cis')", "provider": "'dfs'", "account": "'dataforseo'", "model": "endpoint",
                "stage": "stage", "role": "'data'", "tenant": "tenant_id::text", "status": "coalesce(status, 'ok')",
                "error_class": None, "traffic": "'live'"}.get(dim)
    if source == "geocoder":
        return {"app": "coalesce(app, 'cis')", "provider": "'geocoder'", "account": "provider", "model": "endpoint",
                "stage": "stage", "role": "'data'", "tenant": "tenant_id::text",
                "status": ("coalesce(status, CASE WHEN coalesce(failed_requests, 0) > 0 "
                           "AND failed_requests >= requests THEN 'error' ELSE 'ok' END)"),
                "error_class": None, "traffic": "'live'"}.get(dim)
    raise ValueError(f"unknown source {source!r}")


def metric_sql(source: str) -> dict[str, str]:
    """The per-row metric expressions of one ledger (summed by the rollup)."""
    zero = "0"
    if source == "llm":
        return {"requests": "1", "fallback": f"({FALLBACK_SQL})::int", "tokens_in": "coalesce(tokens_in, 0)",
                "tokens_out": "coalesce(tokens_out, 0)", "cache_read_tokens": "coalesce(cache_read_tokens, 0)",
                "cache_write_tokens": "coalesce(cache_write_tokens, 0)",
                "reasoning_tokens": "coalesce(reasoning_tokens, 0)", "cost_usd": "coalesce(cost_usd, 0)",
                "latency_ms": "latency_ms"}
    requests = "coalesce(requests, 0)" if source == "geocoder" else "1"
    return {"requests": requests, "fallback": zero, "tokens_in": zero, "tokens_out": zero,
            "cache_read_tokens": zero, "cache_write_tokens": zero, "reasoning_tokens": zero,
            "cost_usd": "coalesce(cost_usd, 0)", "latency_ms": "latency_ms"}


def _product_rules() -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    from api.services import llm_route_chains as rc
    return dict(rc._PRODUCT_BY_STAGE), tuple(rc._PRODUCT_BY_PREFIX)


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def product_sql(stage_expr: str) -> str:
    """CASE over a stage expression -> its product (llm_route_chains.product_of), ``crm`` for a ``crm_*`` stage,
    ``other`` for a stage no rule knows (and no stage)."""
    exact, prefixes = _product_rules()
    parts = [f"WHEN {stage_expr} = {_lit(s)} THEN {_lit(p)}" for s, p in sorted(exact.items())]
    parts.append(f"WHEN {stage_expr} LIKE 'crm\\_%' THEN 'crm'")
    parts += [f"WHEN {stage_expr} LIKE {_lit(pre.replace('_', chr(92) + '_') + '%')} THEN {_lit(p)}"
              for pre, p in prefixes]
    return "(CASE " + " ".join(parts) + " ELSE 'other' END)"


def product_of(stage: Optional[str]) -> str:
    """The Python twin of product_sql (CRM rows, tests)."""
    if not stage:
        return "other"
    exact, prefixes = _product_rules()
    if stage in exact:
        return exact[stage]
    if stage.startswith("crm_"):
        return "crm"
    return next((p for pre, p in prefixes if stage.startswith(pre)), "other")


def hist_sql(latency_expr: str = "latency_ms") -> str:
    """ARRAY[count per bucket] of the group's rows with a latency (integer[20])."""
    bounds = "ARRAY[" + ", ".join(str(b) for b in LATENCY_BOUNDS_MS) + "]::int[]"
    wb = f"width_bucket({latency_expr}, {bounds})"
    cells = [f"count(*) FILTER (WHERE {latency_expr} IS NOT NULL AND {wb} = {k})::int" for k in range(HIST_BUCKETS)]
    return "ARRAY[" + ", ".join(cells) + "]::int[]"


def percentile_ms(hist: list[int], q: float = 0.95) -> Optional[int]:
    """The q-quantile (approximate, linear inside its bucket) of a latency histogram; None when it is empty. The last
    bucket has no upper bound: its lower bound is returned."""
    total = sum(int(c or 0) for c in hist)
    if total <= 0:
        return None
    target = q * total
    cum = 0.0
    for k, c in enumerate(hist):
        c = int(c or 0)
        if c and cum + c >= target:
            lo = 0 if k == 0 else LATENCY_BOUNDS_MS[k - 1]
            if k >= len(LATENCY_BOUNDS_MS):
                return lo
            hi = LATENCY_BOUNDS_MS[k]
            return int(round(lo + (hi - lo) * (target - cum) / c))
        cum += c
    return LATENCY_BOUNDS_MS[-1]


def merge_hist(a: Optional[list[int]], b: Optional[list[int]]) -> list[int]:
    out = [0] * HIST_BUCKETS
    for h in (a, b):
        for k, c in enumerate(h or []):
            if k < HIST_BUCKETS:
                out[k] += int(c or 0)
    return out

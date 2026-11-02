"""AA-518/AA-505 — shared Bedrock/OpenAI pricing table, extracted out of client.py so both
LLMClient (Mechanism A) and the Mechanism-B call sites that invoke shared/llm_client/
bedrock_satellite.py::invoke_claude() directly (T5 atomize, N7 E2-E5, s1_from_atom.py, DFS gap
research) can compute a real cost_usd for shared.llm_call_log without duplicating the table or
importing client.py (which would be a heavier/circular-risking import for those modules).

Values themselves are UNCHANGED from client.py's own COST_TABLE — this is a pure relocation, not
a repricing. client.py re-exports the same names so nothing importing from client.py breaks.

AA-658: prices now come from shared.llm_model_catalog first; COST_TABLE is the fallback.

AA-1002 (gateway slice S6, cost parity): ``quote(model, usage, deployment=...)`` prices one billed request from its
full usage — uncached input, output, cache reads, 5-minute and 1-hour cache writes — on the deployment it ran on
(``shared.llm_model_deployment`` price overrides first), with the Message Batches discount, the long-prompt tier of
Claude Haiku 5.5 and the US-only inference premium of the Anthropic API. It returns the cost AND how it was priced
(``Quote.pricing_mode``: ``override`` / ``catalog`` / ``code_table`` / ``estimate``, ``+batch``, ``+geo_us``), so a row
priced by guesswork is flagged in ``shared.llm_call_log.pricing_mode`` instead of looking real. ``calc_cost`` keeps
its signature and, for every shape the log records, its numbers (``tests/unit/test_aa1002_s6_pricing.py`` against the
pre-S6 golden), except the documented corrections (the Haiku 5.5 tier above 100K prompt tokens).

Rates and multipliers (Anthropic, from the claude-api skill's pricing + prompt-caching references, 30/10/2026): cache
read 0.1x the input rate unless the catalog row states it (Opus 5.5 0.05x, Fable 5.1 0.025x); cache write 1.25x
(5-minute TTL) or 2x (1-hour TTL); Message Batches 50% off every token, cache tokens included; ``inference_geo:
"us"`` 1.1x; Claude Haiku 5.5 $0.10 / $0.50 per MTok up to 100K prompt tokens, $0.50 / $2.50 above. Bedrock and
OpenAI rates are the catalog rows (``shared.llm_model_catalog``) and the deployment overrides.

AA-1024 A10 (root ADR 0009 decision 13): the arithmetic, the tables and ``Usage`` / ``Quote`` are the shared core's
(``shared/aa_llm/pricing.py``, the module TripPlanner and the CRM vendor). This module adds what only CIS does: it
reads the catalog row itself (``_catalog_rates``), logs an estimate, finds a logged row's deployment.
"""
from typing import Optional

import structlog

from shared.aa_llm import pricing as core

logger = structlog.get_logger()

# AA-1024 A10: the core's own objects (tests/unit/test_aa1024_a10_core_identity.py).
#
# AA-348 — BEDROCK_SONNET names the acc2-NATIVE (T1) Sonnet model only; a satellite call runs a newer Sonnet
# (bedrock_satellite.py). The constant is a TIER SELECTOR / COST_TABLE key, never a claim about the literal model string
# that tier invokes. AA-635 — Haiku 4.5 is $1/$5 per MTok; cache reads 0.1x, 5-minute writes 1.25x the input rate.
# Mechanism-B callers (invoke_claude) pass the short key ("sonnet"/"haiku") or a satellite label: _SHORT_KEY_TO_MODEL_ID
# maps them to the same COST_TABLE entry.
BEDROCK_SONNET = core.BEDROCK_SONNET
BEDROCK_HAIKU = core.BEDROCK_HAIKU
COST_TABLE = core.COST_TABLE                       # $ per 1K tokens, {"in": ..., "out": ...}
CACHE_WRITE_MULTIPLIER = core.CACHE_WRITE_MULTIPLIER
CACHE_READ_MULTIPLIER = core.CACHE_READ_MULTIPLIER
CACHE_WRITE_1H_MULTIPLIER = core.CACHE_WRITE_1H_MULTIPLIER
BATCH_DISCOUNT = core.BATCH_DISCOUNT
GEO_MULTIPLIERS = core.GEO_MULTIPLIERS
LONG_PROMPT_TIERS = core.LONG_PROMPT_TIERS
ESTIMATE_RATES = core.ESTIMATE_RATES
PRICING_BASES = core.PRICING_BASES
PRICING_MODE_RE = core.PRICING_MODE_RE              # = migration 312's CHECK
_SHORT_KEY_TO_MODEL_ID = core._SHORT_KEY_TO_MODEL_ID
_MODEL_ID_TO_CATALOG_KEY = core._MODEL_ID_TO_CATALOG_KEY
Usage = core.Usage
Quote = core.Quote


def _catalog_rates(model: str) -> Optional[dict]:
    """AA-658 — $/1K rates from shared.llm_model_catalog, or None when the catalog is
    unreachable or has no price for this model."""
    from .catalog import get_model_sync
    try:
        m = get_model_sync(_catalog_key(model))
    except Exception:
        return None
    if m is None:
        return None
    return core.catalog_rates(m.price_in_per_mtok, m.price_out_per_mtok, m.price_cache_read_per_mtok,
                              m.price_cache_write_per_mtok)


def _catalog_key(model: str) -> str:
    """The catalog key behind a gateway label (satellite / vendor prefixes and the legacy ids removed): the core's."""
    return core.catalog_key(model)


def _base_rates(model: str) -> tuple[dict, str]:
    """($/1K rates, basis) from the catalog, else COST_TABLE, else the flagged estimate. Never raises."""
    return core.base_rates(model, _catalog_rates(model))


def _apply_override(rates: dict, deployment) -> bool:
    """Deployment price overrides ($ per MTok, migration 304) replace the matching rate. True when any applied."""
    return core.apply_override(rates, deployment)


def _warn_estimate(model: str) -> None:
    logger.warning("llm_price_unknown_model", model=model, hint="priced at Sonnet rates, row flagged estimate")


def quote(model: str, usage: Usage, deployment=None) -> Quote:
    """AA-1002 (S6) — the cost of one billed request of ``model`` (a catalog key or any label ``calc_cost`` accepts)
    with ``usage``, on ``deployment`` (a ``deployments.ModelDeployment`` or None). Never raises: pricing must never be
    the reason a call fails; a model no source can price is charged ``ESTIMATE_RATES``, warned and flagged
    ``estimate``. AA-1024 A10: the core's ``quote`` with the catalog row this module read."""
    return core.quote(model, usage, deployment, catalog=_catalog_rates(model), on_estimate=_warn_estimate)


def deployment_for(model: str, connection_key: Optional[str]):
    """The ``shared.llm_model_deployment`` row of ``model`` on ``connection_key`` (None when unknown or the registry
    is unreadable). Never raises."""
    if not connection_key:
        return None
    try:
        from . import deployments
        key = _catalog_key(model)
        for d in deployments.deployments_for_sync(key, enabled_only=False):
            if d.connection_key == connection_key:
                return d
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_price_deployment_lookup_failed", model=model, connection=connection_key,
                       error=str(e)[:200])
    return None


def pricing_mode_for(model: str, connection_key: Optional[str] = None) -> Optional[str]:
    """How a logged row of ``model`` (served on ``connection_key``, the log's ``account``) is priced, for rows whose
    caller did not say (``call_log``): the basis only (batch / geo are known only to the call). Never raises."""
    try:
        _, basis = _base_rates(model)
        if basis != "estimate" and _apply_override({"in": 0, "out": 0}, deployment_for(model, connection_key)):
            basis = "override"
        return basis
    except Exception:  # noqa: BLE001
        return None


def calc_cost(model: str, in_tok: int, out_tok: int,
              cache_read: int = 0, cache_write: int = 0) -> float:
    """`model` accepts a Model Key ("sonnet-5"), a full Bedrock model id, a short key
    ("sonnet"/"haiku"), a bedrock_satellite `model_used` label, or "gpt-4.1".

    AA-658 — the catalog (shared.llm_model_catalog) is the price source; COST_TABLE is the
    fallback when the catalog is unreachable. A model known to neither is priced at Sonnet rates
    (pricing must never be the reason an LLM call fails) and logged as a warning.

    `cache_read`/`cache_write` (AA-635) are Anthropic's `cache_read_input_tokens` /
    `cache_creation_input_tokens`. Default 0 keeps every existing caller's result unchanged.

    AA-1002 (S6): a wrapper over ``quote`` (5-minute writes, no deployment, no batch / geo). Callers that know the
    deployment, the 1-hour writes, batch or geo use ``quote`` directly."""
    return quote(model, Usage(input_tokens=in_tok or 0, output_tokens=out_tok or 0, cache_read_tokens=cache_read or 0,
                              cache_write_tokens=cache_write or 0)).cost_usd

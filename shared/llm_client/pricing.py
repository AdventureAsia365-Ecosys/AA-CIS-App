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
"""
from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger()

# Bedrock model IDs (cross-region inference profiles)
# AA-348 — BEDROCK_SONNET names the acc2-NATIVE (T1) Sonnet model only. When a call falls
# through to the satellite account (T1.5a, shared/llm_client/bedrock_satellite.py — the
# common path in practice, native Sonnet is blocked for channel-program accounts, AA-291/AA-329),
# the model actually invoked is a DIFFERENT, newer version: `global.anthropic.claude-sonnet-4-6`
# (see bedrock_satellite.py's _PROFILE_ID_SONNET / the deployment sonnet@acc3). This is a
# deliberate, live-verified difference between the two tiers (bedrock_satellite.py's own
# 16/07/2026 STATUS note), NOT a copy-paste bug — investigated and confirmed still the real state
# under AA-348. client.py still passes this constant into the satellite call path, but only as a
# TIER SELECTOR / COST_TABLE key, never as a claim about the literal model string that tier
# invokes — check bedrock_satellite.py directly if you need the real satellite model version.
BEDROCK_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# $ per 1K tokens, {"in": ..., "out": ...}
# AA-635 — Haiku was {"in": 0.00025, "out": 0.00125}, i.e. Claude *3* Haiku's $0.25/$1.25 per MTok.
# The model actually invoked everywhere is Claude Haiku 4.5 ($1/$5 per MTok), so every Haiku call
# was logged at 25% of what AWS bills. Sonnet 4.5 (acc2-native) and Sonnet 4.6 (satellite) share
# $3/$15, so the one Sonnet entry prices both tiers correctly.
COST_TABLE = {
    BEDROCK_SONNET: {"in": 0.003, "out": 0.015},
    BEDROCK_HAIKU: {"in": 0.001, "out": 0.005},
    "gpt-4.1": {"in": 0.002, "out": 0.008},
}

# AA-635 — Anthropic prompt-cache pricing, as multiples of the model's base input rate. Anthropic
# usage reports cache tokens SEPARATELY from `input_tokens` (which excludes them), so a caller that
# prices only input/output tokens misses every cache write (billed at 1.25x) and read (0.1x).
# 1.25x is the 5-minute-TTL write rate — the only TTL prompt_cache.py sets.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

# Mechanism-B callers (invoke_claude) pass the short model key ("sonnet"/"haiku"), not the full
# Bedrock model id — this maps that key to the same COST_TABLE entry Mechanism A uses, so both
# mechanisms price identically instead of drifting. AA-635: also maps the `model_used` labels
# bedrock_satellite.py returns ("haiku-4-5"/"sonnet-4-6", optionally "satellite-"-prefixed), which
# some log call sites pass straight through — those used to miss the table and fall back to Sonnet.
_SHORT_KEY_TO_MODEL_ID = {
    "sonnet": BEDROCK_SONNET, "haiku": BEDROCK_HAIKU,
    "sonnet-4-6": BEDROCK_SONNET, "haiku-4-5": BEDROCK_HAIKU,
    "satellite-sonnet-4-6": BEDROCK_SONNET, "satellite-haiku-4-5": BEDROCK_HAIKU,
}


_MODEL_ID_TO_CATALOG_KEY = {
    BEDROCK_SONNET: "sonnet", BEDROCK_HAIKU: "haiku",
    "sonnet-4-6": "sonnet", "haiku-4-5": "haiku",
}


def _catalog_rates(model: str) -> Optional[dict]:
    """AA-658 — $/1K rates from shared.llm_model_catalog, or None when the catalog is
    unreachable or has no price for this model."""
    from .catalog import get_model_sync
    from .models import VENDOR_LABEL_PREFIXES
    key = model[len("satellite-"):] if model and model.startswith("satellite-") else model
    for prefix in VENDOR_LABEL_PREFIXES:            # AA-1002 S2 / S9: the Anthropic API / Azure OpenAI path labels
        if key and key.startswith(prefix):
            key = key[len(prefix):]
    key = _MODEL_ID_TO_CATALOG_KEY.get(key, key)
    try:
        m = get_model_sync(key)
    except Exception:
        return None
    if m is None or m.price_in_per_mtok is None or m.price_out_per_mtok is None:
        return None
    rate_in = m.price_in_per_mtok / 1000
    return {
        "in": rate_in,
        "out": m.price_out_per_mtok / 1000,
        "cache_read": (m.price_cache_read_per_mtok / 1000 if m.price_cache_read_per_mtok is not None
                       else rate_in * CACHE_READ_MULTIPLIER),
        "cache_write": (m.price_cache_write_per_mtok / 1000 if m.price_cache_write_per_mtok is not None
                        else rate_in * CACHE_WRITE_MULTIPLIER),
    }


# ── AA-1002 (S6) — one priced request ────────────────────────────────────────────────────────────────────────────

CACHE_WRITE_1H_MULTIPLIER = 2.0         # 1-hour TTL writes (Anthropic: 2x the input rate)
BATCH_DISCOUNT = 0.5                    # Message Batches / Bedrock batch: 50% off every token, cache tokens included
GEO_MULTIPLIERS = {"us": 1.1}           # Anthropic API `inference_geo`; "global" / None = 1.0
# Long-prompt tiers: catalog key -> (prompt tokens above which the tier applies, multiplier on every rate). Claude
# Haiku 5.5: $0.10 / $0.50 -> $0.50 / $2.50 per MTok above 100K prompt tokens (input + cache read + cache write).
LONG_PROMPT_TIERS: dict[str, tuple[int, float]] = {"haiku-5-5": (100_000, 5.0)}
# The rates a model nobody can price is charged at (Sonnet 4.x list): high on purpose, never 0, always flagged.
ESTIMATE_RATES = {"in": 0.003, "out": 0.015}

PRICING_BASES = ("override", "catalog", "code_table", "estimate")
PRICING_MODE_RE = r"^(override|catalog|code_table|estimate)(\+batch)?(\+geo_us)?$"   # = migration 312's CHECK


@dataclass(frozen=True)
class Usage:
    """One billed request. ``input_tokens`` is the UNCACHED input (Anthropic / Converse convention); cache reads and
    writes are separate. ``cache_write_1h_tokens`` is the part of the writes made with the 1-hour TTL (the rest of
    ``cache_write_tokens`` is 5-minute)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    batch: bool = False
    inference_geo: Optional[str] = None

    @property
    def prompt_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)


@dataclass(frozen=True)
class Quote:
    cost_usd: float
    pricing_mode: str
    rates: dict            # $ per 1K tokens actually applied: in, out, cache_read, cache_write, cache_write_1h

    @property
    def estimated(self) -> bool:
        return self.pricing_mode.startswith("estimate")


def _catalog_key(model: str) -> str:
    from .models import VENDOR_LABEL_PREFIXES
    key = model[len("satellite-"):] if model and model.startswith("satellite-") else (model or "")
    for prefix in VENDOR_LABEL_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
    return _MODEL_ID_TO_CATALOG_KEY.get(key, key)


def _base_rates(model: str) -> tuple[dict, str]:
    """($/1K rates, basis) from the catalog, else COST_TABLE, else the flagged estimate. Never raises."""
    rates = _catalog_rates(model)
    if rates is not None:
        return dict(rates), "catalog"
    resolved = _SHORT_KEY_TO_MODEL_ID.get(model, model)
    base = COST_TABLE.get(resolved)
    basis = "code_table"
    if base is None:
        base, basis = ESTIMATE_RATES, "estimate"
    return {"in": base["in"], "out": base["out"], "cache_read": base["in"] * CACHE_READ_MULTIPLIER,
            "cache_write": base["in"] * CACHE_WRITE_MULTIPLIER}, basis


def _apply_override(rates: dict, deployment) -> bool:
    """Deployment price overrides ($ per MTok, migration 304) replace the matching rate. True when any applied."""
    if deployment is None:
        return False
    applied = False
    for field, key in (("price_in_override", "in"), ("price_out_override", "out"),
                       ("price_cache_read_override", "cache_read"), ("price_cache_write_override", "cache_write"),
                       ("price_cache_write_1h_override", "cache_write_1h")):
        value = getattr(deployment, field, None)
        if value is not None:
            rates[key] = float(value) / 1000
            applied = True
    return applied


def quote(model: str, usage: Usage, deployment=None) -> Quote:
    """AA-1002 (S6) — the cost of one billed request of ``model`` (a catalog key or any label ``calc_cost`` accepts)
    with ``usage``, on ``deployment`` (a ``deployments.ModelDeployment`` or None). Never raises: pricing must never be
    the reason a call fails; a model no source can price is charged ``ESTIMATE_RATES``, warned and flagged
    ``estimate``."""
    rates, basis = _base_rates(model)
    if _apply_override(rates, deployment):
        basis = "override"
    if basis == "estimate":
        logger.warning("llm_price_unknown_model", model=model, hint="priced at Sonnet rates, row flagged estimate")
    if rates.get("cache_write_1h") is None:
        rates["cache_write_1h"] = rates["in"] * CACHE_WRITE_1H_MULTIPLIER
    factor = 1.0
    tier = LONG_PROMPT_TIERS.get(_catalog_key(model))
    if tier and basis != "estimate" and usage.prompt_tokens > tier[0]:     # an estimate is already the high rate
        factor *= tier[1]
    mode = basis
    if usage.batch:
        factor *= BATCH_DISCOUNT
        mode += "+batch"
    geo = GEO_MULTIPLIERS.get((usage.inference_geo or "").lower())
    if geo:
        factor *= geo
        mode += "+geo_us"
    write_1h = min(usage.cache_write_1h_tokens or 0, usage.cache_write_tokens or 0)
    write_5m = (usage.cache_write_tokens or 0) - write_1h
    # Same order of operations as the pre-S6 formula, so calc_cost's numbers do not move by a float ulp.
    cache_cost = (write_5m * rates["cache_write"] + (usage.cache_read_tokens or 0) * rates["cache_read"]
                  + write_1h * rates["cache_write_1h"])
    raw = (usage.input_tokens or 0) * rates["in"] + (usage.output_tokens or 0) * rates["out"] + cache_cost
    applied = {k: v * factor for k, v in rates.items()}
    return Quote(cost_usd=round(raw * factor / 1000, 6), pricing_mode=mode, rates=applied)


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

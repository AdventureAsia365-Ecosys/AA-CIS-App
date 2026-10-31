"""The cost of one billed model request (root ADR 0009 decisions 8 and 13).

Same arithmetic as AA-CIS-App ``shared/llm_client/pricing.py`` (AA-658, AA-1002 S6), copied here as the shared core
(AA-642 S8) with one difference: this module never reads a catalog. The caller passes the catalog prices it read
through its own config source (CIS ``shared.llm_model_catalog``, TripPlanner ``catalog.v_llm_gateway_config_v1``),
so the module stays pure. ``tests/unit/test_aa642_s8_aa_llm_parity.py`` in AA-CIS-App proves ``quote`` gives the CIS
numbers and pricing mode for the same prices until the CIS module re-exports this one.

Rates are $ per 1K tokens inside this module (the CIS convention); catalog and deployment prices are $ per MTok.

- ``catalog_rates(...)``: the $/1K rates of one catalog row, or None when it has no input or output price.
- ``quote(model, usage, deployment, catalog=...)``: the cost and how it was priced (``Quote.pricing_mode``:
  ``override`` / ``catalog`` / ``code_table`` / ``estimate``, then ``+batch``, ``+geo_us``). A model no source can
  price is charged ``ESTIMATE_RATES`` (high on purpose, never 0) and flagged ``estimate``. Never raises.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Legacy Bedrock ids (the acc2-native tier selectors of the CIS client) and their $/1K prices: the fallback when the
# catalog cannot be read.
BEDROCK_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
BEDROCK_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
COST_TABLE = {
    BEDROCK_SONNET: {"in": 0.003, "out": 0.015},
    BEDROCK_HAIKU: {"in": 0.001, "out": 0.005},
    "gpt-4.1": {"in": 0.002, "out": 0.008},
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL writes
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_1H_MULTIPLIER = 2.0  # 1-hour TTL writes
BATCH_DISCOUNT = 0.5  # Message Batches / Bedrock batch: 50% off every token, cache tokens included
GEO_MULTIPLIERS = {"us": 1.1}  # Anthropic API inference_geo; "global" / None = 1.0
# Long-prompt tiers: catalog key -> (prompt tokens above which the tier applies, multiplier on every rate).
LONG_PROMPT_TIERS: dict[str, tuple[int, float]] = {"haiku-5-5": (100_000, 5.0)}
# The rates a model nobody can price is charged at (Sonnet 4.x list): high on purpose, never 0, always flagged.
ESTIMATE_RATES = {"in": 0.003, "out": 0.015}

PRICING_BASES = ("override", "catalog", "code_table", "estimate")
PRICING_MODE_RE = r"^(override|catalog|code_table|estimate)(\+batch)?(\+geo_us)?$"  # = migration 312's CHECK

# Label prefixes a gateway puts before a catalog key for a vendor path (CIS models.VENDOR_LABEL_PREFIXES).
VENDOR_LABEL_PREFIXES = (
    "anthropic-api-",
    "azure-openai-",
)
SATELLITE_PREFIX = "satellite-"

_SHORT_KEY_TO_MODEL_ID = {
    "sonnet": BEDROCK_SONNET,
    "haiku": BEDROCK_HAIKU,
    "sonnet-4-6": BEDROCK_SONNET,
    "haiku-4-5": BEDROCK_HAIKU,
    "satellite-sonnet-4-6": BEDROCK_SONNET,
    "satellite-haiku-4-5": BEDROCK_HAIKU,
}
_MODEL_ID_TO_CATALOG_KEY = {
    BEDROCK_SONNET: "sonnet",
    BEDROCK_HAIKU: "haiku",
    "sonnet-4-6": "sonnet",
    "haiku-4-5": "haiku",
}


@dataclass(frozen=True)
class Usage:
    """One billed request. ``input_tokens`` is the UNCACHED input; cache reads and writes are separate.
    ``cache_write_1h_tokens`` is the part of the writes made with the 1-hour TTL (the rest is 5-minute)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    batch: bool = False
    inference_geo: str | None = None

    @property
    def prompt_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)


@dataclass(frozen=True)
class Quote:
    cost_usd: float
    pricing_mode: str
    rates: dict  # $ per 1K tokens actually applied: in, out, cache_read, cache_write, cache_write_1h

    @property
    def estimated(self) -> bool:
        return self.pricing_mode.startswith("estimate")


def catalog_key(model: str | None) -> str:
    """The catalog key behind a gateway label: the satellite and vendor prefixes and the legacy ids removed."""
    key = model[len(SATELLITE_PREFIX) :] if model and model.startswith(SATELLITE_PREFIX) else (model or "")
    for prefix in VENDOR_LABEL_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix) :]
    return _MODEL_ID_TO_CATALOG_KEY.get(key, key)


def catalog_rates(
    price_in_per_mtok: float | None,
    price_out_per_mtok: float | None,
    price_cache_read_per_mtok: float | None = None,
    price_cache_write_per_mtok: float | None = None,
) -> dict | None:
    """The $/1K rates of one catalog row, or None when it has no input or output price (the row cannot price)."""
    if price_in_per_mtok is None or price_out_per_mtok is None:
        return None
    rate_in = float(price_in_per_mtok) / 1000
    return {
        "in": rate_in,
        "out": float(price_out_per_mtok) / 1000,
        "cache_read": (
            float(price_cache_read_per_mtok) / 1000
            if price_cache_read_per_mtok is not None
            else rate_in * CACHE_READ_MULTIPLIER
        ),
        "cache_write": (
            float(price_cache_write_per_mtok) / 1000
            if price_cache_write_per_mtok is not None
            else rate_in * CACHE_WRITE_MULTIPLIER
        ),
    }


def base_rates(model: str, catalog: dict | None) -> tuple[dict, str]:
    """($/1K rates, basis): ``catalog`` (the row's ``catalog_rates``) first, else COST_TABLE, else the flagged
    estimate."""
    if catalog is not None:
        return dict(catalog), "catalog"
    resolved = _SHORT_KEY_TO_MODEL_ID.get(model, model)
    base = COST_TABLE.get(resolved)
    basis = "code_table"
    if base is None:
        base, basis = ESTIMATE_RATES, "estimate"
    return {
        "in": base["in"],
        "out": base["out"],
        "cache_read": base["in"] * CACHE_READ_MULTIPLIER,
        "cache_write": base["in"] * CACHE_WRITE_MULTIPLIER,
    }, basis


_OVERRIDE_FIELDS = (
    ("price_in_override", "in"),
    ("price_out_override", "out"),
    ("price_cache_read_override", "cache_read"),
    ("price_cache_write_override", "cache_write"),
    ("price_cache_write_1h_override", "cache_write_1h"),
)


def apply_override(rates: dict, deployment) -> bool:
    """Deployment price overrides ($ per MTok; an object or a mapping with the ``price_*_override`` fields of
    ``shared.llm_model_deployment``) replace the matching rate. True when any applied."""
    if deployment is None:
        return False
    applied = False
    for field, key in _OVERRIDE_FIELDS:
        value = deployment.get(field) if isinstance(deployment, dict) else getattr(deployment, field, None)
        if value is not None:
            rates[key] = float(value) / 1000
            applied = True
    return applied


def quote(
    model: str,
    usage: Usage,
    deployment=None,
    *,
    catalog: dict | None = None,
    on_estimate: Callable[[str], None] | None = None,
) -> Quote:
    """The cost of one billed request of ``model`` with ``usage`` on ``deployment`` (or None), priced from
    ``catalog`` (the model's ``catalog_rates``, None when unknown or unreadable). Never raises. ``on_estimate`` is
    called with the model when no source could price it (the caller logs it)."""
    rates, basis = base_rates(model, catalog)
    if apply_override(rates, deployment):
        basis = "override"
    if basis == "estimate" and on_estimate is not None:
        try:
            on_estimate(model)
        except Exception:  # noqa: BLE001, S110 — pricing never fails on the caller's logger
            pass
    if rates.get("cache_write_1h") is None:
        rates["cache_write_1h"] = rates["in"] * CACHE_WRITE_1H_MULTIPLIER
    factor = 1.0
    tier = LONG_PROMPT_TIERS.get(catalog_key(model))
    if tier and basis != "estimate" and usage.prompt_tokens > tier[0]:  # an estimate is already the high rate
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
    # Same order of operations as the CIS formula, so the numbers do not move by a float ulp.
    cache_cost = (
        write_5m * rates["cache_write"]
        + (usage.cache_read_tokens or 0) * rates["cache_read"]
        + write_1h * rates["cache_write_1h"]
    )
    raw = (usage.input_tokens or 0) * rates["in"] + (usage.output_tokens or 0) * rates["out"] + cache_cost
    applied = {k: v * factor for k, v in rates.items()}
    return Quote(cost_usd=round(raw * factor / 1000, 6), pricing_mode=mode, rates=applied)

"""AA-642 S8 (root ADR 0009 decision 13): the shared core ``shared/aa_llm`` decides exactly like the CIS gateway.

``shared/aa_llm`` copies the pure logic of ``shared/llm_client/errors.py``, ``pricing.py`` and the ledger v2 row of
``call_log.py``. Until the CIS modules re-export the core (after the registry v2 lane and this slice both merge),
this test pins the two to each other on a table of cases: same error class, same quote (cost and pricing mode) for
the same catalog prices, same v2 column list and same v2 values. A change to one side without the other fails here.
No DB, no AWS, no vendor.
"""
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.aa_llm import errors as core_errors
from shared.aa_llm import ledger as core_ledger
from shared.aa_llm import pricing as core_pricing
from shared.llm_client import call_log
from shared.llm_client import errors as cis_errors
from shared.llm_client import pricing as cis_pricing
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.models import VENDOR_LABEL_PREFIXES

# ── errors ─────────────────────────────────────────────────────────────────────────────────────────────────────────

MESSAGES = [
    None,
    "",
    "An error occurred (ThrottlingException) when calling the InvokeModel operation: Too many requests",
    "An error occurred (AccessDeniedException) when calling the Converse operation: not authorized to perform x",
    "Error 002: Access to Bedrock models is not allowed for this account",
    "Error code: 429 - {'error': {'type': 'insufficient_quota'}}",
    "Error code: 429 - rate_limit_exceeded for the org",
    "credit balance is too low to access the model",
    "overloaded_error: please retry",
    "rate_limit_error",
    "permission_error",
    "You have reached your specified API usage limits",
    "enforced_spend_limit_reached",
    "llm_access_denied: every model refused",
    "connection reset by peer",
    "ServiceQuotaExceededException: quota",
    "read timeout on endpoint",
]


@pytest.mark.parametrize("text", MESSAGES)
def test_classify_text_matches_cis(text):
    assert core_errors.classify_text(text) == cis_errors.classify_text(text)
    assert core_errors.classify(text) == cis_errors.classify(text)


def _chained(core: bool):
    mod = core_errors if core else cis_errors
    try:
        try:
            raise RuntimeError("ThrottlingException: slow down")
        except RuntimeError as inner:
            raise mod.LLMAccessDenied("all refused", mod.QUOTA_EXHAUSTED) from inner
    except mod.LLMAccessDenied as outer:
        return outer


def test_classify_exception_chains_match_cis():
    plain = ValueError("Error 002: Access to Bedrock models is not allowed for this account")
    assert core_errors.classify(plain) == cis_errors.classify(plain)
    tagged = RuntimeError("all models failed")
    tagged.llm_error_class = "throttled"
    assert core_errors.classify(tagged) == cis_errors.classify(tagged) == "throttled"
    assert core_errors.classify(_chained(True)) == cis_errors.classify(_chained(False))
    assert core_errors.is_llm_access_denied(_chained(True)) is cis_errors.is_llm_access_denied(_chained(False)) is True


@pytest.mark.parametrize("classes", [[], ["access_denied"], ["quota_exhausted", "quota_exhausted"],
                                     ["access_denied", "quota_exhausted"], ["access_denied", "throttled"], ["other"]])
def test_account_level_class_matches_cis(classes):
    assert core_errors.account_level_class(classes) == cis_errors.account_level_class(classes)


_ID = "4" * 12  # built, never a literal account ID (public repo resource-ID guard)


@pytest.mark.parametrize("text", [None, "", f"role arn:aws:iam::{_ID}:role/x denied", f"id {_ID} here",
                                  "no ids here", f"{_ID}4 is thirteen digits"])
def test_redact_ids_matches_cis(text):
    assert core_errors.redact_ids(text) == cis_errors.redact_ids(text)


def test_error_constants_match_cis():
    for name in ("ACCESS_DENIED", "QUOTA_EXHAUSTED", "THROTTLED", "OTHER", "LLM_ACCESS_DENIED_REASON",
                 "ACCOUNT_LEVEL", "ARN_MARKER", "ACCOUNT_ID_MARKER"):
        assert getattr(core_errors, name) == getattr(cis_errors, name), name
    assert [(c, p.pattern) for c, p in core_errors._PATTERNS] == [(c, p.pattern) for c, p in cis_errors._PATTERNS]


# ── pricing ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _catalog_model(key, pin, pout, cread=None, cwrite=None):
    return CatalogModel(model_key=key, label=key, vendor="anthropic", provider="bedrock", api_style="converse",
                        bedrock_profile_ids={}, callable_via=("llm_client",), supports_temperature=False,
                        max_output_tokens=4096, price_in_per_mtok=pin, price_out_per_mtok=pout,
                        price_cache_read_per_mtok=cread, price_cache_write_per_mtok=cwrite, enabled=True)


CATALOG = {
    "writer-x": _catalog_model("writer-x", 2.0, 10.0, 0.2, 2.5),
    "judge-y": _catalog_model("judge-y", 0.8, 4.0),
    "haiku-5-5": _catalog_model("haiku-5-5", 0.1, 0.5),
    "half-priced": _catalog_model("half-priced", 1.0, None),
}

USAGES = [
    core_pricing.Usage(input_tokens=1200, output_tokens=300),
    core_pricing.Usage(input_tokens=50, output_tokens=900, cache_read_tokens=4000, cache_write_tokens=2000),
    core_pricing.Usage(input_tokens=50, output_tokens=10, cache_write_tokens=3000, cache_write_1h_tokens=1000),
    core_pricing.Usage(input_tokens=120_000, output_tokens=500),
    core_pricing.Usage(input_tokens=1000, output_tokens=1000, batch=True),
    core_pricing.Usage(input_tokens=1000, output_tokens=1000, inference_geo="us"),
    core_pricing.Usage(input_tokens=1000, output_tokens=1000, batch=True, inference_geo="US"),
    core_pricing.Usage(),
]

MODELS = ["writer-x", "judge-y", "haiku-5-5", "half-priced", "sonnet", "haiku", "satellite-sonnet-4-6",
          "gpt-4.1", "unknown-model", "satellite-writer-x", *(p + "writer-x" for p in VENDOR_LABEL_PREFIXES)]

DEPLOYMENTS = [
    None,
    SimpleNamespace(price_in_override=1.5, price_out_override=None, price_cache_read_override=None,
                    price_cache_write_override=None, price_cache_write_1h_override=None),
    SimpleNamespace(price_in_override=None, price_out_override=7.0, price_cache_read_override=0.1,
                    price_cache_write_override=1.0, price_cache_write_1h_override=3.0),
]


def _fake_get_model_sync(key):
    return CATALOG.get(key)


def _core_catalog(model):
    m = CATALOG.get(core_pricing.catalog_key(model))
    if m is None:
        return None
    return core_pricing.catalog_rates(m.price_in_per_mtok, m.price_out_per_mtok, m.price_cache_read_per_mtok,
                                      m.price_cache_write_per_mtok)


@pytest.mark.parametrize("model", MODELS)
def test_catalog_rates_match_cis(model):
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=_fake_get_model_sync):
        assert _core_catalog(model) == cis_pricing._catalog_rates(model)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("usage", USAGES)
@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_quote_matches_cis(model, usage, deployment):
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=_fake_get_model_sync):
        want = cis_pricing.quote(model, cis_pricing.Usage(**usage.__dict__), deployment)
    got = core_pricing.quote(model, usage, deployment, catalog=_core_catalog(model))
    assert got.cost_usd == want.cost_usd
    assert got.pricing_mode == want.pricing_mode
    assert got.rates == pytest.approx(want.rates)


@pytest.mark.parametrize("model", MODELS)
def test_quote_matches_cis_when_the_catalog_is_unreadable(model):
    usage = core_pricing.Usage(input_tokens=700, output_tokens=200, cache_read_tokens=100)
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=RuntimeError("db down")):
        want = cis_pricing.quote(model, cis_pricing.Usage(**usage.__dict__))
    got = core_pricing.quote(model, usage, catalog=None)
    assert (got.cost_usd, got.pricing_mode) == (want.cost_usd, want.pricing_mode)


def test_pricing_constants_match_cis():
    for name in ("COST_TABLE", "CACHE_WRITE_MULTIPLIER", "CACHE_READ_MULTIPLIER", "CACHE_WRITE_1H_MULTIPLIER",
                 "BATCH_DISCOUNT", "GEO_MULTIPLIERS", "LONG_PROMPT_TIERS", "ESTIMATE_RATES", "PRICING_BASES",
                 "PRICING_MODE_RE", "_SHORT_KEY_TO_MODEL_ID", "_MODEL_ID_TO_CATALOG_KEY"):
        assert getattr(core_pricing, name) == getattr(cis_pricing, name), name
    assert core_pricing.VENDOR_LABEL_PREFIXES == VENDOR_LABEL_PREFIXES


# ── ledger v2 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _insert_columns(sql: str) -> tuple:
    inner = re.search(r"\(([^)]*)\)\s*VALUES", sql, re.S).group(1)
    return tuple(c.strip() for c in inner.split(",") if c.strip())


def test_ledger_columns_match_the_cis_v2_insert():
    assert core_ledger.V2_COLUMNS == call_log.V2_COLUMNS
    assert core_ledger.COLUMNS == _insert_columns(call_log._INSERT_V2_SQL)
    assert _insert_columns(core_ledger.insert_sql()) == core_ledger.COLUMNS
    assert core_ledger.STATUSES == call_log.STATUSES
    assert core_ledger.ERROR_CLASSES == call_log.ERROR_CLASSES
    assert core_ledger.TRUNCATED_STOP_REASONS == call_log.TRUNCATED_STOP_REASONS


def test_ledger_placeholders_cast_like_the_cis_insert():
    cis = re.findall(r"\$\d+(?:::\w+)?", call_log._INSERT_V2_SQL)
    core = re.findall(r"\$\d+(?:::\w+)?", core_ledger.insert_sql())
    assert core == cis


V2_CASES = [
    dict(),
    dict(stop_reason="max_tokens"),
    dict(stop_reason="length", error_class="throttled"),
    dict(quality_signal={"billed_unusable": True}),
    dict(quality_signal={"shadow": True}),
    dict(quality_signal={"eval_run": "r1"}),
    dict(status="refused", error_class="access_denied", latency_ms=12, attempt_no=2),
    dict(status="bogus", error_class="bogus", latency_ms=-5, attempt_no=0),
    dict(status="error", error_class="deadline", latency_ms=True, attempt_no=1.5),
    dict(connection_key="acc3", deployment_wire_id="w" * 400, request_id="r" * 300, effort="high"),
    dict(tour_id="not-a-uuid", reasoning_tokens=40, prompt_hash="h" * 200, prompt_version="v3"),
    dict(tour_id="6f1c0a52-3c7e-4c55-9d2b-1f4f3d6a8b11", units=3, unit_kind="tasks", is_shadow=False),
    dict(is_experiment=True, is_shadow=True, quality_signal={"shadow": False}),
]


@pytest.mark.parametrize("case", V2_CASES)
def test_ledger_v2_values_match_cis(case):
    case = dict(case)
    qs = case.pop("quality_signal", {})
    stop = case.pop("stop_reason", None)
    conn = case.pop("connection_key", None)
    want = call_log._v2_values(stop_reason=stop, quality_signal=qs, connection_key=conn, app="cis", **case)
    row = core_ledger.LedgerRow(app="cis", stage="s", role="writer", model="m", quality_signal=qs, stop_reason=stop,
                                connection_key=conn, **case)
    assert row.v2_values() == want

"""AA-1024 A10 (root ADR 0009 decision 13): CIS runs on the shared core ``shared/aa_llm``.

Before A10 the CIS gateway kept its own copy of the logic and a parity test pinned the two to each other. Now
``shared/llm_client/errors.py``, ``pricing.py`` and the ledger v2 values of ``call_log.py`` import the core, so this
file checks:

- identity: the names CIS exposes are the core's own objects (one class, one function, one table);
- wiring: the CIS ``quote`` is the core's with the catalog row CIS reads, ``attempt_outcome`` asks the core first and
  keeps its CIS-typed refusals;
- shape: a CIS ledger row equals the core's ``LedgerRow`` column by column, and its v2 values equal the pre-A10 CIS
  writer's on a table of cases (``_pre_a10_v2_values`` below is that code, kept as the reference).
No DB, no AWS, no vendor.
"""
import json
import re
import uuid
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


def test_shared_llm_client_imports_the_core():
    """The Done-when of A10: ``git grep "from shared.aa_llm"`` hits in shared/llm_client."""
    from pathlib import Path
    folder = Path(__file__).resolve().parents[2] / "shared" / "llm_client"
    hits = {p.name for p in folder.glob("*.py") if "from shared.aa_llm" in p.read_text(encoding="utf-8")}
    assert {"errors.py", "pricing.py", "call_log.py", "client.py"} <= hits


# ── identity ───────────────────────────────────────────────────────────────────────────────────────────────────────

ERROR_NAMES = ("ACCESS_DENIED", "QUOTA_EXHAUSTED", "THROTTLED", "OTHER", "CREDENTIAL", "DEADLINE", "BUDGET",
               "TRUNCATED", "DISABLED", "ROLE_NOT_SERVED", "LEDGER_ERROR_CLASSES", "TRUNCATED_STOP_REASONS",
               "LLM_ACCESS_DENIED_REASON", "ACCOUNT_LEVEL", "ARN_MARKER", "ACCOUNT_ID_MARKER", "LLMAccessDenied",
               "classify", "classify_text", "is_llm_access_denied", "account_level_class", "redact_ids",
               "disabled_stage_of", "is_truncated", "reply_class", "is_truncated_error", "_PATTERNS", "_chain")


@pytest.mark.parametrize("name", ERROR_NAMES)
def test_cis_errors_are_the_core_objects(name):
    assert getattr(cis_errors, name) is getattr(core_errors, name)


def test_cis_only_errors_build_on_the_core():
    assert issubclass(cis_errors.LLMStageDisabled, core_errors.LLMAccessDenied)
    refusal = cis_errors.LLMStageDisabled("t9_write", "drill")
    assert core_errors.is_llm_access_denied(refusal)
    assert core_errors.classify(refusal) == core_errors.DISABLED
    assert core_errors.disabled_stage_of(str(refusal)) == "t9_write"
    assert core_errors.classify(cis_errors.RouteDeadlineExceeded("late")) == core_errors.DEADLINE
    role = cis_errors.RouteRoleNotServed("writer-x", "writer", [("acc2", "writer")])
    assert core_errors.classify(role) == core_errors.ROLE_NOT_SERVED


PRICING_NAMES = ("BEDROCK_SONNET", "BEDROCK_HAIKU", "COST_TABLE", "CACHE_WRITE_MULTIPLIER", "CACHE_READ_MULTIPLIER",
                 "CACHE_WRITE_1H_MULTIPLIER", "BATCH_DISCOUNT", "GEO_MULTIPLIERS", "LONG_PROMPT_TIERS",
                 "ESTIMATE_RATES", "PRICING_BASES", "PRICING_MODE_RE", "_SHORT_KEY_TO_MODEL_ID",
                 "_MODEL_ID_TO_CATALOG_KEY", "Usage", "Quote")


@pytest.mark.parametrize("name", PRICING_NAMES)
def test_cis_pricing_names_are_the_core_objects(name):
    assert getattr(cis_pricing, name) is getattr(core_pricing, name)


def test_vendor_label_prefixes_match_the_cis_models():
    # The core cannot import shared.llm_client.models (pure); the two tuples must agree.
    assert core_pricing.VENDOR_LABEL_PREFIXES == VENDOR_LABEL_PREFIXES


def test_cis_ledger_tables_are_the_core_objects():
    assert call_log.V2_COLUMNS is core_ledger.V2_COLUMNS
    assert call_log.STATUSES is core_ledger.STATUSES
    assert call_log.ERROR_CLASSES is core_ledger.ERROR_CLASSES is core_errors.LEDGER_ERROR_CLASSES
    assert call_log.TRUNCATED_STOP_REASONS is core_errors.TRUNCATED_STOP_REASONS


def test_ledger_error_classes_literals_match_the_class_constants():
    # The tuple is written as literals for the admin's unit test (frontend/lib/aiGatewayPolish.test.mjs parses it).
    assert core_errors.LEDGER_ERROR_CLASSES == (
        core_errors.ACCESS_DENIED, core_errors.QUOTA_EXHAUSTED, core_errors.THROTTLED, core_errors.CREDENTIAL,
        core_errors.OTHER, core_errors.DEADLINE, core_errors.BUDGET, core_errors.TRUNCATED, core_errors.DISABLED,
        core_errors.ROLE_NOT_SERVED)


def _insert_columns(sql: str) -> tuple:
    inner = re.search(r"\(([^)]*)\)\s*VALUES", sql, re.S).group(1)
    return tuple(c.strip() for c in inner.split(",") if c.strip())


def test_the_cis_v2_insert_names_the_core_columns_with_the_core_casts():
    # The statement text stays in call_log.py (the AA-729 eval harness relabels it); its columns and casts are the
    # core's.
    assert _insert_columns(call_log._INSERT_V2_SQL) == core_ledger.COLUMNS
    cis = re.findall(r"\$\d+(?:::\w+)?", call_log._INSERT_V2_SQL)
    core = re.findall(r"\$\d+(?:::\w+)?", core_ledger.insert_sql())
    assert core == cis


# ── pricing wiring: the CIS quote is the core's with the catalog row CIS reads ─────────────────────────────────────

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
    core_pricing.Usage(input_tokens=1000, output_tokens=1000, batch=True, inference_geo="US"),
    core_pricing.Usage(),
]

MODELS = ["writer-x", "judge-y", "haiku-5-5", "half-priced", "sonnet", "haiku", "satellite-sonnet-4-6",
          "gpt-4.1", "unknown-model", "satellite-writer-x", *(p + "writer-x" for p in VENDOR_LABEL_PREFIXES)]

DEPLOYMENTS = [
    None,
    SimpleNamespace(price_in_override=1.5, price_out_override=None, price_cache_read_override=None,
                    price_cache_write_override=None, price_cache_write_1h_override=None),
]


def _core_catalog(model):
    m = CATALOG.get(core_pricing.catalog_key(model))
    if m is None:
        return None
    return core_pricing.catalog_rates(m.price_in_per_mtok, m.price_out_per_mtok, m.price_cache_read_per_mtok,
                                      m.price_cache_write_per_mtok)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("usage", USAGES)
@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_cis_quote_is_the_core_quote_with_the_catalog_row(model, usage, deployment):
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=CATALOG.get):
        assert cis_pricing._catalog_rates(model) == _core_catalog(model)
        got = cis_pricing.quote(model, usage, deployment)
    assert got == core_pricing.quote(model, usage, deployment, catalog=_core_catalog(model))


def test_cis_quote_prices_from_the_tables_when_the_catalog_is_unreadable():
    usage = core_pricing.Usage(input_tokens=700, output_tokens=200, cache_read_tokens=100)
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=RuntimeError("db down")):
        got = cis_pricing.quote("haiku", usage)
        unknown = cis_pricing.quote("unknown-model", usage)
    assert got == core_pricing.quote("haiku", usage, catalog=None) and got.pricing_mode == "code_table"
    assert unknown.pricing_mode == "estimate" and unknown.cost_usd > 0


def test_calc_cost_still_wraps_quote():
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=CATALOG.get):
        assert cis_pricing.calc_cost("writer-x", 1000, 100, 10, 20) == cis_pricing.quote(
            "writer-x", core_pricing.Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=10,
                                           cache_write_tokens=20)).cost_usd


# ── outcome wiring: attempt_outcome asks the core first ────────────────────────────────────────────────────────────

MESSAGES = [
    "An error occurred (ThrottlingException) when calling the InvokeModel operation: Too many requests",
    "An error occurred (AccessDeniedException) when calling the Converse operation: not authorized to perform x",
    "Error 002: Access to Bedrock models is not allowed for this account",
    "Error code: 429 - {'error': {'type': 'insufficient_quota'}}",
    "credit balance is too low to access the model",
    "overloaded_error: please retry",
    "permission_error",
    "enforced_spend_limit_reached",
    "llm_access_denied: every model refused",
    "connection reset by peer",
    "read timeout on endpoint",
    "Error code: 400 - Could not finish the message because max_tokens or model output limit was reached",
]


@pytest.mark.parametrize("text", MESSAGES)
def test_attempt_outcome_agrees_with_the_core_on_provider_text(text):
    from shared.llm_client.client import attempt_outcome
    assert attempt_outcome(RuntimeError(text)) == core_ledger.failed_outcome(RuntimeError(text))


def test_attempt_outcome_cis_typed_refusals():
    from shared.cost_guard import BudgetExceeded
    from shared.llm_client import breaker, tenant_budget
    from shared.llm_client.client import attempt_outcome
    assert attempt_outcome(cis_errors.LLMStageDisabled("t9_write")) == ("refused", "disabled")
    assert attempt_outcome(cis_errors.RouteRoleNotServed("w", "writer", [("acc2", "writer")])) == (
        "refused", "role_not_served")
    assert attempt_outcome(BudgetExceeded("cap", "llm", "daily")) == ("refused", "budget")
    assert attempt_outcome(tenant_budget.TenantBudgetUnavailable("unreadable")) == ("refused", "budget")
    assert attempt_outcome(cis_errors.RouteDeadlineExceeded("late")) == ("error", "deadline")
    assert attempt_outcome(cis_errors.RouteAttemptCapReached("cap")) == ("error", "deadline")
    assert attempt_outcome(breaker.LimiterSaturated("busy")) == ("refused", "throttled")
    assert attempt_outcome(RuntimeError("boom")) == ("error", "other")


def test_attempt_outcome_kill_switch_wins_over_a_wrapping_error():
    from shared.llm_client.client import attempt_outcome
    try:
        try:
            raise cis_errors.LLMStageDisabled("t2_generate")
        except cis_errors.LLMStageDisabled:
            raise RuntimeError("ThrottlingException while handling")
    except RuntimeError as wrapped:
        assert attempt_outcome(wrapped) == ("refused", "disabled")


# ── ledger shape: a CIS row is the core row, column by column ──────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self):
        self.calls = []

    def is_in_transaction(self):
        return False

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def close(self):
        pass


SHAPE_CASES = [
    dict(stage="t9_write", role="writer", model="writer-x", tokens_in=1200, tokens_out=300, cost_usd=0.0054,
         quality_signal={"attempt": 1}, provider="bedrock-satellite", account="acc3", fallback_used=False,
         stop_reason="end_turn", cache_read_tokens=40, cache_write_tokens=0, pricing_mode="catalog",
         latency_ms=812, attempt_no=1, deployment_wire_id="wire-writer-x", request_id="req-1",
         prompt_hash="ab" * 8, prompt_version="v3", effort="medium", reasoning_tokens=12),
    dict(stage="a3_place_dedupe", role="validate", model="judge-y", tokens_in=0, tokens_out=0, cost_usd=0.0,
         quality_signal={}, provider="openai", account=None, connection_key="openai-direct",
         stop_reason=None, status="refused", error_class="throttled", latency_ms=35, attempt_no=2),
    dict(stage="t2_generate", role="writer", model="writer-x", tokens_in=900, tokens_out=4096, cost_usd=0.04,
         quality_signal={"shadow": True}, provider="anthropic-api", account="anthropic-direct",
         stop_reason="max_tokens", tenant_id=str(uuid.UUID(int=7)), tour_id=str(uuid.UUID(int=9)),
         units=3, unit_kind="tasks"),
    dict(stage="s1_rewrite", role="judge", model="judge-y", tokens_in=10, tokens_out=5, cost_usd=0.00001,
         quality_signal={"billed_unusable": True, "eval_run": "r1"}, provider="bedrock-native", account="acc2",
         stop_reason="end_turn", status="bogus", error_class="bogus", latency_ms=-3, attempt_no=0),
]

_CIS_ONLY_KEYS = ("connection_key",)


@pytest.mark.parametrize("case", SHAPE_CASES, ids=lambda c: c["stage"])
def test_a_cis_ledger_row_matches_the_core_row_column_by_column(case, monkeypatch):
    import asyncio

    fake = _FakeConn()

    async def _connect(*a, **k):
        return fake

    monkeypatch.setattr(call_log.asyncpg, "connect", _connect)
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://ledger.test/db")
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    # Known models, so CIS does not add its 'estimate' flag (a CIS-only fact: the core row states what it is given).
    monkeypatch.setattr("shared.llm_client.catalog.get_model_sync", CATALOG.get)
    asyncio.run(call_log.record_call(**case))
    assert len(fake.calls) == 1
    sql, args = fake.calls[0]
    assert _insert_columns(sql) == core_ledger.COLUMNS
    cis = dict(zip(core_ledger.COLUMNS, args))

    fields = {k: v for k, v in case.items() if k not in _CIS_ONLY_KEYS}
    # CIS states the connection column as the vendor connection, else the account (a Bedrock account is its
    # connection), and the account as the connection when the caller names none.
    conn_key = case.get("connection_key") or case.get("account")
    fields["account"] = case.get("account") or case.get("connection_key")
    core = dict(zip(core_ledger.COLUMNS, core_ledger.LedgerRow(app="cis", connection_key=conn_key, **fields).args()))

    for column in core_ledger.COLUMNS:
        if column == "quality_signal":
            assert json.loads(cis[column]) == json.loads(core[column]), column
        else:
            assert cis[column] == core[column], column


# The CIS writer's v2 normalisation before A10 (shared/llm_client/call_log.py at 01054cf0), kept as the reference the
# core-backed writer must still match.
def _pre_a10_v2_values(*, stop_reason=None, quality_signal=None, connection_key=None, eval_bound=False, **fields):
    def _text(value, limit):
        return value[:limit] if isinstance(value, str) and value else None

    def _int(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _uuid(value):
        if value is None or str(value) in ("", "None"):
            return None
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            return None

    statuses = ("ok", "refused", "error", "billed_unusable", "truncated")
    classes = ("access_denied", "quota_exhausted", "throttled", "credential", "other", "deadline", "budget",
               "truncated", "disabled", "role_not_served")
    qs = quality_signal if isinstance(quality_signal, dict) else {}
    status = fields.get("status") if fields.get("status") in statuses else None
    if status is None:
        if qs.get("billed_unusable") is True:
            status = "billed_unusable"
        elif isinstance(stop_reason, str) and stop_reason in ("max_tokens", "length", "model_context_window_exceeded"):
            status = "truncated"
        else:
            status = "ok"
    error_class = fields.get("error_class") if fields.get("error_class") in classes else None
    if error_class is None and status == "truncated":
        error_class = "truncated"
    latency, attempt = _int(fields.get("latency_ms")), _int(fields.get("attempt_no"))
    shadow, experiment = fields.get("is_shadow"), fields.get("is_experiment")
    return (
        _text(fields.get("app"), 40) or "cis", status, error_class,
        latency if latency is not None and latency >= 0 else None,
        _text(connection_key, 100), _text(fields.get("deployment_wire_id"), 300),
        attempt if attempt is not None and attempt >= 1 else None,
        _text(fields.get("request_id"), 200), _uuid(fields.get("tour_id")), _int(fields.get("reasoning_tokens")),
        _text(fields.get("effort"), 40),
        shadow if isinstance(shadow, bool) else qs.get("shadow") is True,
        experiment if isinstance(experiment, bool) else (eval_bound or "eval_run" in qs),
        _text(fields.get("prompt_hash"), 128), _text(fields.get("prompt_version"), 100), _int(fields.get("units")),
        _text(fields.get("unit_kind"), 40),
    )


V2_CASES = [
    dict(),
    dict(stop_reason="max_tokens"),
    dict(stop_reason="length", error_class="throttled"),
    dict(quality_signal={"billed_unusable": True}),
    dict(quality_signal={"shadow": True}),
    dict(quality_signal={"eval_run": "r1"}),
    dict(status="refused", error_class="access_denied", latency_ms=12, attempt_no=2),
    dict(status="refused", error_class="disabled"),
    dict(status="refused", error_class="role_not_served", attempt_no=1),
    dict(status="bogus", error_class="bogus", latency_ms=-5, attempt_no=0),
    dict(status="error", error_class="deadline", latency_ms=True, attempt_no=1.5),
    dict(connection_key="acc3", deployment_wire_id="w" * 400, request_id="r" * 300, effort="high"),
    dict(tour_id="not-a-uuid", reasoning_tokens=40, prompt_hash="h" * 200, prompt_version="v3"),
    dict(tour_id=str(uuid.UUID(int=11)), units=3, unit_kind="tasks", is_shadow=False),
    dict(is_experiment=True, is_shadow=True, quality_signal={"shadow": False}),
    dict(app="tripplanner"),
    dict(app=""),
    dict(app=7),
]


@pytest.mark.parametrize("case", V2_CASES)
@pytest.mark.parametrize("eval_bound", [False, True])
def test_cis_v2_values_match_the_pre_a10_writer(case, eval_bound):
    case = dict(case)
    if eval_bound:
        with call_log.bind_eval_stage("eval_x", "run-1"):
            got = call_log._v2_values(**case)
    else:
        got = call_log._v2_values(**case)
    assert got == _pre_a10_v2_values(eval_bound=eval_bound, **case)

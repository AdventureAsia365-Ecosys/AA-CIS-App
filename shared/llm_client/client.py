import os
import hashlib
import json
import random
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
import boto3
from botocore.config import Config
from typing import Optional

import openai  # noqa: F401  (kept: tests patch shared.llm_client.client.openai; the client is adapters/openai_chat.py)
import structlog
from .models import ANTHROPIC_API_LABEL_PREFIX, AZURE_OPENAI_LABEL_PREFIX, LLMRequest, LLMResponse
from .prompt_cache import build_cached_system_prompt, build_cached_messages
from .pricing import BEDROCK_SONNET, BEDROCK_HAIKU, COST_TABLE, Usage, calc_cost
from . import pricing
from .role_config import get_stage_config_sync
from .bedrock_satellite import DEFAULT_SATELLITE_ACCOUNT
from .catalog import LEGACY_KEYS, CatalogModel, capability, get_model_sync
from .schema_check import SchemaMismatch, conform
from .errors import (DEADLINE, OTHER, THROTTLED, TRUNCATED, LLMAccessDenied, RouteDeadlineExceeded,
                     account_level_class, classify)
from . import (breaker, connection_budget, connections, data_scope, prompt_registry, stage_params, stream_sink,
               tenant_budget)
from .vendor_route import (VendorDeployment, deployment_plan, pinned_deployment, split_pin,
                           vendor_deployment_for)
from .deployments import alias_targets_sync

logger = structlog.get_logger()

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-west-1")

# Default model tier — override via ECS env var DEFAULT_MODEL_TIER. Only reached when a call
# passes neither an explicit request.model_tier NOR a request.stage (AA-518) — every real call
# site in this codebase now passes at least one of those two, so this is belt-and-suspenders for
# an unforeseen caller, not the live default path anymore.
# Options: "haiku" (cheapest) | "sonnet" (premium). "gpt-4.1" (OpenAI platform API) is retired (S235,
# migration 224 disables its catalog row) and refused by the gate below.
DEFAULT_MODEL_TIER = os.environ.get("DEFAULT_MODEL_TIER", "haiku")

# BEDROCK_SONNET/BEDROCK_HAIKU/COST_TABLE relocated to pricing.py (AA-518/AA-505) so Mechanism-B
# call sites (invoke_claude() direct, e.g. T5 atomize, N7 E2-E5) can price a call without
# importing this whole client — re-exported here unchanged so nothing importing them FROM this
# module breaks.

LEGACY_ACC2_NATIVE_ENV = "LLM_LEGACY_ACC2_NATIVE"
# AA-1002 (S3) rollback switches, read per call (ECS env, AA-CIS-Infra task definition):
#   LLM_DEPLOYMENT_ROUTING (default ON): a catalog key is tried on its deployments (vendor_route.deployment_plan) —
#     with the 304 / 306 rows that is today's single call per model, for every Dev stage (snapshot test); "off"
#     restores the S2 code path byte for byte.
#   LLM_LEGACY_ALIASES (default OFF): the legacy haiku / sonnet keys run their alias targets (shared.llm_model_alias,
#     migration 307) through the catalog + deployments instead of the hard-coded _legacy_chain. With the 304 / 307 rows
#     the calls are identical (tests), but it has not run live yet (every provider is blocked, S259): switch it on
#     after a live check of one legacy stage. Off, or no alias row / no registry, = _legacy_chain.
DEPLOYMENT_ROUTING_ENV = "LLM_DEPLOYMENT_ROUTING"
LEGACY_ALIASES_ENV = "LLM_LEGACY_ALIASES"
#   LLM_CONVERSE_CACHE_POINT (default ON, AA-1002 S6): a Converse request to a Claude model whose system prompt
#     reaches the model's minimum cacheable length ends its system list with a cachePoint (_converse_cache_point);
#     "off" sends the pre-S6 request body byte for byte.
CONVERSE_CACHE_POINT_ENV = "LLM_CONVERSE_CACHE_POINT"
_ON = ("1", "true", "on", "yes")
_OFF = ("0", "false", "off", "no")


def _in_eval_run() -> bool:
    """AA-919: True while a writing-memory eval run is bound (call_log.bind_eval_stage)."""
    from .call_log import eval_label
    return eval_label() is not None


def legacy_acc2_native_enabled() -> bool:
    """AA-843 — whether the legacy haiku/sonnet chain still tries acc2-native Bedrock first.

    acc2 is a channel-program account: Anthropic models there fail every time with
    "ValidationException … not available for channel program accounts" (~2,400 wasted attempts a
    week, ~0.1-0.2 s each). Off by default, so the chain starts at the satellite. Set the ECS env var
    LLM_LEGACY_ACC2_NATIVE=on (1/true/yes) to restore the old acc2-first order if acc2 ever gains
    model access. Read per call, not at import."""
    return os.environ.get(LEGACY_ACC2_NATIVE_ENV, "").strip().lower() in ("1", "true", "on", "yes")


def _switch(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return default


def deployment_routing_enabled() -> bool:
    """AA-1002 (S3): catalog keys are spread over their deployments (default on; LLM_DEPLOYMENT_ROUTING=off rolls back
    to the S2 path)."""
    return _switch(DEPLOYMENT_ROUTING_ENV, True)


def converse_cache_point_enabled() -> bool:
    """AA-1002 (S6): Converse sends a cachePoint after a long Claude system prompt (default on; read per call)."""
    return _switch(CONVERSE_CACHE_POINT_ENV, True)


def legacy_aliases_enabled() -> bool:
    """AA-1002 (S3): legacy haiku / sonnet keys run through their aliases (default off; LLM_LEGACY_ALIASES=on)."""
    return _switch(LEGACY_ALIASES_ENV, False)


# The InvokeModel label and the price key of the two catalog rows whose wire format is the legacy Messages body
# (api_style anthropic_native): the same values _call_bedrock_satellite / _call_bedrock produce for the legacy chain.
_NATIVE_PRICE_KEY = {"sonnet": BEDROCK_SONNET, "haiku": BEDROCK_HAIKU}


class OpenAIPlatformBlocked(RuntimeError):
    """S235 — a call would go to an OpenAI platform API model whose catalog row is not enabled."""


# The OpenAI API connection of the paths that name no deployment (a catalog openai_chat key without registry rows, the
# retired gpt-4.1 tier): migration 304's row. AA-1002 (S9): the SDK client is built per connection at call time
# (adapters/openai_chat.py); a deployment on another openai / azure_openai connection names its own.
OPENAI_CONNECTION_KEY = "openai-direct"
ACC2_CONNECTION_KEY = "acc2"     # AA-1002 (S10): the task-role Bedrock connection the acc2-native legacy tiers call
# AA-1002 (S9): a reply from an Azure OpenAI connection is labelled AZURE_OPENAI_LABEL_PREFIX + catalog key (models.py),
# so llm_call_log records provider='azure_openai' for every caller (call_log._normalize_model_provider), as the
# Anthropic API path does with "anthropic-api-<key>". OpenAI-API replies keep the bare catalog key.


def _openai_data_scope(conn: Optional[connections.ProviderConnection] = None) -> str:
    conn = conn or connections.connection_or_builtin(OPENAI_CONNECTION_KEY)
    return conn.data_scope if conn is not None else data_scope.VENDOR_DIRECT


def openai_platform_allowed(model_key: str) -> bool:
    """S235 (Nghiệp, 14/10/2026): Bedrock first; the OpenAI platform API is only the last-resort
    fallback of a stage route (gpt-6-luna-openai at the end of the judge routes). The adapter
    (`LLMClient._call_openai`) runs only for a catalog row with provider 'openai' that is enabled.
    Migration 224 retires gpt-4.1 (disabled), so the legacy gpt-4.1 tier and the legacy chain's
    GPT-4.1 last resort are refused here. Fails closed: an unreadable catalog (DB down, unit tests)
    means not allowed. AA-1002 (S5): the stage's data scope is a second gate (``openai_scope_allowed``)."""
    m = get_model_sync(model_key)
    return m is not None and m.provider in OPENAI_FORMAT_PROVIDERS and m.enabled


# AA-1002 (S9): the catalog providers the OpenAI-format adapter serves (an Azure OpenAI deployment of an OpenAI model is
# provider 'openai' in the catalog; a model only on Azure may say 'azure_openai').
OPENAI_FORMAT_PROVIDERS = ("openai", "azure_openai")


def _int_attr(obj, name: str) -> int:
    value = getattr(obj, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _str_attr(obj, name: str) -> Optional[str]:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) else None


def _openai_cached_tokens(usage, prompt_tokens) -> int:
    """AA-1002 (S6): OpenAI's cached prompt tokens (usage.prompt_tokens_details.cached_tokens), 0 when absent or not an
    int (an older SDK, a test double), never more than the prompt."""
    details = getattr(usage, "prompt_tokens_details", None)
    value = details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or not isinstance(prompt_tokens, int):
        return 0
    return min(value, prompt_tokens)


def openai_scope_allowed(allowed_scopes: Optional[tuple],
                         conn: Optional[connections.ProviderConnection] = None) -> bool:
    """AA-1002 (S5): True when a stage with ``allowed_scopes`` may send content to the OpenAI connection (its data
    scope: vendor_direct). None (no stage policy: the column not migrated, an adhoc stage) = allowed, the S235 catalog
    gate alone (``openai_platform_allowed``), i.e. the pre-S5 behaviour. AA-1002 (S9): ``conn`` = the connection the
    call goes to (default openai-direct)."""
    return allowed_scopes is None or data_scope.allows(allowed_scopes, _openai_data_scope(conn))


# AA-1002 (S5): (stage, allowed_scopes) of the call being generated, bound by LLMClient._generate / the shadow so the
# gateway reads a stage's config once per call (the route and the scopes come from the same row).
_STAGE_SCOPES: ContextVar[Optional[tuple]] = ContextVar("llm_stage_scopes", default=None)

# ── AA-1018 part 2: the route deadline ───────────────────────────────────────────────────────────────────────────
#
# llm_role_config.route_deadline_s (migration 320) caps the wall time of a whole request: every model of the route,
# every deployment of a model, every legacy tier. NULL (every row today) binds nothing: no check, no timeout, the
# provider requests are byte-identical. When set, the gateway checks the remaining time before each attempt after the
# first (the first always runs) and raises RouteDeadlineExceeded instead of starting another one (ledger status
# 'error', error_class 'deadline', 0 USD); the OpenAI and Anthropic API adapters also get the remaining time as their
# per-request timeout. Bedrock requests keep their client's read timeout (botocore has no per-request timeout): a slow
# Bedrock attempt finishes, the next attempt is the one refused.
_DEADLINE: ContextVar[Optional[tuple]] = ContextVar("llm_route_deadline", default=None)   # (monotonic end, seconds)
DEADLINE_MIN_TIMEOUT_S = 1.0       # a per-request timeout never below this (the SDKs refuse 0 / negative values)


def deadline_remaining() -> Optional[float]:
    """Seconds left of the bound route deadline (may be <= 0), or None when the request has no deadline."""
    bound = _DEADLINE.get()
    return None if bound is None else bound[0] - time.monotonic()


def request_timeout() -> Optional[float]:
    """The per-request timeout an API adapter should pass: the remaining route time (at least
    DEADLINE_MIN_TIMEOUT_S), or None without a deadline (the connection's own timeout applies, as before)."""
    left = deadline_remaining()
    return None if left is None else max(left, DEADLINE_MIN_TIMEOUT_S)


def check_deadline(request: LLMRequest, before: str) -> None:
    """Raise RouteDeadlineExceeded when the bound route deadline has passed (``before`` names the attempt not
    started). No deadline bound: nothing."""
    bound = _DEADLINE.get()
    if bound is not None and time.monotonic() >= bound[0]:
        raise RouteDeadlineExceeded(f"route deadline of {bound[1]} s for stage {request.stage!r} passed before "
                                    f"{before}")


# ── AA-1018 part 2: deprecated gateway paths (D4 in part) ─────────────────────────────────────────────────────────
#
# Paths that call a connection without the deployment plan. Since S262 each runs inside breaker.guarded (circuit,
# limiter, outcome), and each use is logged once per (path, stage) per process with its deprecation dates, so the
# removal can be planned from the logs and the S262 step-0 queries Q1-Q3 (legacy-key routes, traffic per stage x model,
# route vs models seen). Removal needs: S11 (the legacy stages moved to aliases, a route change with approval and a
# paid trial) and 30 days without traffic on the path. REMOVE_AFTER is the proposed date, not a gate.
DEPRECATED_SINCE = "2026-10-31"
DEPRECATED_PATHS = {
    "legacy_chain": ("2026-12-31", "the haiku / sonnet aliases (LLM_LEGACY_ALIASES=on, shared.llm_model_alias)"),
    "s2_vendor_deployment": ("2026-12-31", "the deployment plan (LLM_DEPLOYMENT_ROUTING on)"),
    "catalog_fallback_converse": ("2026-12-31", "the deployment plan (registry rows for the model)"),
    "catalog_fallback_openai": ("2026-12-31", "the deployment plan (registry rows for the model)"),
}
_DEPRECATED_LOGGED: set = set()


def log_deprecated_path(path: str, stage: Optional[str]) -> None:
    """One warning per (path, stage) per process: llm_deprecated_path_used with the dates and the replacement."""
    key = (path, stage or "unstaged")
    if key in _DEPRECATED_LOGGED:
        return
    _DEPRECATED_LOGGED.add(key)
    remove_after, replacement = DEPRECATED_PATHS.get(path, ("", ""))
    logger.warning("llm_deprecated_path_used", path=path, stage=key[1], deprecated_since=DEPRECATED_SINCE,
                   remove_after=remove_after, replacement=replacement)


def _guarded(conn: Optional[connections.ProviderConnection], model_key: str, call, *, answered: tuple = ()):
    """``breaker.guarded`` on ``conn`` (admission, limiter, outcome); a plain call when the connection is unknown."""
    if conn is None:
        return call()
    return breaker.guarded(conn, model_key, call, answered=answered)


# The catalog key of what a legacy tier really calls (its health row: llm_provider_health.model_key references the
# catalog). The T2.5a tier of a "sonnet" request calls Haiku, so it is counted on haiku.
_LEGACY_BREAKER_KEY = {BEDROCK_SONNET: "sonnet", BEDROCK_HAIKU: "haiku", "gpt-4.1": "gpt-4.1"}

# AA-1018 part 2: the ledger roles an LLM request may state for itself (LLMRequest.role, used when its stage has no
# llm_role_config row: an adhoc_* script, a drill). The stage row's role always wins.
REQUEST_ROLES = ("writer", "judge", "validate", "embed")


def _role_of(request: LLMRequest, stage_cfg) -> str:
    """The ledger role of a request: its stage row's (llm_role_config or SAFE_DEFAULTS), else the role the request
    states (REQUEST_ROLES), else 'writer' (the pre-AA-1018 default). A stage with no row resolves to role_config's
    generic fallback (stage 'unknown'), which does not count as a row."""
    has_row = (stage_cfg is not None and getattr(stage_cfg, "role", None)
               and getattr(stage_cfg, "stage", None) != "unknown")
    if has_row:
        return stage_cfg.role
    asked = getattr(request, "role", None)
    if asked in REQUEST_ROLES:
        return asked
    return getattr(stage_cfg, "role", None) or "writer"


def stage_scopes(request: LLMRequest) -> Optional[tuple]:
    """AA-1002 (S5): the data scopes of ``request.stage``: the ones bound for this call, else role_config (20 s cache);
    None for no stage policy (no stage, a stage with no row, the column not migrated). Never raises."""
    if not request.stage:
        return None
    bound = _STAGE_SCOPES.get()
    if bound is not None and bound[0] == request.stage:
        return bound[1]
    try:
        return data_scope.normalize(getattr(get_stage_config_sync(request.stage), "allowed_scopes", None))
    except Exception as e:  # noqa: BLE001 — role_config never raises; belt and braces on the call path
        logger.warning("llm_stage_scope_read_failed", stage=request.stage, error=str(e)[:200])
        return None


def _checked_json(model_label: str, value, schema: dict) -> str:
    """AA-659 — a structured output that does not match its schema raises, so the stage route
    moves to its next model instead of a caller's error path accepting it silently."""
    try:
        return json.dumps(conform(value, schema["schema"]))
    except SchemaMismatch as e:
        raise RuntimeError(f"{model_label} output does not match schema {schema['name']!r}: {e}") from e


class UnusableReply(RuntimeError):
    """S251 (AA-835): the model answered and was billed, but the reply cannot be used. A caller that treats a bad
    reply differently from an outage (no retry, an `unsure` verdict) catches this; its gateway rows are already
    written, so the caller must not log the attempt again. Cost of the attempt(s): `unusable_reply_cost()`."""


class BilledUnusableReply(UnusableReply):
    """AA-927: the provider billed the request(s) but the reply is unusable (schema mismatch after the
    Converse retry, empty content, invalid JSON). `billed_usage` = one dict per billed model attempt
    (model, account, tokens, cost — internal retries folded, `provider_requests` > 1), so the gateway can
    write the llm_call_log row nobody else would: the caller never sees this attempt. Built with
    `_billed_unusable()`."""

    billed_usage: tuple = ()


def _billed_unusable(message: str, billed_usage: list[dict]) -> BilledUnusableReply:
    exc = BilledUnusableReply(message)
    exc.billed_usage = billed_usage
    return exc


class RouteUnusableReply(UnusableReply):
    """S251 (AA-835): every model a stage route tried answered with an unusable (billed) reply; nothing was down.
    Same message as the route's plain RuntimeError. `logged_usage` holds the attempts' billing dicts, which
    `_call_key_logged` has already written. It is deliberately NOT named `billed_usage`, so `_billed_usage_of()` never
    logs them a second time."""

    logged_usage: tuple = ()


def unusable_reply_cost(exc: BaseException) -> float:
    """The billed USD of an UnusableReply's attempt(s) (0.0 for any other exception), for a caller's own run budget.
    The gateway rows already count it in llm_call_log."""
    usage = getattr(exc, "billed_usage", None) or getattr(exc, "logged_usage", None) or ()
    return float(sum(float((u or {}).get("cost_usd") or 0.0) for u in usage))


def _all_failed(message: str, failures: list) -> RuntimeError:
    """AA-900: the error to raise when every model a request tried failed (`failures`, one exception each).
    LLMAccessDenied when all of them were refused at account level (access denied / no credit): a retry cannot
    help, so the job runner fails the job at once. Otherwise the plain RuntimeError as before. The last failure is
    the cause either way (the job error and the traceback keep the provider's own words)."""
    if failures and all(isinstance(f, connection_budget.ConnectionBudgetExceeded) for f in failures):
        # AA-1002 (S10): nothing was called, every model was left out by a connection's hard monthly budget: a budget
        # stop (the job runner ends the job as stopped_budget), not an outage to retry.
        keys = tuple(dict.fromkeys(k for f in failures for k in f.connection_keys))
        budget = connection_budget.exceeded(message, keys)
        budget.__cause__ = failures[-1]
        return budget
    classes = [classify(f) for f in failures]
    cls = account_level_class(classes)
    exc: RuntimeError = LLMAccessDenied(message, cls) if cls else RuntimeError(message)
    if not cls:
        # Decided here: a mixed failure is not account-level, whatever its last cause says.
        exc.llm_error_class = THROTTLED if THROTTLED in classes else OTHER
    if failures:
        exc.__cause__ = failures[-1]
    return exc


class _AliasSteps(list):
    """AA-1002 (S4): a legacy alias's (catalog model, deployment) attempts, plus ``circuit_errors``: one
    breaker.CircuitOpen per alias target none of whose deployments could be called (every circuit open). _run_alias
    starts its failure list with them, so _all_failed keeps the class that opened the circuits."""

    def __init__(self, items=()):
        super().__init__(items)
        self.circuit_errors: list = []


def _billed_usage(model_used: str, account: Optional[str], usage_totals: dict, cost: float,
                  stop_reason: Optional[str], provider_requests: int, *, cache_read: int = 0, cache_write: int = 0,
                  pricing_mode: Optional[str] = None) -> dict:
    """One billed attempt of a BilledUnusableReply. `account` is the AWS account key on Bedrock, the vendor
    connection key on a vendor API (AA-1002 S6: OPENAI_CONNECTION_KEY / the Anthropic connection); the cache split
    and pricing_mode reach llm_call_log through call_log.billed_unusable_rows."""
    rec = {"model": model_used, "account": account, "tokens_in": usage_totals.get("tokens_in"),
           "tokens_out": usage_totals.get("tokens_out"), "cost_usd": cost, "stop_reason": stop_reason,
           "provider_requests": provider_requests}
    if cache_read or cache_write:
        rec.update(cache_read_tokens=cache_read, cache_write_tokens=cache_write)
    if pricing_mode:
        rec["pricing_mode"] = pricing_mode
    return rec


def _add_converse_usage(total: dict, usage: dict) -> None:
    for wire, key in (("inputTokens", "in"), ("outputTokens", "out"), ("cacheReadInputTokens", "cache_read"),
                      ("cacheWriteInputTokens", "cache_write")):
        total[key] = total.get(key, 0) + (usage.get(wire, 0) or 0)


def _first_tool_input(blocks: list, schema: dict, model, allow_text_json: bool = False) -> dict:
    """AA-714 — the forced-tool input from a Converse response, or raise if the model skipped it.

    AA-803 (S246): with `allow_text_json` (tool choice `auto`, see `_forced_tool_choice_ok`) a model
    that answered with the JSON object as plain text instead of calling the tool is accepted too —
    the schema check (`_checked_json`) still runs on whatever comes back."""
    tool_inputs = [b["toolUse"]["input"] for b in blocks if "toolUse" in b]
    if tool_inputs:
        return tool_inputs[0]
    if allow_text_json:
        parsed = _json_object_in_text("".join(b.get("text", "") for b in blocks))
        if parsed is not None:
            return parsed
    raise RuntimeError(f"{model.model_key} returned no tool call for schema {schema['name']!r}")


def _json_object_in_text(text: str) -> Optional[dict]:
    """The JSON object in a plain-text answer (optionally inside a ``` fence), or None."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    if not t.startswith("{"):
        return None
    try:
        value = json.loads(t)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# S251 (AA-835 live run): the legacy haiku/sonnet chain calls the Anthropic Messages API (invoke_claude /
# _call_bedrock), which has no tool or response_format here, so a request's json_schema used to be dropped without a
# word and the caller got free text ("reply is not JSON"). The schema is now asked for in the system prompt and the
# reply is checked like a Converse reply. Stages that warned once per process are kept here.
_LEGACY_SCHEMA_WARNED: set[str] = set()


def _legacy_schema_instruction(schema: dict) -> str:
    """The system-prompt instruction a legacy key gets for a structured request (appended after the caller's own
    system prompt, so the caller's text stays first)."""
    return (f"Answer with exactly one JSON object that matches the JSON schema {schema['name']!r} below, and nothing "
            "else: no prose before or after it, no markdown, no code fence.\n"
            f"JSON schema:\n{json.dumps(schema['schema'], ensure_ascii=False, sort_keys=True)}")


# AA-803 (S246): Claude models whose Messages API answers a forced tool choice (`any` / `tool`) with a
# 400 — Opus 5.5, Sonnet 5.5, Fable 5.1 / Mythos 5.1. Haiku 5.5 is NOT one of them: it accepts a forced
# tool (with thinking on, the response then starts with the tool call and carries no thinking block), so
# it keeps the forced tool — fewer output tokens and a reliable schema on its high-volume stages, the same
# request shape Haiku 4.5 gets. Matched against the Bedrock
# inference-profile id (e.g. "global.anthropic.claude-sonnet-5-5"), so every account's profile of the
# same model matches. A model outside this list that rejects forced tool use is caught at runtime
# (`_tool_choice_rejected`) and remembered for the life of the process.
# AA-1017 (registry v2): the catalog row's capability `forced_tool_choice` (bool) answers first; these markers are the
# fallback for a row that does not state it (every row at migration 320).
_NO_FORCED_TOOL_CHOICE_MARKERS = (
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1",
)
_FORCED_TOOL_REJECTED: set[str] = set()


def _forced_tool_choice_ok(model, profile_id: str) -> bool:
    if model.model_key in _FORCED_TOOL_REJECTED:
        return False
    stated = capability(model, "forced_tool_choice")
    if isinstance(stated, bool):
        return stated
    pid = (profile_id or "").lower()
    return not any(marker in pid for marker in _NO_FORCED_TOOL_CHOICE_MARKERS)


def _tool_choice_rejected(exc: BaseException) -> bool:
    """AA-803: Bedrock's 400 for a forced tool choice the model does not take, e.g.
    "ValidationException ... tool_choice: type "tool" and "any" are not supported for this model"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "validationexception" in text and ("tool_choice" in text or "toolchoice" in text) and any(
        w in text for w in ("not supported", "unsupported", "not allowed", "thinking", "deprecated"))


def _auto_tool_instruction(schema: dict) -> str:
    return (f"Return your answer by calling the tool {schema['name']!r} exactly once, with the complete "
            "object as its input. Do not answer in plain text.")


def _use_auto_tool_choice(kwargs: dict, schema: dict) -> None:
    """Switch a Converse request from a forced tool to `auto` + an explicit instruction. The original
    system block stays first and unchanged (prompt-cache prefix); the instruction is a second block."""
    kwargs["toolConfig"] = {**kwargs["toolConfig"], "toolChoice": {"auto": {}}}
    # AA-1002 (S6): before a trailing cachePoint, so the instruction is part of the cached prefix.
    system = list(kwargs["system"])
    at = len(system) - 1 if system and "cachePoint" in system[-1] else len(system)
    kwargs["system"] = [*system[:at], {"text": _auto_tool_instruction(schema)}, *system[at:]]


# AA-1002 (S6) — prompt caching on Converse. Bedrock caches a Claude prompt prefix only up to an explicit cachePoint
# block (the Luna / OpenAI-on-Bedrock models cache on their own and take no cachePoint). The point goes at the END of
# the system list, so the cached prefix is the tool definitions + the whole system prompt (render order tools ->
# system -> messages): the part that repeats across the tours of a stage; the user message (the tour) stays uncached.
# A prefix below the model's minimum is simply not cached (no error, no charge); the threshold only keeps the block
# off requests that cannot cache. Minimums (claude-api skill, prompt caching, 30/10/2026; same on every platform),
# matched against the profile id in order (first hit wins); unknown Claude family = the highest minimum.
_CACHE_POINT_MIN_TOKENS = (
    (("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5", "claude-fable-5",
      "claude-mythos-5"), 512),
    (("claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-opus-4-1",
      "claude-opus-4-2025", "claude-sonnet-4-2025"), 1024),
    (("claude-opus-4-7", "claude-3-5-haiku", "claude-haiku-3-5"), 2048),
    (("claude-opus-4-6", "claude-opus-4-5", "claude-haiku-4-5"), 4096),
)
_CACHE_POINT_DEFAULT_MIN_TOKENS = 4096
# A rough token estimate for the threshold (English prose ~4 characters per token). Erring either way is harmless:
# too short a prefix is not cached and not charged.
_CHARS_PER_TOKEN = 4
CACHE_POINT_BLOCK = {"cachePoint": {"type": "default"}}
_CACHE_POINT_REJECTED: set[str] = set()


def _cache_point_min_tokens(profile_id: str) -> int:
    pid = (profile_id or "").lower()
    for markers, minimum in _CACHE_POINT_MIN_TOKENS:
        if any(m in pid for m in markers):
            return minimum
    return _CACHE_POINT_DEFAULT_MIN_TOKENS


def _converse_cache_point(model, profile_id: str, system_prompt: str) -> bool:
    """True when a Converse request to ``profile_id`` should end its system list with a cachePoint: a Claude model
    on Bedrock (an ``anthropic.`` inference profile), the switch on, the model not seen rejecting it, and a system
    prompt at or above the model's minimum cacheable length.

    AA-1017: the catalog row's capabilities answer first — ``prompt_cache`` (bool) instead of the ``anthropic.``
    profile test, ``cache_min_tokens`` (int) instead of the per-family table; unset = today's rules."""
    pid = (profile_id or "").lower()
    stated = capability(model, "prompt_cache")
    cacheable = stated if isinstance(stated, bool) else "anthropic." in pid
    if not cacheable or not converse_cache_point_enabled() or model.model_key in _CACHE_POINT_REJECTED:
        return False
    minimum = capability(model, "cache_min_tokens")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        minimum = _cache_point_min_tokens(pid)
    return len(system_prompt or "") // _CHARS_PER_TOKEN >= minimum


def _cache_point_rejected(exc: BaseException) -> bool:
    """Bedrock's 400 for a cachePoint the model / profile does not take (a ValidationException naming caching)."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "validationexception" in text and ("cachepoint" in text or "cache point" in text
                                              or "prompt caching" in text or "cache_control" in text)


def _strip_cache_point(kwargs: dict) -> None:
    kwargs["system"] = [b for b in kwargs.get("system") or [] if "cachePoint" not in b]


def _converse_call(call, kwargs: dict, model_key: str):
    """One Converse request. AA-1002 (S6): when Bedrock rejects the cachePoint we sent, retry ONCE without it (a
    ValidationException is not billed), drop it from ``kwargs`` for any follow-up call and remember the model."""
    try:
        return call(**kwargs)
    except Exception as e:  # noqa: BLE001 — botocore ClientError; anything else is re-raised below
        if not any("cachePoint" in b for b in kwargs.get("system") or []) or not _cache_point_rejected(e):
            raise
        logger.warning("converse_cache_point_rejected", model=model_key, error=str(e)[:200],
                       action="retry_without_cache_point")
        _CACHE_POINT_REJECTED.add(model_key)
        _strip_cache_point(kwargs)
        return call(**kwargs)


def _converse_with_param_downgrades(call, kwargs: dict, model_key: str, schema: Optional[dict]):
    """`_converse_dropping_rejected_temperature`, plus AA-803: when Bedrock rejects the forced tool
    choice of a structured call, switch `kwargs` to `auto` (`_use_auto_tool_choice`), remember the model
    and retry ONCE. A ValidationException is not billed."""
    try:
        return _converse_dropping_rejected_temperature(call, kwargs, model_key)
    except Exception as e:  # noqa: BLE001 — botocore ClientError; anything else is re-raised below
        tool_choice = (kwargs.get("toolConfig") or {}).get("toolChoice") or {}
        if not schema or "auto" in tool_choice or not _tool_choice_rejected(e):
            raise
        logger.warning("converse_tool_choice_rejected", model=model_key, error=str(e)[:200],
                       action="retry_with_auto_tool_choice; add the model to _NO_FORCED_TOOL_CHOICE_MARKERS")
        _FORCED_TOOL_REJECTED.add(model_key)
        _use_auto_tool_choice(kwargs, schema)
        return _converse_dropping_rejected_temperature(call, kwargs, model_key)


def _temperature_rejected(exc: BaseException) -> bool:
    """AA-801: Bedrock's answer to a temperature the model no longer takes, e.g. Claude Sonnet 5:
    "ValidationException ... temperature is deprecated for this model"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "validationexception" in text and "temperature" in text and any(
        w in text for w in ("deprecated", "not supported", "unsupported", "not allowed"))


def _converse_dropping_rejected_temperature(call, kwargs: dict, model_key: str):
    """AA-801: one Converse call; when Bedrock rejects the temperature we sent (the catalog row still
    says `supports_temperature`), retry ONCE without it and drop it from `kwargs` for any follow-up
    call. A ValidationException is not billed, so the retry costs nothing extra. The real fix is the
    catalog flag (migration 227 for sonnet-5); this keeps a stale flag from failing every call."""
    try:
        return _converse_call(call, kwargs, model_key)
    except Exception as e:  # noqa: BLE001 — botocore ClientError; anything else is re-raised below
        inference = kwargs.get("inferenceConfig") or {}
        if "temperature" not in inference or not _temperature_rejected(e):
            raise
        logger.warning("converse_temperature_rejected", model=model_key, error=str(e)[:200],
                       action="retry_without_temperature; set supports_temperature=false in the catalog")
        kwargs["inferenceConfig"] = {k: v for k, v in inference.items() if k != "temperature"}
        return _converse_call(call, kwargs, model_key)


def _bedrock_request_id(resp) -> Optional[str]:
    """AA-804: the Bedrock RequestId of a Converse / ConverseStream reply (ResponseMetadata), when there is one."""
    meta = resp.get("ResponseMetadata") if isinstance(resp, dict) else None
    rid = meta.get("RequestId") if isinstance(meta, dict) else None
    return rid if isinstance(rid, str) and rid else None


def _reasoning_tokens(usage) -> Optional[int]:
    """AA-804: OpenAI usage.completion_tokens_details.reasoning_tokens (an int), else None."""
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _billed_usage_of(exc: BaseException) -> list:
    """The billed attempts carried by `exc` or anything in its cause / context chain (the legacy chain
    wraps a T3 OpenAI failure in its own RuntimeError)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        billed = getattr(exc, "billed_usage", None)
        if billed:
            return list(billed)
        exc = exc.__cause__ or exc.__context__
    return []


def log_gateway_billed_failure(request: LLMRequest, *, role: str, billed: list[dict], error: str,
                               extra: Optional[dict] = None) -> None:
    """AA-927: the gateway's own llm_call_log rows for billed-but-unusable attempts (see
    `LLMClient._call_key_logged`): one row per billed attempt, `gateway_attempt: true`,
    `billed_unusable: true`, no ok-rate key. Written fire-and-forget on a background thread so a slow or
    down database never delays the route's next model. Module-level so the AA-729 harness can see it.
    Never raises."""
    from .call_log import _in_background, billed_unusable_rows, record_calls
    try:
        rows = []
        for b in billed:
            rows += billed_unusable_rows(
                stage=request.stage or "unstaged", role=role, billed=[b], error=error,
                quality_signal_extra={**(extra or {}), "gateway_attempt": True, "model": b.get("model"),
                                      "account": b.get("account"),
                                      **({"provider_requests": b["provider_requests"]}
                                         if (b.get("provider_requests") or 1) > 1 else {})})
        if rows:
            _in_background(record_calls, {"rows": rows})
    except Exception as e:  # noqa: BLE001 — a log write never breaks the call it records
        logger.warning("llm_gateway_billed_failure_log_failed", stage=request.stage, error=str(e)[:200])


# ── AA-804 (ledger v2): one llm_call_log row per attempt ─────────────────────────────────────────────────────────
#
# Callers keep writing the row of the reply they get (one per success, `**billed_fields(resp)` now also carries the
# attempt's latency, position, wire id, request id, effort, reasoning tokens and prompt hash). The gateway writes the
# rows nobody else can: a billed-but-unusable attempt (AA-927, unchanged) and, new, every attempt that was refused or
# failed with nothing billed — cost 0, status refused | error, error_class, latency, connection, wire id, attempt
# number. Each provider attempt runs through `LLMClient._attempt` (one level per path: `_call_target` for the
# deployment plan, alias steps and the admin probe; the legacy chain's tiers; the catalog paths without a plan); a
# failure the gateway raised before any attempt (model not in the catalog, every deployment behind an open circuit or
# over its budget) gets its row from the `_call_key_logged` / `probe` net. A logged exception is marked, so an
# aggregate error raised over it (`_all_failed`) is never logged twice. Written fire-and-forget on a background thread,
# like the billed-failure rows: a slow or down database never delays the route's next model.


@dataclass
class _AttemptScope:
    """The ledger context of one gateway request (generate, a shadow call, the admin probe): its role, the route
    position being tried, extra quality_signal keys and the running attempt counter."""
    role: str = "writer"
    position: int = 0
    extra: dict = field(default_factory=dict)
    attempts: int = 0
    prompt_hash: Optional[str] = None
    prompt_version: Optional[str] = None      # AA-900: the stage's registered prompt version (prompt_registry)

    def next_attempt(self) -> int:
        self.attempts += 1
        return self.attempts


_ATTEMPT_SCOPE: ContextVar[Optional[_AttemptScope]] = ContextVar("llm_attempt_scope", default=None)
_LEDGERED_ATTR = "_aa804_ledgered"


def prompt_hash(request: LLMRequest) -> Optional[str]:
    """sha256 hex of the request's system prompt (llm_call_log.prompt_hash; never the prompt itself). AA-1018 part 2:
    the system prompt only (S262 plan A4), so every call that sends one prompt version shares one hash whatever the
    tour; the user prompt (the tour's data) is left out. None when it is not text (a test double)."""
    system = getattr(request, "system_prompt", None)
    if not isinstance(system, str):
        return None
    return hashlib.sha256(system.encode("utf-8")).hexdigest()


def _scope_for(request: LLMRequest, role: str, extra: Optional[dict] = None) -> _AttemptScope:
    return _AttemptScope(role=role or "writer", extra=dict(extra or {}), prompt_hash=prompt_hash(request),
                         prompt_version=prompt_registry.version_for(getattr(request, "stage", None)))


def _ledgered(exc: BaseException) -> bool:
    """True when `exc` or anything in its cause / context chain already has its ledger row."""
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, _LEDGERED_ATTR, False):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _mark_ledgered(exc: BaseException) -> None:
    try:
        setattr(exc, _LEDGERED_ATTR, True)
    except Exception:  # noqa: BLE001 — an exception type without __dict__: at worst a second row
        pass


def attempt_outcome(exc: BaseException) -> tuple[str, str]:
    """(status, error_class) of an attempt that raised with nothing billed. 'refused' when the provider or the gateway
    said no (access denied, no credit, a missing / rejected key, a rate limit, an open circuit, the connection's own
    limiter, a budget, a data-scope block); 'error' otherwise (outage, timeout, a bad reply that was not billed).
    error_class = the breaker class (a request error counts as 'other'), or 'budget'. AA-1018 part 2: a route deadline
    is ('error', 'deadline'); a refusal to return a reply cut at its token limit is ('truncated', 'truncated')."""
    from shared.cost_guard import BudgetExceeded
    if isinstance(exc, BudgetExceeded) or any(isinstance(link, BudgetExceeded) for link in _causes(exc)):
        return "refused", "budget"
    if any(isinstance(link, RouteDeadlineExceeded) for link in _causes(exc)):
        return "error", DEADLINE                       # AA-1018 part 2: the route's time ran out, nothing was sent
    if classify(exc) == TRUNCATED:
        # AA-1018 part 2: the provider refused a reply cut at its token limit (OpenAI 400): the request asked for too
        # few tokens; recorded as truncated, like a cut reply that came back.
        return "truncated", TRUNCATED
    cls = breaker.failure_class(exc)
    if cls not in ("access_denied", "quota_exhausted", "throttled", "credential"):
        cls = OTHER
    refused = cls != OTHER or isinstance(exc, (breaker.CircuitOpen, breaker.LimiterSaturated, OpenAIPlatformBlocked))
    return ("refused" if refused else "error"), cls


def _causes(exc: BaseException) -> list:
    out, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _provider_of(conn: Optional[connections.ProviderConnection]) -> Optional[str]:
    """The llm_call_log provider of a call on `conn` (the values call_log derives from a reply's label)."""
    if conn is None:
        return None
    if conn.kind == "bedrock":
        return "bedrock-native" if conn.auth_mode == "task_role" else "bedrock-satellite"
    return conn.kind


def log_gateway_failed_attempt(request: LLMRequest, exc: BaseException, *, model: str,
                               connection_key: Optional[str] = None, wire_id: Optional[str] = None,
                               provider: Optional[str] = None, latency_ms: Optional[int] = None,
                               attempt_no: Optional[int] = None, attempt: bool = False) -> None:
    """AA-804: the gateway's llm_call_log row for one attempt that was refused or failed with nothing billed (see the
    block comment above). A billed attempt (BilledUnusableReply anywhere in the chain) is not logged here: its
    billed_unusable row is written by `_call_key_logged` / `probe`. ``attempt`` = a real provider attempt
    (`LLMClient._attempt`): always logged, even when the provider raised the same exception object before; otherwise
    (the net) nothing is written when the failure is already covered. Marks `exc`. Never raises."""
    if _billed_usage_of(exc) or (not attempt and _ledgered(exc)):
        return
    _mark_ledgered(exc)
    from .call_log import _in_background, gateway_failure_row, record_calls
    try:
        scope = _ATTEMPT_SCOPE.get() or _scope_for(request, "writer")
        status, error_class = attempt_outcome(exc)
        row = gateway_failure_row(
            stage=request.stage or "unstaged", role=scope.role, model=model, status=status, error_class=error_class,
            error=f"{type(exc).__name__}: {exc}", provider=provider, connection_key=connection_key,
            latency_ms=latency_ms, attempt_no=attempt_no, deployment_wire_id=wire_id, prompt_hash=scope.prompt_hash,
            prompt_version=scope.prompt_version, is_shadow=bool(scope.extra.get("shadow")),
            quality_signal_extra={**scope.extra, "position": scope.position})
        _in_background(record_calls, {"rows": [row]})
    except Exception as e:  # noqa: BLE001 — a log write never breaks the call it records
        logger.warning("llm_gateway_failed_attempt_log_failed", stage=request.stage, error=str(e)[:200])


def _enrich_billed(exc: BaseException, facts: dict) -> None:
    """Add the attempt's facts (latency, attempt number, wire id, connection) to the billed records an unusable reply
    carries, so its billed_unusable row has them too. Keeps a value already set."""
    for b in _billed_usage_of(exc):
        if isinstance(b, dict):
            for k, v in facts.items():
                if v is not None and b.get(k) is None:
                    b[k] = v


class LLMClient:
    """
    Legacy haiku/sonnet chain (acc3 is the only satellite account — AA-998 removed the
    cross-account fallback tiers T1.5b/T2.5b; an acc3 failure degrades Sonnet to acc3 Haiku,
    then raises, and the route's next model or the job retry takes over):
    T1:    Claude Sonnet 4.5 (Bedrock, acc2 native) + prompt caching — AA-843: only when
                                 LLM_LEGACY_ACC2_NATIVE is on (default off; acc2 always refuses)
    T1.5a: Claude Sonnet     (Bedrock satellite, acc3)
    T2:    Claude Haiku 4.5  (Bedrock, acc2 native) + prompt caching — AA-843: same flag as T1
    T2.5a: Claude Haiku      (Bedrock satellite, acc3)
    T3:    GPT-4.1           (OpenAI platform API) — S235: retired; runs only if the gpt-4.1
                                 catalog row is enabled again (openai_platform_allowed)
    """

    def __init__(self):
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=Config(
                read_timeout=300,
                connect_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        # AA-1002 (S9): no OpenAI client is built here any more. Each call gets the client of its connection
        # (adapters/openai_chat.client_for: the connection's secret, else its allowlisted credential env; its endpoint,
        # timeout and retries). An object set here (tests, a one-off script) is used for every OpenAI-format call.
        self._openai = None

    def generate(self, request: LLMRequest) -> LLMResponse:
        # AA-637 — report call boundaries to a bound live-progress sink (no-op without one).
        stream_sink.emit_call_start(request.stage)
        ok = False
        try:
            resp = self._generate(request)
            ok = True
            return resp
        finally:
            stream_sink.emit_call_end(request.stage, ok)

    def _attempt(self, request: LLMRequest, call, *, model: str, connection_key: Optional[str] = None,
                 wire_id: Optional[str] = None, provider: Optional[str] = None) -> LLMResponse:
        """AA-804 (ledger v2): ONE provider attempt (``call()``), timed and numbered in the request's attempt scope. A
        reply gets ``latency_ms`` / ``attempt_no`` / ``deployment_wire_id`` / ``prompt_hash`` (the caller's row carries
        them through billed_fields); a billed unusable reply gets them on its billed records; any other failure gets
        the gateway's 0 USD row (``log_gateway_failed_attempt``). The exception is re-raised unchanged."""
        scope = _ATTEMPT_SCOPE.get()
        attempt_no = scope.next_attempt() if scope is not None else None
        started = time.monotonic()
        try:
            resp = call()
        except Exception as e:
            latency = int((time.monotonic() - started) * 1000)
            if _billed_usage_of(e):
                _enrich_billed(e, {"latency_ms": latency, "attempt_no": attempt_no, "deployment_wire_id": wire_id,
                                   "connection_key": connection_key,
                                   "prompt_hash": scope.prompt_hash if scope is not None else None,
                                   "prompt_version": scope.prompt_version if scope is not None else None})
            else:
                log_gateway_failed_attempt(request, e, model=model, connection_key=connection_key, wire_id=wire_id,
                                           provider=provider, latency_ms=latency, attempt_no=attempt_no,
                                           attempt=True)
            raise
        try:
            resp.latency_ms = int((time.monotonic() - started) * 1000)
            resp.attempt_no = attempt_no
            if wire_id and not resp.deployment_wire_id:
                resp.deployment_wire_id = wire_id
            if scope is not None and not resp.prompt_hash:
                resp.prompt_hash = scope.prompt_hash
            if scope is not None and not resp.prompt_version:
                resp.prompt_version = scope.prompt_version
        except Exception:  # noqa: BLE001 — a test double that is not an LLMResponse
            pass
        return resp

    def _logged_failure_net(self, request: LLMRequest, exc: BaseException, *, model: str) -> None:
        """AA-804: the row of a request the gateway refused before any provider attempt (a model not in the catalog or
        disabled, every deployment behind an open circuit or over its budget, no usable deployment). Nothing is
        written when an attempt row (or a billed row) already covers the failure."""
        if _ledgered(exc) or _billed_usage_of(exc):
            return
        log_gateway_failed_attempt(request, exc, model=model, latency_ms=0)

    def _generate(self, request: LLMRequest) -> LLMResponse:
        # AA-518 — request.stage (when set) resolves the admin's per-stage config; an explicit
        # request.model_tier still wins over it (AA-237's opt-in haiku->sonnet auto-upgrade, and
        # any other real per-request override), same relationship model_tier already had with
        # the DEFAULT_MODEL_TIER env var before this task.
        stage_cfg = get_stage_config_sync(request.stage) if request.stage else None
        if stage_cfg is None:
            return self._generate_with(request, stage_cfg)
        token = _STAGE_SCOPES.set((request.stage, data_scope.normalize(getattr(stage_cfg, "allowed_scopes", None))))
        try:
            return self._generate_with(request, stage_cfg)
        finally:
            _STAGE_SCOPES.reset(token)

    def _generate_with(self, request: LLMRequest, stage_cfg) -> LLMResponse:
        # The satellite account. AA-998: acc3 is the only one (llm_role_config.account_route is CHECKed
        # to acc3 / NULL by migration 295); any other stored value is ignored, never called.
        primary_acct = DEFAULT_SATELLITE_ACCOUNT
        if stage_cfg and stage_cfg.account_route and stage_cfg.account_route != primary_acct:
            logger.warning("llm_account_route_ignored", stage=request.stage, account_route=stage_cfg.account_route,
                           used=primary_acct)

        # AA-659 / ADR 0006 — the stage route is model_id + fallback_model_ids. An explicit
        # request.model_tier still wins and runs alone (no route, no shadow).
        if request.model_tier:
            chain = [request.model_tier]
        elif stage_cfg:
            chain = [stage_cfg.model_id, *stage_cfg.fallback_model_ids]
        else:
            chain = [DEFAULT_MODEL_TIER]

        role = _role_of(request, stage_cfg)
        # AA-804 (ledger v2): the attempt scope of this request (role, attempt counter, prompt hash).
        scope_token = _ATTEMPT_SCOPE.set(_scope_for(request, role))
        # AA-1018 part 2: the stage's route deadline (None = no deadline: nothing bound, nothing changes).
        deadline_s = getattr(stage_cfg, "route_deadline_s", None) if stage_cfg else None
        if not isinstance(deadline_s, int) or isinstance(deadline_s, bool) or deadline_s <= 0:
            deadline_s = None                   # a test double or a bad value: no deadline
        deadline_token = _DEADLINE.set((time.monotonic() + deadline_s, deadline_s)) if deadline_s else None
        try:
            # AA-1016: the tenant's monthly AI budget, before any attempt (no budget row = nothing read, no change).
            self._tenant_budget_gate(request, chain[0])
            if len(chain) == 1:
                try:
                    resp = self._call_key_logged(request, chain[0], primary_acct, role=role, position=0)
                except (LLMAccessDenied, UnusableReply):
                    raise
                except Exception as e:
                    # AA-900: a single-model request refused at account level raises the typed error (no retry helps).
                    if account_level_class([classify(e)]):
                        raise _all_failed(f"{chain[0]} refused for stage {request.stage!r}: {str(e)[:300]}",
                                          [e]) from e
                    raise
            else:
                resp = self._call_route(request, chain, primary_acct, role=role)
        finally:
            if deadline_token is not None:
                _DEADLINE.reset(deadline_token)
            _ATTEMPT_SCOPE.reset(scope_token)

        # AA-919: no A/B shadow call inside a writing-memory eval run (call_log.bind_eval_stage): it would add spend
        # and llm_shadow_log rows for an eval, not production traffic (cost-cuts K2, "shadow_sample_pct = 0").
        if (stage_cfg and not request.model_tier and stage_cfg.shadow_model_id
                and random.random() * 100 < stage_cfg.shadow_sample_pct and not _in_eval_run()):
            self._start_shadow(request, stage_cfg, resp, primary_acct)
        return resp

    def _tenant_budget_gate(self, request: LLMRequest, first_key: str) -> None:
        """AA-1016: ``tenant_budget.check`` for the tenant bound to this call (the job's tenant). A refusal (a T-stage
        call of a tenant past its approval threshold with hard_stop on) gets its 0 USD 'refused' / 'budget' ledger row
        and is raised as is: the job ends ``stopped_budget`` and waits for staff (``tenant_budget`` module)."""
        try:
            tenant_budget.check(request.stage)
        except tenant_budget.TenantBudgetApprovalRequired as e:
            pin = split_pin(first_key)
            log_gateway_failed_attempt(request, e, model=pin[0] if pin else first_key, latency_ms=0)
            raise

    def _call_key(self, request: LLMRequest, key: str, primary_acct: str) -> LLMResponse:
        # AA-658 / ADR 0005 — non-legacy Model Keys resolve through the catalog, with no hidden
        # fallback; a stage that wants one lists it in its route (AA-659).
        if key not in LEGACY_KEYS:
            return self._call_catalog_model(request, key, primary_acct)
        if request.json_schema and key != "gpt-4.1":
            # S251: never lose the schema on a legacy Claude key (gpt-4.1 enforces it through response_format).
            return self._legacy_structured(request, key, primary_acct)
        return self._legacy_call(request, key, primary_acct)

    def _legacy_call(self, request: LLMRequest, tier: str, primary_acct: str) -> LLMResponse:
        """AA-1002 (S3): a legacy key through its alias (LLM_LEGACY_ALIASES on, an alias row and a registry that
        describes every target), else the hard-coded _legacy_chain, unchanged (the rollback path)."""
        steps = self._alias_steps(tier, stage_scopes(request)) if legacy_aliases_enabled() else None
        if steps is None:
            return self._legacy_chain(request, tier, primary_acct)
        return self._run_alias(request, tier, steps)

    def _alias_steps(self, tier: str, allowed_scopes: Optional[tuple] = None) -> Optional[list]:
        """The (catalog model, deployment) attempts of legacy key ``tier``, in order: each alias target (shared.
        llm_model_alias) on each of its deployments (vendor_route.deployment_plan). None = take _legacy_chain: gpt-4.1,
        acc2-native on (its order is the chain's own), no alias row, or a target the catalog / registry cannot describe
        (all or nothing: never half an alias). A disabled catalog target is skipped (the catalog is the switch)."""
        if tier == "gpt-4.1" or legacy_acc2_native_enabled():
            return None
        targets = alias_targets_sync(tier)
        if not targets:
            return None
        steps = _AliasSteps()
        for key in targets:
            model = get_model_sync(key)
            if model is None:
                logger.warning("llm_alias_fallback_legacy_chain", alias=tier, target=key, reason="not in catalog")
                return None
            if not model.enabled:
                logger.info("llm_alias_target_skip", alias=tier, target=key, reason=model.blocked_reason or "disabled")
                continue
            plan = deployment_plan(model, allowed_scopes)
            if plan is None:
                logger.warning("llm_alias_fallback_legacy_chain", alias=tier, target=key, reason="no registry rows")
                return None
            skips = getattr(plan, "circuit_skips", None)
            if not plan and skips:
                # AA-1002 (S4): every deployment of this target is behind an open circuit. Kept as the target's
                # failure, so an alias whose targets are all refused at account level still ends in LLMAccessDenied.
                logger.info("llm_alias_target_skip", alias=tier, target=key, reason="circuit open")
                steps.circuit_errors.append(breaker.circuit_error(model.model_key, skips))
            elif not plan and getattr(plan, "budget_skips", None):
                # AA-1002 (S10): every connection of this target reached its hard monthly budget.
                logger.info("llm_alias_target_skip", alias=tier, target=key, reason="monthly budget reached")
                steps.circuit_errors.append(connection_budget.budget_error(model.model_key, plan.budget_skips))
            steps += [(model, target) for target in plan]
        return steps

    def _run_alias(self, request: LLMRequest, tier: str, steps: list) -> LLMResponse:
        """The legacy chain's contract on alias steps: the first that answers wins; `fallback_used` when a later target
        model answered (Sonnet degraded to Haiku); every step down = the chain's own final error (incl. the S235
        GPT-4.1 tail, which only runs if that retired catalog row is enabled again)."""
        failures: list = list(getattr(steps, "circuit_errors", ()))   # targets not called: circuit open (S4)
        intended = (alias_targets_sync(tier) or (None,))[0]     # the alias's first model, even if it had no step
        for position, (model, target) in enumerate(steps):
            if position > 0:
                where = f"{model.model_key}@{target.connection.connection_key}"
                self._deadline_gate(request, model.model_key, f"alias {tier} step {position} ({where})", failures)
            try:
                resp = self._call_target(request, model, target)
            except (UnusableReply, RouteDeadlineExceeded):
                raise
            except Exception as e:
                failures.append(e)
                logger.warning("llm_alias_step_failed", alias=tier, model=model.model_key,
                               connection=target.connection.connection_key, position=position, error=str(e)[:300])
                continue
            resp.fallback_used = model.model_key != intended
            logger.info("llm_alias_used", alias=tier, model=model.model_key,
                        connection=target.connection.connection_key, position=position)
            return resp
        return self._legacy_last_resort(request, tier, failures)

    def _legacy_structured(self, request: LLMRequest, key: str, primary_acct: str) -> LLMResponse:
        """S251 (AA-835): a json_schema request on a legacy haiku/sonnet key. The Messages API path has no structured
        output, so (a) the system prompt gets an explicit "one JSON object matching this schema" instruction with the
        schema, and (b) the reply's JSON object is taken (a ``` fence is tolerated, prose is not) and conformed with
        `_checked_json`. An unusable reply raises BilledUnusableReply carrying the billed usage, so
        `_call_key_logged` writes its billed_unusable llm_call_log row and a route moves to its next model, exactly as
        on the Converse path. A request without json_schema never comes here (byte-identical to before)."""
        schema = request.json_schema
        stage = request.stage or "unstaged"
        if stage not in _LEGACY_SCHEMA_WARNED:
            _LEGACY_SCHEMA_WARNED.add(stage)
            logger.warning("llm_legacy_key_structured_output", stage=stage, model=key, schema=schema["name"],
                           action="schema asked by prompt only; route this stage to a catalog (Converse) key")
        asked = request.model_copy(
            update={"system_prompt": f"{request.system_prompt}\n\n{_legacy_schema_instruction(schema)}"})
        resp = self._legacy_call(asked, key, primary_acct)
        if not resp.content and not (resp.input_tokens or resp.output_tokens):
            # Nothing billed (same rule as the Converse path): a plain failure, no billed_unusable row.
            raise RuntimeError(f"{resp.model_used} returned empty content for schema {schema['name']!r}")
        try:
            parsed = _json_object_in_text(resp.content)
            if parsed is None:
                raise RuntimeError(f"{resp.model_used} returned no JSON object for schema {schema['name']!r}")
            resp.content = _checked_json(resp.model_used, parsed, schema)
        except RuntimeError as e:
            # The same billing fields a caller's record_unusable_sync(resp) would write for this reply.
            from .call_log import billed_record
            raise _billed_unusable(str(e), [billed_record(resp)]) from e
        return resp

    def _call_key_logged(self, request: LLMRequest, key: str, primary_acct: str, *,
                         role: str, position: int, extra: Optional[dict] = None) -> LLMResponse:
        """AA-927: `_call_key`, and when the attempt raised AFTER the provider billed it
        (BilledUnusableReply), write that attempt's llm_call_log row here before re-raising — the caller
        only ever sees the reply that came back, so no one else can. One row per billed attempt,
        `billed_unusable: true`, no ok-rate key (call_log.OK_RATE_KEYS)."""
        scope = _ATTEMPT_SCOPE.get()
        if scope is not None:
            scope.position = position          # AA-804: the route position on this key's attempt rows
        try:
            return self._call_key(request, key, primary_acct)
        except Exception as e:
            billed = _billed_usage_of(e)
            if billed:
                log_gateway_billed_failure(request, role=role, billed=billed, error=str(e),
                                           extra={"position": position, **(extra or {})})
            else:
                self._logged_failure_net(request, e, model=split_pin(key)[0] if split_pin(key) else key)
            raise

    def _call_route(self, request: LLMRequest, chain: list, primary_acct: str,
                    role: str = "writer") -> LLMResponse:
        errors = []
        # S251: the billed usage of attempts whose reply was unusable; an attempt that failed any other way (outage,
        # throttle, blocked) clears `all_unusable`: the final error is RouteUnusableReply only when no model was down.
        unusable_usage: list = []
        all_unusable = True
        failures: list = []  # AA-900: one exception per model actually tried (skipped keys are not attempts)
        for position, key in enumerate(chain):
            if key not in LEGACY_KEYS:
                m = get_model_sync(key)
                if m is None or not m.enabled:
                    reason = "not in catalog" if m is None else (m.blocked_reason or "disabled")
                    logger.info("llm_route_skip", stage=request.stage, model=key, reason=reason)
                    errors.append(f"{key}: skipped ({reason})")
                    continue
            if position > 0:
                self._deadline_gate(request, key, f"route position {position} ({key})", errors)
            try:
                resp = self._call_key_logged(request, key, primary_acct, role=role,
                                             position=position)
            except RouteDeadlineExceeded:
                raise
            except Exception as e:
                logger.warning("llm_route_attempt_failed", stage=request.stage, model=key,
                               position=position, error=str(e)[:300])
                errors.append(f"{key}: {str(e)[:200]}")
                failures.append(e)
                billed = _billed_usage_of(e) if isinstance(e, UnusableReply) else []
                if billed:
                    unusable_usage.extend(billed)
                else:
                    all_unusable = False
                continue
            if position > 0:
                resp.fallback_used = True
            if resp.provider in OPENAI_FORMAT_PROVIDERS:
                # S235 — the OpenAI platform API is a last resort for a Bedrock outage: tenant
                # content left AWS. Fixed event name for a CloudWatch metric filter / alarm
                # (proposed in docs/implementation-notes/S235-bedrock-only.md); llm_call_log gets
                # provider='openai' + fallback_used=true from the caller's record_call.
                logger.warning("llm_openai_fallback_used", stage=request.stage, model=key,
                               position=position, errors=errors)
            logger.info("llm_route_used", stage=request.stage, model=key, position=position)
            return resp
        message = f"All models in the route failed for stage {request.stage!r}: " + "; ".join(errors)
        if all_unusable and unusable_usage:
            exc = RouteUnusableReply(message)
            exc.logged_usage = tuple(unusable_usage)
            raise exc
        raise _all_failed(message, failures)

    def _deadline_gate(self, request: LLMRequest, model: str, before: str, errors: Optional[list] = None) -> None:
        """AA-1018 part 2: ``check_deadline`` before a later attempt; when it passed, the request's 0 USD 'deadline'
        ledger row (model = the attempt not started) and RouteDeadlineExceeded naming what failed so far."""
        try:
            check_deadline(request, before)
        except RouteDeadlineExceeded as e:
            if errors:
                e.args = (f"{e.args[0]}; tried: " + "; ".join(str(x) for x in errors)[:600],)
            logger.warning("llm_route_deadline_passed", stage=request.stage, model=model, before=before)
            log_gateway_failed_attempt(request, e, model=split_pin(model)[0] if split_pin(model) else model,
                                       latency_ms=0)
            raise

    def _start_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                      primary_acct: str) -> None:
        # A plain thread does not inherit contextvars, so the shadow never writes to the
        # live-progress stream sink bound to the primary call. The job id and tenant ARE carried
        # over explicitly (AA-741 G1) so the shadow's llm_call_log row is attributed like the
        # primary's.
        from .call_log import current_job_id, current_tenant_id
        threading.Thread(
            target=self._run_shadow,
            args=(request.model_copy(), cfg, primary, primary_acct,
                  current_job_id(), current_tenant_id()),
            daemon=True,
        ).start()

    def _run_shadow(self, request: LLMRequest, cfg, primary: LLMResponse,
                    primary_acct: str,
                    job_id: Optional[str] = None, tenant_id: Optional[str] = None) -> None:
        from .call_log import bind_job, bind_tenant
        with bind_job(job_id), bind_tenant(tenant_id):
            self._run_shadow_bound(request, cfg, primary, primary_acct)

    def _run_shadow_bound(self, request: LLMRequest, cfg, primary: LLMResponse,
                          primary_acct: str) -> None:
        from .shadow import record_shadow_sync
        t0 = time.monotonic()
        resp, error = None, None
        token = _STAGE_SCOPES.set((request.stage, data_scope.normalize(getattr(cfg, "allowed_scopes", None))))
        scope_token = _ATTEMPT_SCOPE.set(_scope_for(request, cfg.role or "writer", {"shadow": True}))
        try:
            resp = self._call_key_logged(request, cfg.shadow_model_id, primary_acct,
                                         role=cfg.role or "writer", position=0, extra={"shadow": True})
        except Exception as e:
            error = str(e)[:1000]
        finally:
            _ATTEMPT_SCOPE.reset(scope_token)
            _STAGE_SCOPES.reset(token)
        record_shadow_sync(
            stage=cfg.stage, role=cfg.role, request=request, primary=primary,
            shadow_model=cfg.shadow_model_id, shadow=resp, error=error,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    def _legacy_chain(self, request: LLMRequest, tier: str, primary_acct: str) -> LLMResponse:
        # AA-1018 part 2 (D4 in part): every tier runs inside breaker.guarded (_legacy_tier), and the path is logged as
        # deprecated (log_deprecated_path); its replacement is the alias path (_run_alias).
        log_deprecated_path("legacy_chain", request.stage)
        # Direct GPT-4.1 — no Bedrock fallback (explicit choice)
        if tier == "gpt-4.1":
            # AA-1002 (S10): no plan on this path, so the connection's hard monthly budget is checked here (a budget
            # stop, raised as is: not wrapped into the outage error below).
            connection_budget.check(OPENAI_CONNECTION_KEY, "gpt-4.1")
            gpt_conn = connections.connection_or_builtin(OPENAI_CONNECTION_KEY)
            try:
                return self._attempt(
                    request, lambda: _guarded(gpt_conn, "gpt-4.1", lambda: self._call_openai(request, model="gpt-4.1")),
                    model="gpt-4.1", connection_key=OPENAI_CONNECTION_KEY, wire_id="gpt-4.1", provider="openai")
            except Exception as e:
                logger.error("gpt41_direct_failed", error=str(e))
                raise RuntimeError(f"GPT-4.1 failed: {e}") from e

        # AA-843 — acc2-native (T1/T2) only behind LLM_LEGACY_ACC2_NATIVE; by default the chain
        # starts at the primary satellite. Event names stay the same; only `reason` says why the
        # satellite was the first stop.
        acc2_native = legacy_acc2_native_enabled()
        failures: list = []  # AA-900: every tier that was tried and failed, for the final error

        if tier == "sonnet":
            # T1: Claude Sonnet — premium quality (acc2-native, off by default)
            if acc2_native:
                try:
                    resp = self._legacy_tier(request, tier, ACC2_CONNECTION_KEY, BEDROCK_SONNET,
                                             lambda: self._call_bedrock(request, model=BEDROCK_SONNET, use_cache=True))
                    return resp
                except RouteDeadlineExceeded:
                    raise
                except Exception as e:
                    failures.append(e)
                    if "AccessDeniedException" in str(e) or "not authorized" in str(e).lower():
                        logger.warning("t1_sonnet_not_subscribed", model=BEDROCK_SONNET,
                                       hint="Enable cross-region inference profile in AWS Marketplace (AA-50)")
                    else:
                        logger.warning("t1_failed_trying_t2", model=BEDROCK_SONNET, error=str(e))

            # T1.5a: Claude Sonnet on the acc3 satellite
            try:
                resp = self._legacy_tier(request, tier, primary_acct, BEDROCK_SONNET,
                                         lambda: self._call_bedrock_satellite(request, model=BEDROCK_SONNET,
                                                                              account=primary_acct))
                resp.fallback_used = False    # KHÔNG phải fallback — vẫn đúng Sonnet, đúng ý định
                resp.satellite_account = primary_acct
                logger.info("t1_5a_satellite_used", model=BEDROCK_SONNET, account=primary_acct,
                            reason="acc2 T1 failed" if acc2_native else "acc2 native off (AA-843)")
                return resp
            except RouteDeadlineExceeded:
                raise
            except Exception as e:
                failures.append(e)
                # AA-998: no cross-account retry; Sonnet degrades to Haiku on the same account.
                logger.warning("t1_5a_satellite_failed_trying_t2", model=BEDROCK_SONNET,
                               account=primary_acct, error=str(e))

        # T2: Claude Haiku — fast / default tier, or Sonnet fallback (acc2-native, off by default)
        if acc2_native:
            try:
                resp = self._legacy_tier(request, tier, ACC2_CONNECTION_KEY, BEDROCK_HAIKU,
                                         lambda: self._call_bedrock(request, model=BEDROCK_HAIKU, use_cache=True))
                resp.fallback_used = tier == "sonnet"  # only a fallback when Sonnet was intended
                return resp
            except RouteDeadlineExceeded:
                raise
            except Exception as e:
                failures.append(e)
                logger.warning("t2_failed_trying_t2_5a", model=BEDROCK_HAIKU, error=str(e))

        # T2.5a: Claude Haiku on the acc3 satellite
        try:
            # AA-1002 (S10): the legacy chain has no deployment plan, so each tier checks its connection's hard monthly
            # budget before the call. A tier over budget is a failure like any other (the chain moves on); when every
            # tier was, _all_failed makes it a budget stop (the job ends as stopped_budget, nothing was sent).
            resp = self._legacy_tier(request, tier, primary_acct, BEDROCK_HAIKU,
                                     lambda: self._call_bedrock_satellite(request, model=BEDROCK_HAIKU,
                                                                          account=primary_acct))
            # giữ đúng logic gốc T2: chỉ coi là fallback nếu ý định ban đầu là sonnet
            resp.fallback_used = tier == "sonnet"
            resp.satellite_account = primary_acct
            logger.info("t2_5a_satellite_used", model=BEDROCK_HAIKU, account=primary_acct,
                        reason="acc2 T2 failed" if acc2_native else "acc2 native off (AA-843)")
            return resp
        except RouteDeadlineExceeded:
            raise
        except Exception as e:
            failures.append(e)
            logger.warning("t2_5a_satellite_failed_trying_t3", model=BEDROCK_HAIKU,
                           account=primary_acct, error=str(e))

        return self._legacy_last_resort(request, tier, failures)

    def _legacy_tier(self, request: LLMRequest, tier: str, connection_key: str, wire_id: str, call) -> LLMResponse:
        """One tier of the legacy chain as one ledger attempt (AA-804): the connection's hard monthly budget check
        (AA-1002 S10, a budget refusal is a 0 USD 'refused' row), then the call. AA-1018 part 2: a later tier first
        checks the route deadline, and the call runs inside breaker.guarded on the tier's connection and the catalog
        key it really calls (an open connection or deployment circuit skips it without a call, like a plan)."""
        scope = _ATTEMPT_SCOPE.get()
        if scope is not None and scope.attempts > 0:
            self._deadline_gate(request, tier, f"legacy tier {wire_id} on {connection_key}")
        conn = connections.connection_or_builtin(connection_key)
        breaker_key = _LEGACY_BREAKER_KEY.get(wire_id, tier)

        def run() -> LLMResponse:
            connection_budget.check(connection_key, tier)     # outside the breaker: a budget stop is not a failure
            return _guarded(conn, breaker_key, call)
        provider = ("openai" if connection_key == OPENAI_CONNECTION_KEY
                    else "bedrock-native" if connection_key == ACC2_CONNECTION_KEY else "bedrock-satellite")
        return self._attempt(request, run, model=tier, connection_key=connection_key, wire_id=wire_id,
                             provider=provider)

    def _legacy_last_resort(self, request: LLMRequest, tier: str, failures: list) -> LLMResponse:
        # T3: GPT-4.1 — last resort for all tiers. S235: GPT-4.1 is retired (migration 224 disables its catalog row),
        # so a Bedrock outage on a Haiku/Sonnet writer raises here. Writers are Anthropic; an OpenAI last resort for a
        # writer would change vendor unseen (ADR 0005 §4) — only the judge routes list an OpenAI-API last resort,
        # explicitly. Shared by _legacy_chain and the S3 alias path (_run_alias).
        if not (openai_platform_allowed("gpt-4.1") and openai_scope_allowed(stage_scopes(request))):
            logger.error("t3_skipped_gpt41_retired", tier=tier,
                         reason="GPT-4.1 retired (S235); no OpenAI last resort for legacy tiers")
            raise _all_failed("All LLM providers failed (Bedrock tiers down; GPT-4.1 retired)", failures)
        try:
            resp = self._legacy_tier(request, "gpt-4.1", OPENAI_CONNECTION_KEY, "gpt-4.1",
                                     lambda: self._call_openai(request, model="gpt-4.1"))     # AA-1002 (S10) budget
            resp.fallback_used = True
            logger.warning("t3_fallback_used", model="gpt-4.1",
                           reason=f"T2 failed (tier={tier})")
            return resp
        except RouteDeadlineExceeded:
            raise
        except Exception as e:
            logger.error("t3_failed_all_providers_down", error=str(e))
            raise _all_failed("All LLM providers failed", [*failures, e]) from e

    def _call_bedrock(
        self, request: LLMRequest, model: str, use_cache: bool = False,
        connection_key: str = ACC2_CONNECTION_KEY,
    ) -> LLMResponse:
        """The acc2-native (task-role) Messages body on InvokeModel: the legacy T1 / T2 tiers and a task-role
        `anthropic_native` deployment. AA-642 (S0): the reply names its connection (`connection_key`, default the
        acc2 task role) so the llm_call_log row stores account='acc2' (call_log._row_account) and the monthly
        connection budget (cost_guard.connection_month_spend) sees it; satellite_account stays None (it only holds a
        satellite AWS account: generated_content.satellite_account CHECK)."""
        system = (
            build_cached_system_prompt(request.system_prompt)
            if use_cache
            else [{"type": "text", "text": request.system_prompt}]
        )
        messages = (
            build_cached_messages(
                request.few_shots if hasattr(request, "few_shots") else [],
                request.user_prompt,
            )
            if use_cache
            else [{"role": "user", "content": request.user_prompt}]
        )

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": messages,
        }
        if "temperature" in request.model_fields_set and request.temperature is not None:
            body["temperature"] = request.temperature   # S224: was silently dropped

        # AA-637 — a new provider attempt: drop any partial text a failed attempt already showed.
        stream_sink.emit_restart(request.stage)
        # S251: no live text for a structured request (the caller gets the conformed JSON), as on Converse.
        on_delta = None if request.json_schema else stream_sink.delta_callback(request.stage)
        response = self._bedrock.invoke_model_with_response_stream(
            modelId=model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )

        # AA-224: stream accumulation. Non-stream invoke_model held the HTTP connection open for
        # the full generation (default 60s read_timeout) -> Sonnet branded prompts timed out ->
        # silent fallback to Haiku. Streaming returns chunks incrementally so the socket never
        # idles past read_timeout. Usage now arrives across events: input_tokens in
        # message_start, output_tokens (cumulative) in message_delta.
        content_parts = []
        in_tok = out_tok = cache_read = cache_write = 0
        stop_reason = None
        for event in response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            ctype = chunk.get("type")
            if ctype == "message_start":
                u = chunk.get("message", {}).get("usage", {})
                in_tok      = u.get("input_tokens", 0)
                cache_read  = u.get("cache_read_input_tokens", 0) or 0
                cache_write = u.get("cache_creation_input_tokens", 0) or 0
            elif ctype == "content_block_delta":
                d = chunk.get("delta", {})
                if d.get("type") == "text_delta":
                    content_parts.append(d.get("text", ""))
                    if on_delta is not None:
                        on_delta(d.get("text", ""))
            elif ctype == "message_delta":
                out_tok = chunk.get("usage", {}).get("output_tokens", out_tok)
                # AA-493: stop_reason ("end_turn" | "max_tokens" | "stop_sequence" | ...) arrives
                # on this same event's `delta`, alongside the cumulative output_tokens above —
                # confirmed against the real streaming schema while building this fix (STEP0).
                stop_reason = chunk.get("delta", {}).get("stop_reason", stop_reason)
        content = "".join(content_parts)
        cost    = self._calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)

        logger.info("llm_success", provider="bedrock", model=model,
                    in_tokens=in_tok, out_tokens=out_tok,
                    cache_read=cache_read, cache_write=cache_write,
                    cost_usd=cost, stop_reason=stop_reason)

        return LLMResponse(
            content=content, model_used=model, provider="bedrock",
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            # AA-288: cache_read/cache_write were parsed above and logged, but discarded before
            # this fix — the caller had no way to know a cache hit/write happened at all.
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            stop_reason=stop_reason, connection_key=connection_key,
        )

    def _call_bedrock_satellite(self, request: LLMRequest, model: str,
                                account: str = DEFAULT_SATELLITE_ACCOUNT,
                                profile_id: Optional[str] = None) -> LLMResponse:
        """AA-296/397 — gọi Claude qua satellite account (acc3, the only one since AA-998),
        dùng khi acc2 không có Anthropic model (TrueIDC channel-program org chặn).
        Xem shared/llm_client/bedrock_satellite.py cho chi tiết AssumeRole chain +
        bug 2-dạng-ARN đã fix trong IAM policy.

        request.system_prompt được forward qua tham số system= của invoke_claude()
        (Anthropic Messages API "system" field riêng, không nối vào user prompt) —
        khớp với cách _call_bedrock (acc2, T1) gửi system qua build_cached_system_prompt.
        """
        from .bedrock_satellite import invoke_claude, BedrockUnavailable
        model_key = "sonnet" if model == BEDROCK_SONNET else "haiku"
        stream_sink.emit_restart(request.stage)  # AA-637
        try:
            result = invoke_claude(
                request.user_prompt,
                model=model_key,
                max_tokens=request.max_tokens,
                system=request.system_prompt,
                account=account,
                # AA-637 — only set when a live-progress sink streams this stage; None keeps the
                # exact pre-AA-637 non-streaming invoke_model request.
                # S251: never for a structured request (the caller gets the conformed JSON), as on Converse.
                on_delta=None if request.json_schema else stream_sink.delta_callback(request.stage),
                # S224: forward an explicitly requested temperature (was silently dropped).
                temperature=(request.temperature if "temperature" in request.model_fields_set else None),
                # AA-1002 (S3): the alias path passes the deployment's wire_id; None = the legacy constant.
                **({"profile_id": profile_id} if profile_id else {}),
            )
        except BedrockUnavailable as e:
            raise RuntimeError(f"Satellite Bedrock failed: {e}") from e

        in_tok = result.usage.get("input_tokens", 0)
        out_tok = result.usage.get("output_tokens", 0)
        # AA-324: previously never read here at all -- cache_read_tokens/cache_write_tokens
        # always defaulted to 0 on every satellite LLMResponse regardless of what Bedrock
        # actually returned in usage, independently of whether the request-side caching
        # (invoke_claude()'s system= wiring, fixed same task) was even working. Mirrors
        # _call_bedrock()'s (acc2-native T1) own extraction above exactly.
        cache_read  = result.usage.get("cache_read_input_tokens", 0) or 0
        cache_write = result.usage.get("cache_creation_input_tokens", 0) or 0
        cost = self._calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)

        logger.info("llm_success", provider="bedrock-satellite", model=result.model_used,
                    in_tokens=in_tok, out_tokens=out_tok,
                    cache_read=cache_read, cache_write=cache_write,
                    cost_usd=cost, latency_ms=result.latency_ms, stop_reason=result.stop_reason)

        return LLMResponse(
            content=result.text,
            model_used=f"satellite-{result.model_used}",
            provider="bedrock-satellite",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            fallback_used=False,  # set lại đúng ở generate() tuỳ ngữ cảnh gọi
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            stop_reason=result.stop_reason,
        )

    def _call_catalog_model(self, request: LLMRequest, model_key: str, preferred_acct: str) -> LLMResponse:
        # AA-1002 (S2): "<model key>@<connection>" pins one deployment (probes, connection tests; model_tier only —
        # a stage route names catalog keys, and `_call_route` skips anything the catalog does not know).
        pin = split_pin(model_key)
        if pin:
            model_key = pin[0]
        model = get_model_sync(model_key)
        if model is None:
            raise RuntimeError(f"Model {model_key!r} is not in shared.llm_model_catalog "
                               "(or the catalog is unreachable)")
        if not model.enabled:
            raise RuntimeError(f"Model {model_key!r} is disabled in the catalog: "
                               f"{model.blocked_reason or 'no reason given'}")
        # AA-1017: the stage x model parameters (shared.stage_model_params; call site -> row -> protocol default). The
        # empty table returns the same request object, so the provider request is byte-identical.
        request = stage_params.apply(request, model)
        if pin:
            pinned = pinned_deployment(model, pin[1])
            return self._attempt(request, lambda: self._call_anthropic_messages(request, model, pinned),
                                 model=model.model_key, connection_key=pinned.connection.connection_key,
                                 wire_id=pinned.deployment.wire_id, provider="anthropic")
        # AA-1002 (S3): the model's deployments, in order (vendor_route.deployment_plan). None = the registry does not
        # describe this model (or LLM_DEPLOYMENT_ROUTING=off): the S2 path below, unchanged.
        # AA-1002 (S5): only deployments on connections whose data scope the stage allows.
        scopes = stage_scopes(request)
        plan = deployment_plan(model, scopes) if deployment_routing_enabled() else None
        if plan is not None:
            return self._call_plan(request, model, plan)
        # AA-1002 (S2): an enabled deployment on an enabled Anthropic connection, first by priority, serves the
        # model through the Messages API. None (every other registry state) = today's path below, unchanged.
        # AA-1002 (S10): an Anthropic deployment left out by its connection's hard budget is named in budget_skips.
        budget_skips: list = []
        vendor = vendor_deployment_for(model, scopes, budget_skips=budget_skips)
        if vendor is not None:
            # AA-1018 part 2 (D4 in part): inside breaker.guarded like a plan target; deprecated (the plan replaces it).
            log_deprecated_path("s2_vendor_deployment", request.stage)
            return self._attempt(
                request, lambda: _guarded(vendor.connection, model.model_key,
                                          lambda: self._call_anthropic_messages(request, model, vendor),
                                          answered=(UnusableReply,)),
                model=model.model_key, connection_key=vendor.connection.connection_key,
                wire_id=vendor.deployment.wire_id, provider="anthropic")
        if model.api_style == "anthropic_messages":
            if budget_skips:
                raise connection_budget.budget_error(model.model_key, budget_skips)
            raise RuntimeError(f"Model {model_key!r} is served only by the Anthropic API and has no enabled "
                               "deployment on an enabled Anthropic connection")
        # AA-1002 (S10): the paths below call a connection without a plan, so they check its hard budget themselves.
        if model.api_style == "openai_chat":
            wire = model.wire_model or model.model_key
            log_deprecated_path("catalog_fallback_openai", request.stage)    # AA-1018 part 2 (D4 in part)
            openai_conn = connections.connection_or_builtin(OPENAI_CONNECTION_KEY)

            def openai_call() -> LLMResponse:
                connection_budget.check(OPENAI_CONNECTION_KEY, model.model_key)
                return _guarded(openai_conn, model.model_key,
                                lambda: self._call_openai(request, model=wire, catalog_model=model),
                                answered=(UnusableReply,))
            return self._attempt(request, openai_call, model=model.model_key, connection_key=OPENAI_CONNECTION_KEY,
                                 wire_id=wire, provider="openai")
        if model.api_style != "converse":
            raise RuntimeError(f"Model {model_key!r} has api_style={model.api_style!r}, "
                               "which LLMClient cannot call through the catalog path")
        account = model.account_for(preferred_acct)
        if account is None or account == "acc2":
            raise RuntimeError(f"Model {model_key!r} has no satellite account in bedrock_profile_ids")
        # An Anthropic deployment left out by its budget falls through to today's Converse call on the satellite (the
        # next way in answers), unless that connection is over its own budget too.

        log_deprecated_path("catalog_fallback_converse", request.stage)     # AA-1018 part 2 (D4 in part)
        converse_conn = connections.connection_or_builtin(account)

        def converse_call() -> LLMResponse:
            connection_budget.check(account, model.model_key)
            return _guarded(converse_conn, model.model_key,
                            lambda: self._call_bedrock_converse(request, model, account),
                            answered=(UnusableReply,))
        return self._attempt(request, converse_call, model=model.model_key, connection_key=account,
                             wire_id=(model.bedrock_profile_ids or {}).get(account), provider="bedrock-satellite")

    def _call_plan(self, request: LLMRequest, model: CatalogModel, plan: list) -> LLMResponse:
        """AA-1002 (S3): ``model`` on each deployment of its plan, in order, until one answers. Same model first: the
        route's next model is tried only after every deployment of this one failed (the caller, _call_route).

        - One deployment (every model with today's rows): its call, with its own exception, untouched.
        - An unusable reply (billed: schema mismatch, refusal, empty) is the model's answer, not an outage: it is
          raised at once (the gateway's billed_unusable row is written by _call_key_logged; the route moves on).
        - Every deployment down: _all_failed (LLMAccessDenied when every one was refused at account level)."""
        if not plan:
            skips = getattr(plan, "circuit_skips", None)
            if skips:
                # AA-1002 (S4): not called because of the breaker; the error keeps the class that opened the circuit.
                raise breaker.circuit_error(model.model_key, skips)
            budget_skips = getattr(plan, "budget_skips", None)
            if budget_skips:
                # AA-1002 (S10): every usable connection reached its hard monthly budget (a BudgetExceeded).
                raise connection_budget.budget_error(model.model_key, budget_skips)
            raise RuntimeError(f"Model {model.model_key!r} has no usable deployment (every deployment or its "
                               "connection is disabled, cannot serve it, or its circuit is open)")
        if len(plan) == 1:
            return self._call_target(request, model, plan[0])
        failures: list = []
        errors: list = []
        for position, target in enumerate(plan):
            if position > 0:
                self._deadline_gate(request, model.model_key,
                                    f"deployment {position} of {model.model_key} ({target.connection.connection_key})",
                                    errors)
            try:
                resp = self._call_target(request, model, target)
            except (UnusableReply, RouteDeadlineExceeded):
                raise
            except Exception as e:
                failures.append(e)
                errors.append(f"{target.connection.connection_key}: {str(e)[:200]}")
                logger.warning("llm_deployment_attempt_failed", stage=request.stage, model=model.model_key,
                               connection=target.connection.connection_key, position=position, error=str(e)[:300])
                continue
            if position > 0:
                logger.info("llm_deployment_fallback_used", stage=request.stage, model=model.model_key,
                            connection=target.connection.connection_key, position=position)
            return resp
        raise _all_failed(f"Every deployment of {model.model_key!r} failed: " + "; ".join(errors), failures)

    def _call_target(self, request: LLMRequest, model: CatalogModel, target: VendorDeployment, *,
                     admit: bool = True) -> LLMResponse:
        """AA-1002 (S4): one catalog model on one deployment through the breaker (breaker.guarded): a half-open circuit
        lets one probe call through (``admit``), the connection's limiter holds a slot, and the outcome is counted on
        the deployment's circuit (an unusable reply is an answer: it counts as a success). Raises the provider's own
        exception unchanged, or breaker.CircuitOpen / LimiterSaturated without a call."""
        conn = target.connection
        return self._attempt(
            request, lambda: breaker.guarded(conn, model.model_key,
                                             lambda: self._dispatch_target(request, model, target),
                                             admit_call=admit, answered=(UnusableReply,)),
            model=model.model_key, connection_key=conn.connection_key, wire_id=target.deployment.wire_id,
            provider=_provider_of(conn))

    def probe(self, request: LLMRequest, model: CatalogModel, target: VendorDeployment) -> LLMResponse:
        """AA-1002 (S4): the admin's connection test — ``model`` on one explicit deployment (which may be switched off:
        the test runs before enabling it), no stage route, no shadow, no fallback. It is the operator's half-open
        probe: an open circuit is not in the way, and the outcome is counted (a success closes the circuit). A billed
        but unusable reply writes the gateway's billed_unusable llm_call_log row (as _call_key_logged does) and is
        re-raised; an answered call is logged by the caller (stage adhoc_conn_test)."""
        # AA-804 (ledger v2): a refused or failed test writes its 0 USD attempt row (role validate).
        scope_token = _ATTEMPT_SCOPE.set(_scope_for(request, "validate",
                                                    {"connection_test": target.connection.connection_key}))
        try:
            return self._call_target(request, model, target, admit=False)
        except Exception as e:
            billed = _billed_usage_of(e)
            if billed:
                log_gateway_billed_failure(request, role="validate", billed=billed, error=str(e),
                                           extra={"connection_test": target.connection.connection_key})
            raise
        finally:
            _ATTEMPT_SCOPE.reset(scope_token)

    def _dispatch_target(self, request: LLMRequest, model: CatalogModel, target: VendorDeployment) -> LLMResponse:
        """AA-1002 (S3): one catalog model on one deployment, by connection kind and the row's api_style. AA-1017: the
        deployment's own api_style when it states one (migration 320; NULL on every row = the catalog's)."""
        dep, conn = target.deployment, target.connection
        api_style = getattr(dep, "api_style", None) or model.api_style
        if conn.kind == "anthropic":
            return self._call_anthropic_messages(request, model, target)
        if conn.kind in ("openai", "azure_openai"):
            return self._call_openai(request, model=dep.wire_id, catalog_model=model, connection=conn,
                                     deployment=dep)
        if conn.kind != "bedrock":
            raise RuntimeError(f"connection {conn.connection_key!r} ({conn.kind}) has no adapter")
        if api_style == "converse":
            if conn.auth_mode == "task_role":
                from .bedrock_satellite import native_runtime
                return self._call_bedrock_converse(request, model, conn.connection_key, profile_id=dep.wire_id,
                                                   runtime=native_runtime(conn))
            return self._call_bedrock_converse(request, model, conn.connection_key, profile_id=dep.wire_id)
        if api_style == "anthropic_native" and model.model_key in _NATIVE_PRICE_KEY:
            # The legacy Messages body (InvokeModel), exactly as _legacy_chain sends it: the satellite (T1.5a / T2.5a)
            # on an assume-role connection, acc2-native (T1 / T2) on a task-role one.
            if conn.auth_mode == "task_role":
                return self._call_bedrock(request, model=dep.wire_id, use_cache=True,
                                          connection_key=conn.connection_key)
            resp = self._call_bedrock_satellite(request, model=_NATIVE_PRICE_KEY[model.model_key],
                                                account=conn.connection_key, profile_id=dep.wire_id)
            resp.satellite_account = conn.connection_key
            return resp
        raise RuntimeError(f"Model {model.model_key!r} has api_style={api_style!r}, "
                           f"which LLMClient cannot call on a {conn.kind} connection")

    def _call_bedrock_converse(self, request: LLMRequest, model: CatalogModel, account: str,
                               profile_id: Optional[str] = None, runtime=None) -> LLMResponse:
        """AA-658 — Bedrock Converse / ConverseStream on a satellite session. One wire format for
        every vendor on Bedrock (Anthropic and OpenAI models alike). AA-1002 (S3): `profile_id` is the
        deployment's wire_id (None = the catalog's bedrock_profile_ids[account]); `runtime` a task-role
        client (acc2), else the satellite session of connection `account`."""
        from .bedrock_satellite import get_satellite_client

        profile_id = profile_id or model.bedrock_profile_ids[account]
        inference = {"maxTokens": request.max_tokens}
        if model.max_output_tokens:
            inference["maxTokens"] = min(request.max_tokens, model.max_output_tokens)
        if model.supports_temperature and "temperature" in request.model_fields_set:
            inference["temperature"] = request.temperature
        kwargs = {
            "modelId": profile_id,
            "system": [{"text": request.system_prompt}],
            "messages": [{"role": "user", "content": [{"text": request.user_prompt}]}],
            "inferenceConfig": inference,
        }
        schema = request.json_schema
        if schema:
            # AA-659 — structured output on Converse: one tool, forced, whose input is the schema.
            kwargs["toolConfig"] = {
                "tools": [{"toolSpec": {"name": schema["name"],
                                        "description": "Return the result as this object.",
                                        "inputSchema": {"json": schema["schema"]}}}],
                "toolChoice": {"tool": {"name": schema["name"]}},
            }
            # AA-803 (S246): Sonnet 5.5 / Opus 5.5 / Fable 5.1 / Mythos 5.1 400 on a forced tool choice —
            # ask them with `auto` and an explicit instruction instead (`_forced_tool_choice_ok`).
            if not _forced_tool_choice_ok(model, profile_id):
                _use_auto_tool_choice(kwargs, schema)
        # AA-1002 (S6): cache the tools + system prefix of a long Claude system prompt (_converse_cache_point). Last
        # in the list, after the auto-tool instruction, so the whole stable prefix is cached.
        if _converse_cache_point(model, profile_id, request.system_prompt):
            kwargs["system"] = [*kwargs["system"], dict(CACHE_POINT_BLOCK)]
        # AA-1017: a stage x model effort / reasoning_mode is not sent on Converse (see stage_params); said once.
        stage_params.warn_not_applied(request.stage, model.model_key, "converse")
        # AA-803: no `thinking` / `budget_tokens` field is ever sent (no additionalModelRequestFields), so
        # adaptive-thinking-only models (Haiku 5.5, Sonnet 5.5, Opus 5.5) run their default, and the
        # last message is always the user's (no assistant prefill, which those models reject).

        stream_sink.emit_restart(request.stage)
        on_delta = None if schema else stream_sink.delta_callback(request.stage)
        rt = runtime if runtime is not None else get_satellite_client("bedrock-runtime", account=account)
        native = runtime is not None
        label = model.model_key if native else f"satellite-{model.model_key}"

        in_tok = out_tok = cache_read = cache_write = 0
        stop_reason = None
        # AA-927: every billed Converse request of this attempt (the schema retry makes it 2).
        billed_tok: dict = {}
        provider_requests = 1

        def billed_failure(message: str, cause: Optional[BaseException] = None):
            t = billed_tok
            cost_ = self._calc_cost(model.model_key, t.get("in", 0), t.get("out", 0),
                                    cache_read=t.get("cache_read", 0), cache_write=t.get("cache_write", 0))
            exc = _billed_unusable(message, [_billed_usage(
                label, account,
                {"tokens_in": t.get("in", 0) + t.get("cache_read", 0) + t.get("cache_write", 0),
                 "tokens_out": t.get("out", 0)}, cost_, stop_reason, provider_requests,
                cache_read=t.get("cache_read", 0), cache_write=t.get("cache_write", 0))])
            exc.__cause__ = cause
            return exc

        if on_delta is None:
            resp = _converse_with_param_downgrades(rt.converse, kwargs, model.model_key, schema)
            request_id = _bedrock_request_id(resp)
            _add_converse_usage(billed_tok, resp.get("usage", {}))
            stop_reason = resp.get("stopReason")
            # AA-927: from here on the first request is billed — any raise below (schema mismatch after
            # the retry, the retry request itself failing, a malformed body) carries the billed usage.
            try:
                blocks = resp["output"]["message"]["content"]
                if schema:
                    auto = "auto" in kwargs["toolConfig"]["toolChoice"]
                    # AA-714: Converse does not enforce the tool schema, so a model may omit a required
                    # key. Retry ONCE with an explicit instruction before giving up — far cheaper and
                    # more accurate than failing over to the next (pricier, different-vendor) model.
                    try:
                        content = _checked_json(model.model_key, _first_tool_input(blocks, schema, model, auto),
                                                schema)
                    except RuntimeError as e:
                        logger.warning("converse_schema_retry", model=model.model_key, stage=request.stage,
                                       error=str(e)[:200])
                        retry = dict(kwargs)
                        retry["messages"] = [
                            kwargs["messages"][0],
                            {"role": "assistant", "content": [{"text": "(previous answer was missing a "
                                                                       "required field)"}]},
                            {"role": "user", "content": [{"text":
                                f"Call the tool {schema['name']!r} again and include EVERY required key: "
                                f"{', '.join(schema['schema'].get('required', []))}. Return the full object."}]},
                        ]
                        resp = rt.converse(**retry)
                        provider_requests = 2
                        _add_converse_usage(billed_tok, resp.get("usage", {}))
                        stop_reason = resp.get("stopReason")
                        blocks = resp["output"]["message"]["content"]
                        content = _checked_json(model.model_key, _first_tool_input(blocks, schema, model, auto),
                                                schema)
                else:
                    content = "".join(block.get("text", "") for block in blocks)
            except Exception as e:
                raise billed_failure(str(e), e) from e
        else:
            resp = _converse_dropping_rejected_temperature(rt.converse_stream, kwargs, model.model_key)
            request_id = _bedrock_request_id(resp)
            parts = []
            usage = {}
            for event in resp["stream"]:
                if "contentBlockDelta" in event:
                    text = event["contentBlockDelta"].get("delta", {}).get("text", "")
                    if text:
                        parts.append(text)
                        on_delta(text)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
            content = "".join(parts)
            # A stream that broke before its metadata event has no usage: cost unknown, not logged
            # (AA-927 documented exclusion — the exception surfaces as a route / caller failure).
            _add_converse_usage(billed_tok, usage)
        if not content:
            message = f"{model.model_key} returned empty content (stopReason={stop_reason})"
            if billed_tok.get("in") or billed_tok.get("out") or billed_tok.get("cache_read"):
                raise billed_failure(message)
            raise RuntimeError(message)
        in_tok = billed_tok.get("in", 0)
        out_tok = billed_tok.get("out", 0)
        cache_read = billed_tok.get("cache_read", 0)
        cache_write = billed_tok.get("cache_write", 0)
        # AA-927: after a schema retry these are the sums of BOTH billed requests (the first reply was
        # billed too) — before AA-927 only the second request was priced.
        # AA-1002 (S6): priced on the deployment it ran on (its price overrides), the basis kept on the response.
        priced = self._quote(model.model_key, Usage(input_tokens=in_tok, output_tokens=out_tok,
                                                    cache_read_tokens=cache_read, cache_write_tokens=cache_write),
                             account)
        cost = priced.cost_usd
        # S234 (AA-772/PR0): Converse's `inputTokens` is only the UNCACHED part of the prompt.
        # Bedrock caches long prompt prefixes of the Luna models on its own (no cachePoint sent),
        # so llm_call_log showed tokens_in of 2-118 for 2-5k-character prompts (t10_judge,
        # t2_judge, a3_atomize on gpt-6-luna / gpt-5.6-luna) while cost_usd already priced the
        # cached tokens. `input_tokens` now carries the whole prompt; the cache split stays in
        # cache_read_tokens / cache_write_tokens, and the cost above is unchanged.
        prompt_tok = in_tok + cache_read + cache_write

        logger.info("llm_success", provider="bedrock" if native else "bedrock-satellite", api="converse",
                    model=model.model_key, account=account,
                    in_tokens=prompt_tok, uncached_in_tokens=in_tok, cache_read=cache_read,
                    cache_write=cache_write, out_tokens=out_tok, cost_usd=cost, stop_reason=stop_reason)

        return LLMResponse(
            # "satellite-" prefix: call_log.py strips it and records provider=bedrock-satellite. A task-role
            # (acc2-native) deployment answers as plain "bedrock", like the legacy chain's T1 / T2.
            # AA-642 (S0): a task-role reply names its connection (`account` is the deployment's connection_key
            # here), so its llm_call_log row stores account='acc2' instead of NULL; satellite_account stays None
            # (generated_content.satellite_account only holds a satellite AWS account).
            content=content, model_used=label, provider="bedrock" if native else "bedrock-satellite",
            input_tokens=prompt_tok, output_tokens=out_tok, cost_usd=cost,
            fallback_used=False, satellite_account=None if native else account,
            connection_key=account if native else None,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            stop_reason=stop_reason, provider_requests=provider_requests, pricing_mode=priced.pricing_mode,
            request_id=request_id, deployment_wire_id=profile_id,
        )

    def _call_anthropic_messages(self, request: LLMRequest, model: CatalogModel,
                                 vendor: VendorDeployment) -> LLMResponse:
        """AA-1002 (S2) — one model on one Anthropic API connection (adapters/anthropic_messages.py).

        Same contract as the Converse path: a structured request is conformed to its schema (`_checked_json`);
        a reply that was billed but cannot be used (stop_reason refusal, empty, not JSON, schema mismatch) raises
        BilledUnusableReply carrying its usage, so `_call_key_logged` writes the billed_unusable row and a route
        moves on. `input_tokens` = uncached + cache read + cache write (the Converse convention for
        llm_call_log.tokens_in); the cost is the catalog price of the Model Key (pricing.calc_cost), and the model
        the API reports goes to `provider_model` and the success log. The label "anthropic-api-<key>" makes
        llm_call_log record provider='anthropic' for every caller (call_log._normalize_model_provider)."""
        from .adapters import anthropic_messages

        dep, conn = vendor.deployment, vendor.connection
        schema = request.json_schema
        max_tokens = request.max_tokens
        if model.max_output_tokens:
            max_tokens = min(max_tokens, model.max_output_tokens)
        stream_sink.emit_restart(request.stage)
        on_delta = None if schema else stream_sink.delta_callback(request.stage)
        # AA-1017: a stage x model effort beats the deployment's own effort_by_stage / effort; the catalog row's
        # capabilities (effort_default) are passed only when it states some, so today's call is unchanged.
        request_defaults = dep.request_defaults
        effort = stage_params.effort_for(request.stage, model.model_key)
        if effort and request.stage:
            by_stage = request_defaults.get("effort_by_stage") if isinstance(request_defaults, dict) else None
            request_defaults = {**(request_defaults or {}),
                                "effort_by_stage": {**(by_stage if isinstance(by_stage, dict) else {}),
                                                    request.stage: effort}}
        extra = {"capabilities": model.capabilities} if getattr(model, "capabilities", None) else {}
        timeout = request_timeout()       # AA-1018 part 2: the route deadline's remaining time; None = unchanged
        if timeout is not None:
            extra["timeout"] = timeout
        result = anthropic_messages.send(
            connection=conn, wire_id=dep.wire_id, system=request.system_prompt, user=request.user_prompt,
            max_tokens=max_tokens, stage=request.stage, json_schema=schema, request_defaults=request_defaults,
            on_delta=on_delta, **extra)

        label = f"{ANTHROPIC_API_LABEL_PREFIX}{model.model_key}"
        in_tok, out_tok = result.input_tokens, result.output_tokens
        cache_read, cache_write = result.cache_read_tokens, result.cache_write_tokens
        prompt_tok = in_tok + cache_read + cache_write
        # AA-1002 (S6): 1-hour writes at 2x, the US-only inference premium, the deployment's price overrides.
        priced = self._quote(model.model_key, Usage(
            input_tokens=in_tok, output_tokens=out_tok, cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            cache_write_1h_tokens=_int_attr(result, "cache_write_1h_tokens"),
            inference_geo=_str_attr(result, "inference_geo")), conn.connection_key, deployment=dep)
        cost = priced.cost_usd
        stop_reason = result.stop_reason
        billed = bool(in_tok or out_tok or cache_read or cache_write)

        def unusable(message: str) -> RuntimeError:
            if not billed:
                return RuntimeError(message)
            return _billed_unusable(message, [_billed_usage(
                label, conn.connection_key, {"tokens_in": prompt_tok, "tokens_out": out_tok}, cost, stop_reason, 1,
                cache_read=cache_read, cache_write=cache_write, pricing_mode=priced.pricing_mode)])

        content = result.text
        if stop_reason == "refusal":
            category = (result.stop_details or {}).get("category")
            raise unusable(f"{model.model_key} refused the request (stop_reason=refusal, category={category})")
        if not content:
            raise unusable(f"{model.model_key} returned empty content (stop_reason={stop_reason})")
        if schema:
            try:
                parsed = json.loads(content) if result.structured else _json_object_in_text(content)
                if not isinstance(parsed, dict):
                    raise RuntimeError(f"{model.model_key} returned no JSON object for schema {schema['name']!r}")
                content = _checked_json(model.model_key, parsed, schema)
            except (ValueError, RuntimeError) as e:
                raise unusable(str(e)) from e

        logger.info("llm_success", provider="anthropic", api="messages", model=model.model_key,
                    provider_model=result.provider_model, wire_id=dep.wire_id, connection=conn.connection_key,
                    data_scope=conn.data_scope, in_tokens=prompt_tok, uncached_in_tokens=in_tok,
                    cache_read=cache_read, cache_write=cache_write, out_tokens=out_tok, cost_usd=cost,
                    stop_reason=stop_reason, effort=result.effort, request_id=result.request_id)
        return LLMResponse(
            content=content, model_used=label, provider="anthropic",
            input_tokens=prompt_tok, output_tokens=out_tok, cost_usd=cost, fallback_used=False,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write, stop_reason=stop_reason,
            provider_model=result.provider_model, pricing_mode=priced.pricing_mode,
            cache_write_1h_tokens=_int_attr(result, "cache_write_1h_tokens"), connection_key=conn.connection_key,
            request_id=_str_attr(result, "request_id"), effort=_str_attr(result, "effort"),
            deployment_wire_id=dep.wire_id,
        )

    def _call_openai(self, request: LLMRequest, model: str, catalog_model: Optional[CatalogModel] = None,
                     connection: Optional[connections.ProviderConnection] = None,
                     deployment=None) -> LLMResponse:
        """One Chat Completions request on an OpenAI-format connection. AA-1002 (S9): ``connection`` (default
        openai-direct, the 304 row or its built-in mirror) gives the SDK client (adapters/openai_chat.py: key from its
        secret at call time, else its credential env; endpoint; timeout / retries); a 401 drops the cached key and
        client. A reply from an Azure OpenAI connection is labelled ``azure-openai-<key>`` (provider azure_openai)."""
        # AA-209: forward sampling controls only when the caller explicitly set them. This makes the
        # GPT-4.1 judge reproducible (it passes temperature + seed) without changing behavior of
        # content/T3-fallback calls that rely on provider defaults (they never set these fields).
        # AA-659: catalog models (e.g. gpt-6-luna-openai) are reasoning models — they take
        # max_completion_tokens, and temperature/seed only when the catalog says they are supported.
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user",   "content": request.user_prompt},
            ],
        }
        if catalog_model is None:
            kwargs["max_tokens"] = request.max_tokens
        else:
            kwargs["max_completion_tokens"] = request.max_tokens
        sampling_ok = catalog_model is None or catalog_model.supports_temperature
        fields_set = request.model_fields_set
        if sampling_ok and "temperature" in fields_set:
            kwargs["temperature"] = request.temperature
        if sampling_ok and "seed" in fields_set and request.seed is not None:
            kwargs["seed"] = request.seed
        if catalog_model is not None:      # AA-1017: no effort / reasoning field on this path yet (said once)
            stage_params.warn_not_applied(request.stage, catalog_model.model_key, "openai_chat")
        if request.json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.json_schema["name"], "strict": True,
                                "schema": request.json_schema["schema"]},
            }
        model_label = catalog_model.model_key if catalog_model else model
        conn = connection or connections.connection_or_builtin(OPENAI_CONNECTION_KEY)
        conn_key = conn.connection_key if conn is not None else OPENAI_CONNECTION_KEY
        azure = conn is not None and conn.kind == "azure_openai"
        reply_label = f"{AZURE_OPENAI_LABEL_PREFIX}{model_label}" if azure else model_label
        provider = "azure_openai" if azure else "openai"
        # S235 — the single gate in front of the OpenAI platform API: every path into this adapter
        # (legacy gpt-4.1 tier, T3, catalog openai_chat rows, shadows) stops here unless the
        # model's catalog row is provider 'openai' AND enabled, and (AA-1002 S5) the stage's
        # data scopes allow the OpenAI connection's.
        if not openai_platform_allowed(model_label):
            raise OpenAIPlatformBlocked(
                f"{model_label}: OpenAI platform API is disabled (S235 Bedrock-only policy)")
        scopes = stage_scopes(request)
        if not openai_scope_allowed(scopes, conn):
            raise OpenAIPlatformBlocked(
                f"{model_label}: stage {request.stage!r} may not send content outside AWS "
                f"(data scopes {', '.join(scopes or ())})")
        if conn is None:
            raise connections.ConnectionCredentialError(f"connection {OPENAI_CONNECTION_KEY!r} is not known")
        from .adapters import openai_chat
        client = self._openai or openai_chat.client_for(
            conn, getattr(deployment, "request_defaults", None))     # fails closed before any network call
        stream_sink.emit_restart(request.stage)  # AA-637
        timeout = request_timeout()       # AA-1018 part 2: the route deadline's remaining time; None = unchanged
        try:
            resp = (client.chat.completions.create(**kwargs) if timeout is None
                    else client.chat.completions.create(**kwargs, timeout=timeout))
        except Exception as e:
            if openai_chat.is_auth_error(e):
                openai_chat.forget_connection(conn_key)              # a rotated key is read on the next call
            raise
        content = resp.choices[0].message.content or ""
        # AA-637 — no token streaming on this last-resort path; a live view gets the whole text.
        _cb = None if request.json_schema else stream_sink.delta_callback(request.stage)   # S251: as on Converse
        if _cb is not None and content:
            _cb(content)
        in_tok  = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
        # AA-1002 (S6): `prompt_tokens` includes the cached prefix (usage.prompt_tokens_details.cached_tokens), which
        # OpenAI bills at the cache-read rate; before S6 every prompt token was priced at the input rate.
        cached = _openai_cached_tokens(resp.usage, in_tok)
        priced = self._quote(model_label, Usage(input_tokens=in_tok - cached, output_tokens=out_tok,
                                                cache_read_tokens=cached), conn_key, deployment=deployment)
        cost    = priced.cost_usd
        # AA-493: OpenAI's field is "finish_reason" ("stop" | "length" | "content_filter" | ...)
        # — different name from Anthropic's "stop_reason", same purpose. Stored under the same
        # LLMResponse.stop_reason field so callers/the DB log don't need a provider branch.
        finish_reason = resp.choices[0].finish_reason

        def billed_failure(message: str) -> BilledUnusableReply:
            # AA-927: the request was billed even though the reply is unusable.
            # AA-1002 (S6): the row's account is the connection (no AWS account on the OpenAI API).
            return _billed_unusable(message, [_billed_usage(
                reply_label, conn_key, {"tokens_in": in_tok, "tokens_out": out_tok}, cost,
                finish_reason, 1, cache_read=cached, pricing_mode=priced.pricing_mode)])

        if catalog_model is not None and not content:
            # A reasoning model can spend the whole token budget thinking and return nothing.
            raise billed_failure(f"{model_label} returned empty content (finish_reason={finish_reason})")
        if request.json_schema:
            try:
                parsed = json.loads(content)
            except ValueError as e:
                raise billed_failure(f"{model_label} returned invalid JSON for schema "
                                     f"{request.json_schema['name']!r}: {e}") from e
            try:
                content = _checked_json(model_label, parsed, request.json_schema)
            except RuntimeError as e:
                raise billed_failure(str(e)) from e

        logger.info("llm_success", provider=provider, model=model_label,
                    in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost,
                    stop_reason=finish_reason, connection=conn_key)

        return LLMResponse(
            content=content, model_used=reply_label, provider=provider,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            stop_reason=finish_reason, cache_read_tokens=cached, pricing_mode=priced.pricing_mode,
            connection_key=conn_key, deployment_wire_id=model if isinstance(model, str) else None,
            # AA-804 (ledger v2): the SDK's x-request-id and the reasoning part of completion_tokens, when reported
            request_id=_str_attr(resp, "_request_id"),
            reasoning_tokens=_reasoning_tokens(resp.usage),
        )

    def _calc_cost(self, model: str, in_tok: int, out_tok: int,
                   cache_read: int = 0, cache_write: int = 0) -> float:
        # AA-635 — delegates to pricing.calc_cost() (was a second copy of the same formula) so
        # cache-token pricing and any future rate fix land in one place for both mechanisms.
        return calc_cost(model, in_tok, out_tok, cache_read=cache_read, cache_write=cache_write)

    def _quote(self, model: str, usage: Usage, connection_key: Optional[str] = None,
               deployment=None) -> "pricing.Quote":
        """AA-1002 (S6): pricing.quote on the deployment of ``model`` on ``connection_key`` (looked up when not
        given; None = catalog / code-table rates)."""
        if deployment is None:
            deployment = pricing.deployment_for(model, connection_key)
        return pricing.quote(model, usage, deployment)

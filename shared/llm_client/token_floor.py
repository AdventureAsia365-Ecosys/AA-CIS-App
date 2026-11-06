"""S271 (AA-741 / AA-900) — the gateway owns the output-token budget: the floor of a reasoning-capable model and the
one truncation retry of every stage. The rules are the pure core's (``shared/aa_llm/tokens.py``); this module binds
them to a request, a catalog model and the gateway's switches.

**Floor** (``apply_floor``, called in ``LLMClient._call_catalog_model`` right after the stage x model parameters): a
reasoning-capable catalog model (Claude 5.x, GPT-5.x / 6.x, or a row whose ``capabilities.reasoning`` is true) is sent
at least ``tokens.REASONING_FLOOR`` (or the row's ``capabilities.min_output_tokens``), whatever the call site asked
for. It overrides a call site's explicit value on purpose: a call site cannot know which model the admin routes its
stage to, and a ceiling under the floor on such a model only cuts the reply (the thinking is billed inside it). A
higher ceiling is never lowered. The legacy keys (haiku / sonnet = Claude 4.5) do not reason and read nothing here.

**Retry** (``LLMClient._call_key_logged``): a reply cut at its ceiling, a provider's refusal to return a cut reply
(OpenAI 400, class ``truncated``) or a billed unusable reply whose stop reason is a cut, is asked ONCE more on the same
route model with ``tokens.retry_budget`` (double, capped at the model's output cap), before the route moves on to its
next model. Never a third call. Ledger (the token_budget.py semantics, AA-927):

- the cut reply that came back gets the gateway's own row (``cut_reply_row``: status truncated, ``budget_cut: true``,
  ``gateway_attempt: true``, no ok-rate key), written before the retry is sent; a cut that raised already has its
  row (the 0 USD truncated row of ``_attempt`` or the billed_unusable row of ``_call_key_logged``);
- the retry's reply is the one the caller gets and logs: ``LLMResponse.truncation_retry_max_tokens`` is set, and
  ``call_log.billed_fields`` turns it into ``truncation_retry: true`` + ``max_tokens`` on the caller's row;
- both requests share the gateway request (its route_trace) and the retry reports the attempt number of the reply it
  retried, so route_watch never reads the retry as a fallback rung; it still counts on the attempt cap.

Not retried: a stage in ``NO_RETRY_STAGES`` (connection probes, ``a3_atomize`` which re-asks with its own ceiling), a
request with ``budget_retry=False`` (a call site that does the retry itself and writes its own two rows:
``services/acp_shared/token_budget.py``; the guard test lists every such site), a shadow call, a request already at
the model's output cap, or ``LLM_BUDGET_RETRY=off``. ``LLM_TOKEN_FLOOR=off`` switches the floor off.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import structlog

from shared.aa_llm import tokens

from .errors import TRUNCATED, classify, is_truncated

logger = structlog.get_logger()

FLOOR_SWITCH_ENV = "LLM_TOKEN_FLOOR"
RETRY_SWITCH_ENV = "LLM_BUDGET_RETRY"

# Stages the gateway never retries (and never floors): the connection probe answers "ok" in a few tokens by design
# (its cut is the expected outcome), and a3_atomize re-asks a cut read itself with its own ceiling.
NO_RETRY_STAGES = frozenset({"adhoc_conn_test", "a3_atomize"})


def _on(env: str) -> bool:
    return (os.environ.get(env) or "on").strip().lower() not in ("off", "0", "false", "no")


def floor_enabled() -> bool:
    return _on(FLOOR_SWITCH_ENV)


def retry_enabled() -> bool:
    return _on(RETRY_SWITCH_ENV)


def _caps(model: Any) -> dict:
    caps = getattr(model, "capabilities", None)
    return caps if isinstance(caps, dict) else {}


def _model_cap(model: Any) -> Optional[int]:
    cap = getattr(model, "max_output_tokens", None)
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None


def floored_max_tokens(request: Any, model: Any) -> int:
    """The ceiling ``request`` is sent with on ``model`` once the floor applies (pure; the switch and the probe
    stages are honoured)."""
    max_tokens = int(getattr(request, "max_tokens", 0) or 0)
    if model is None or not floor_enabled() or getattr(request, "stage", None) in NO_RETRY_STAGES:
        return max_tokens
    return tokens.floored(max_tokens, getattr(model, "model_key", None), _caps(model), _model_cap(model))


def _with_max_tokens(request: Any, max_tokens: int) -> Any:
    out = request.model_copy(update={"max_tokens": max_tokens})
    # max_tokens is now "set by the call site": a stage_model_params row never lowers it again (stage_params.resolve).
    out.__pydantic_fields_set__ = set(request.model_fields_set) | {"max_tokens"}
    return out


def apply_floor(request: Any, model: Any) -> Any:
    """``request`` with its ceiling raised to ``model``'s floor, or the SAME object when nothing changes (a model
    that does not reason, a ceiling already above the floor). Never raises."""
    try:
        want = floored_max_tokens(request, model)
        if want <= int(request.max_tokens):
            return request
        out = _with_max_tokens(request, want)
    except Exception as e:  # noqa: BLE001 — a floor never breaks the call
        logger.warning("llm_token_floor_failed", stage=getattr(request, "stage", None), error=str(e)[:200])
        return request
    logger.info("llm_token_floor_applied", stage=request.stage, model=getattr(model, "model_key", None),
                asked=request.max_tokens, sent=want)
    return out


def retry_ceiling(first_max_tokens: int, model: Any) -> Optional[int]:
    """The ceiling of the one retry, or None when a retry cannot help (already at the model's output cap)."""
    cap = _model_cap(model)
    if not tokens.retry_helps(first_max_tokens, cap):
        return None
    return tokens.retry_budget(first_max_tokens, cap)


def retry_request(request: Any, retry_max_tokens: int) -> Any:
    """The retry's request: the caller's own, every other field kept, max_tokens set to the retry's ceiling."""
    return _with_max_tokens(request, retry_max_tokens)


def retry_allowed(request: Any, scope_extra: Optional[dict] = None) -> bool:
    """True when the gateway retries a cut reply of ``request`` (see the module doc for what is left out)."""
    if not retry_enabled():
        return False
    if getattr(request, "budget_retry", True) is False:
        return False
    if getattr(request, "stage", None) in NO_RETRY_STAGES:
        return False
    return not (scope_extra or {}).get("shadow")


def is_cut_reply(resp: Any) -> bool:
    """True when a reply that came back stopped on its token ceiling."""
    return is_truncated(getattr(resp, "stop_reason", None))


def _billed_records(exc: BaseException) -> list:
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        billed = getattr(cur, "billed_usage", None)
        if billed:
            return list(billed)
        cur = cur.__cause__ or cur.__context__
    return []


def is_cut_failure(exc: BaseException) -> bool:
    """True when a model attempt raised because its reply was cut: the provider refused to return a cut reply (class
    truncated, nothing billed), or a billed unusable reply (cut JSON, missing tool input) whose stop reason is a cut."""
    try:
        if classify(exc) == TRUNCATED:
            return True
    except Exception:  # noqa: BLE001 — an unclassifiable error is not a cut
        pass
    return any(isinstance(b, dict) and is_truncated(b.get("stop_reason")) for b in _billed_records(exc))


__all__ = ["FLOOR_SWITCH_ENV", "NO_RETRY_STAGES", "RETRY_SWITCH_ENV", "apply_floor", "floor_enabled",
           "floored_max_tokens", "is_cut_failure", "is_cut_reply", "retry_allowed", "retry_ceiling",
           "retry_enabled", "retry_request"]

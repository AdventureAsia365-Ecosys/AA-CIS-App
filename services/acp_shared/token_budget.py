"""
services.acp_shared.token_budget — the output-token budgets of the tenant writing stages (T8 angles, T9 write,
T9 repair, T9 FAQ filter) and the one budget retry they share (AA-741, S270).

Why: since route v6 serves the 5.x models (Sonnet 5.5 / Haiku 5.5) these stages stop on their token ceiling much more
often than on the 4.x models: the 5.x models write longer and think adaptively inside max_tokens. The S270 ledger
(shared.llm_call_log, the 7 days to 05/11/2026) showed t8_angle_gen at 2048 cut on 7 of 9 calls (5 of them billed but
unusable: "Expected exactly 3 angles" / "Angle 2 missing required field(s)", so 2 of 4 live T8 requests ended
pending_goal with no angle), t9_write cut on 1 of 9 at 8000, t9_repair on 1 of 1 at 2048 and t9_faq_filter on 1 of 1
at 3000. A budget is a ceiling: a complete reply costs the same under a higher one, only a reply that would have been
cut costs more (and that one was thrown away before).

The retry: a reply cut at its ceiling (stop reason max_tokens / length / model_context_window_exceeded, the
gateway's TRUNCATED class) — or a provider that refused to return a cut reply (the OpenAI 400, gateway class
``truncated``, nothing billed) — is asked ONCE more through the same LLMClient path with double the ceiling,
capped. Never a third call: a reply still cut after the retry goes back to the caller with ``still_cut`` set, and
each caller fails it clearly (T8 / FAQ filter / sentence repair raise, the FAQ regeneration and the SEO fit give
nothing back, the T9 writer's F0_complete gate holds the piece).

Ledger: the caller writes one row per billed reply (AA-927). The cut first reply is written through ``on_cut`` before
the retry is sent, so it is in the ledger even when the retry raises; its row carries no ok-rate key and its status is
derived from the stop reason ('truncated'). The retry's row carries ``truncation_retry: true``. ``attempt_no`` is not
set by the caller: it is the gateway's attempt position inside ONE request (route_watch reads > 1 as a fallback rung),
and each of the two requests here is its own gateway request.

S271: the gateway now owns a truncation retry for every stage (``shared/llm_client/token_floor.py``) and the floor of a
reasoning-capable model. The stages here keep their own retry (their cut row carries the piece / request ids only
the caller has), so every request this module sends says ``budget_retry=False`` and the gateway does not retry it a
second time; the retry ceiling is the core's (``shared/aa_llm/tokens.py``).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import structlog

from shared.aa_llm import tokens
from shared.llm_client.errors import TRUNCATED, classify, is_truncated
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

# Per-stage ceilings (S270 ledger evidence above). One named constant per stage; the floors are pinned by
# tests/unit/test_aa741_s270_max_tokens.py.
T8_ANGLE_GEN_MAX_TOKENS = 4096        # was 2048: 7/9 calls cut (3 angles x 4 fields + reason, 5.x verbosity)
T9_WRITE_BLOG_MAX_TOKENS = 12000      # was 8000: 1/9 cut (JSON envelope + a 4,700-13,000 char body + tags)
T9_WRITE_DEFAULT_MAX_TOKENS = 4096    # was 3000: the short channels, same 5.x head-room
# was 2048 (pipeline) / 400 (admin suggest): 1/1 cut on haiku-5-5; S271: 4096 -> 8192, the S271 ledger still had 3
# cut first replies at 4096 (each retried at 8192): the first ceiling now is the one that answered
T9_REPAIR_MAX_TOKENS = 8192
T9_FAQ_FILTER_MAX_TOKENS = 6000       # was 3000: 1/1 cut on haiku-5-5 (30 verdicts + the JSON envelope)

RETRY_FACTOR = tokens.RETRY_FACTOR    # the one retry doubles the ceiling ...
RETRY_CAP = tokens.RETRY_CAP          # ... up to this (the gateway also caps at the catalog's max_output_tokens)

# The stages this module budgets (the stage x ceiling the tests check).
STAGE_FLOORS = {
    "t8_angle_gen": T8_ANGLE_GEN_MAX_TOKENS,
    "t9_write": T9_WRITE_DEFAULT_MAX_TOKENS,
    "t9_repair": T9_REPAIR_MAX_TOKENS,
    "t9_faq_filter": T9_FAQ_FILTER_MAX_TOKENS,
}


def retry_budget(max_tokens: int) -> int:
    """The ceiling of the one retry: double, capped at RETRY_CAP, never below the first ceiling."""
    return tokens.retry_budget(max_tokens)


def is_cut(resp: Any) -> bool:
    """True when a reply stopped on its token ceiling (the gateway's TRUNCATED stop reasons)."""
    return is_truncated(getattr(resp, "stop_reason", None))


def is_truncated_refusal(exc: BaseException) -> bool:
    """True when the gateway raised because the provider refused to return a reply cut at its ceiling."""
    try:
        return classify(exc) == TRUNCATED
    except Exception:  # noqa: BLE001 — an unclassifiable error is not a truncation
        return False


@dataclass
class BudgetedReply:
    """The reply a caller uses (the retry's when one ran), the cut first reply when a retry ran after one, and the
    ceilings of both requests. ``still_cut`` = the reply the caller got is cut too (no third call is made)."""
    resp: Any
    cut_first: Any = None
    retried: bool = False
    max_tokens: int = 0
    retry_max_tokens: Optional[int] = None

    @property
    def still_cut(self) -> bool:
        return is_cut(self.resp)

    @property
    def cost_usd(self) -> float:
        """Every billed reply of the two calls (the cut first one was billed too)."""
        total = float(getattr(self.resp, "cost_usd", 0.0) or 0.0)
        if self.cut_first is not None:
            total += float(getattr(self.cut_first, "cost_usd", 0.0) or 0.0)
        return total

    def signal(self) -> dict:
        """The quality_signal keys of the row of the reply the caller uses: {} when no retry ran."""
        if not self.retried:
            return {}
        return {"truncation_retry": True, "max_tokens": self.retry_max_tokens}


def cut_signal(max_tokens: int, retry_max_tokens: int) -> dict:
    """The quality_signal keys of the cut first reply's row (no ok-rate key: the row never moves a stage's ok-rate)."""
    return {"budget_cut": True, "max_tokens": max_tokens, "retry_max_tokens": retry_max_tokens}


def _own_retry(request: LLMRequest) -> LLMRequest:
    """S271: the caller's request with ``budget_retry=False``: this module retries a cut reply, the gateway must not
    retry it again (that would be a third call). The same object when it already says so."""
    if getattr(request, "budget_retry", True) is False:
        return request
    out = request.model_copy(update={"budget_retry": False})
    out.__pydantic_fields_set__ = set(request.model_fields_set) | {"budget_retry"}
    return out


def _retry_request(request: LLMRequest) -> LLMRequest:
    # model_copy keeps every other field the caller set (temperature, model_tier, json_schema, role) and marks
    # max_tokens as set by the call site, so a stage_model_params row never overrides the retry's ceiling.
    out = request.model_copy(update={"max_tokens": retry_budget(request.max_tokens), "budget_retry": False})
    out.__pydantic_fields_set__ = set(request.model_fields_set) | {"max_tokens", "budget_retry"}
    return out


def generate_with_budget_retry(generate: Callable[[LLMRequest], Any], request: LLMRequest, *,
                               on_cut: Optional[Callable[[Any, int, int], None]] = None) -> BudgetedReply:
    """Sync. At most two ``generate`` calls (the caller passes ``LLMClient().generate``, so the AA-927 call-site
    guard still sees the call in the caller, which writes the rows): the request, and once more with
    ``retry_budget`` when the first reply is cut (``on_cut(resp, max_tokens, retry_max_tokens)`` writes its row
    first) or the provider refused a cut reply. Any other error propagates; an error of the retry propagates too
    (the cut row is already written)."""
    request = _own_retry(request)
    first_max = request.max_tokens
    try:
        resp = generate(request)
    except Exception as e:
        if not is_truncated_refusal(e):
            raise
        retry = _retry_request(request)
        logger.info("llm_budget_retry", stage=request.stage, reason="truncated_refusal",
                    max_tokens=first_max, retry_max_tokens=retry.max_tokens)
        return BudgetedReply(resp=generate(retry), retried=True, max_tokens=first_max,
                             retry_max_tokens=retry.max_tokens)
    if not is_cut(resp):
        return BudgetedReply(resp=resp, max_tokens=first_max)
    retry = _retry_request(request)
    if on_cut is not None:
        on_cut(resp, first_max, retry.max_tokens)
    logger.info("llm_budget_retry", stage=request.stage, reason="cut", max_tokens=first_max,
                retry_max_tokens=retry.max_tokens)
    return BudgetedReply(resp=generate(retry), cut_first=resp, retried=True, max_tokens=first_max,
                         retry_max_tokens=retry.max_tokens)


async def agenerate_with_budget_retry(generate: Callable[[LLMRequest], Any], request: LLMRequest, *,
                                      on_cut: Optional[Callable[[Any, int, int], Awaitable[None]]] = None
                                      ) -> BudgetedReply:
    """Async twin of ``generate_with_budget_retry``: each ``generate`` call runs in ``asyncio.to_thread`` (the
    blocking gateway call never runs on the event loop) and ``on_cut`` is awaited."""
    request = _own_retry(request)
    first_max = request.max_tokens
    try:
        resp = await asyncio.to_thread(generate, request)
    except Exception as e:
        if not is_truncated_refusal(e):
            raise
        retry = _retry_request(request)
        logger.info("llm_budget_retry", stage=request.stage, reason="truncated_refusal",
                    max_tokens=first_max, retry_max_tokens=retry.max_tokens)
        return BudgetedReply(resp=await asyncio.to_thread(generate, retry), retried=True,
                             max_tokens=first_max, retry_max_tokens=retry.max_tokens)
    if not is_cut(resp):
        return BudgetedReply(resp=resp, max_tokens=first_max)
    retry = _retry_request(request)
    if on_cut is not None:
        await on_cut(resp, first_max, retry.max_tokens)
    logger.info("llm_budget_retry", stage=request.stage, reason="cut", max_tokens=first_max,
                retry_max_tokens=retry.max_tokens)
    return BudgetedReply(resp=await asyncio.to_thread(generate, retry), cut_first=resp, retried=True,
                         max_tokens=first_max, retry_max_tokens=retry.max_tokens)


class ReplyStillCut(Exception):
    """The reply was cut at its ceiling twice (the request and its one budget retry). Raised by callers that need a
    complete reply; never retried again."""

    def __init__(self, stage: Optional[str], max_tokens: int, retry_max_tokens: Optional[int]):
        super().__init__(stage, max_tokens, retry_max_tokens)
        self.stage, self.max_tokens, self.retry_max_tokens = stage, max_tokens, retry_max_tokens

    def __str__(self) -> str:
        return (f"reply cut at its token ceiling twice on stage {self.stage!r} "
                f"(max_tokens {self.max_tokens}, then {self.retry_max_tokens})")


__all__ = ["BudgetedReply", "RETRY_CAP", "RETRY_FACTOR", "ReplyStillCut", "STAGE_FLOORS", "T8_ANGLE_GEN_MAX_TOKENS",
           "T9_FAQ_FILTER_MAX_TOKENS", "T9_REPAIR_MAX_TOKENS", "T9_WRITE_BLOG_MAX_TOKENS",
           "T9_WRITE_DEFAULT_MAX_TOKENS", "agenerate_with_budget_retry", "cut_signal", "generate_with_budget_retry",
           "is_cut", "is_truncated_refusal", "retry_budget"]

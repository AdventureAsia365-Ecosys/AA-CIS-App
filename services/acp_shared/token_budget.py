"""
services.acp_shared.token_budget — the output-token ceilings of the tenant writing stages (T8 angles, T9 write,
T9 repair, T9 FAQ filter) (AA-741, S270; a ceiling calculator only since S271 W0, AA-900).

Why: since route v6 serves the 5.x models (Sonnet 5.5 / Haiku 5.5) these stages stop on their token ceiling much more
often than on the 4.x models: the 5.x models write longer and think adaptively inside max_tokens. The S270 ledger
(shared.llm_call_log, the 7 days to 05/11/2026) showed t8_angle_gen at 2048 cut on 7 of 9 calls (5 of them billed but
unusable: "Expected exactly 3 angles" / "Angle 2 missing required field(s)", so 2 of 4 live T8 requests ended
pending_goal with no angle), t9_write cut on 1 of 9 at 8000, t9_repair on 1 of 1 at 2048 and t9_faq_filter on 1 of 1
at 3000. A budget is a ceiling: a complete reply costs the same under a higher one, only a reply that would have been
cut costs more (and that one was thrown away before).

Before S271 W0 this module also retried a cut reply itself (S270): see the S271 W0 paragraph for who does now. A
reply still cut after the one retry goes back to the caller with ``still_cut`` set, and each caller fails it clearly
(T8 / FAQ filter / sentence repair raise, the FAQ regeneration and the SEO fit give nothing back, the T9 writer's
F0_complete gate holds the piece).

S271 W0 (AA-900 l13): this module only COMPUTES ceilings now. The gateway (``shared/llm_client/token_floor.py``,
``LLMClient._call_key_logged``) is the single owner of the one truncation retry: a cut reply, a refused cut reply, AND
an empty / unusable reply cut at max_tokens (a billed unusable reply) are retried once on the SAME route model at
double the ceiling before the route moves on. Before W0 every request here said ``budget_retry=False`` and this module
retried itself, but it never saw an empty cut (it raised inside the route), so Sonnet 5.5 empty cuts fell silently to
Sonnet 4.6 (3 of 3 in the 06/11 ledger). The ledger keeps two rows per retried cut: the gateway's cut row (status
truncated or billed_unusable, ``budget_cut`` / ``gateway_attempt``, ``retry_reason``) and the caller's row of the
retry (``truncation_retry: true``, ``max_tokens``, ``retry_reason``, through ``billed_fields``). The gateway's cut row
carries the job and tenant of the bound context and the route_trace of the caller's row, not the piece / request id.

``generate_with_budget_retry`` keeps its name and shape for the call sites (one gateway call; ``BudgetedReply`` reads
the gateway's retry facts off the reply: ``retried``, ``still_cut`` = the reply the caller got is still cut, no third
call, ``cost_usd`` = the reply plus the cut attempt it replaced).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

import structlog

from shared.aa_llm import tokens
from shared.llm_client.errors import is_truncated
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

# Per-stage ceilings (S270 ledger evidence above). One named constant per stage; the floors are pinned by
# tests/unit/test_aa741_s270_max_tokens.py.
# S271 W0: T8 4096 -> 8192 and the short T9 channels 4096 -> 8192, the effort floor of their route's first model
# (Sonnet 5.5 at effort high / medium: token_floor.effort_floor; T8 was still cut at 4096 on 9 of 68 calls in the week
# of 02/11). A ceiling is head-room: a complete reply costs the same.
T8_ANGLE_GEN_MAX_TOKENS = 8192        # was 2048 -> 4096 (S270: 7/9 calls cut, 3 angles x 4 fields + reason)
T9_WRITE_BLOG_MAX_TOKENS = 12000      # was 8000: 1/9 cut (JSON envelope + a 4,700-13,000 char body + tags)
T9_WRITE_DEFAULT_MAX_TOKENS = 8192    # was 3000 -> 4096 (S270): the short channels
# was 2048 (pipeline) / 400 (admin suggest): 1/1 cut on haiku-5-5; S271: 4096 -> 8192, the S271 ledger still had 3
# cut first replies at 4096 (each retried at 8192): the first ceiling now is the one that answered
T9_REPAIR_MAX_TOKENS = 8192
T9_FAQ_FILTER_MAX_TOKENS = 6000       # was 3000: 1/1 cut on haiku-5-5 (30 verdicts + the JSON envelope)

RETRY_FACTOR = tokens.RETRY_FACTOR    # the one retry doubles the ceiling ...
RETRY_CAP = tokens.RETRY_CAP          # ... up to this (the gateway also caps at the catalog's max_output_tokens)

# The stages this module budgets (the stage x lowest ceiling a call site sends; the CI lint checks each against the
# effort floor of every model of the stage's route: tests/unit/test_aa900_s271_w0_gateway.py).
STAGE_FLOORS = {
    "t8_angle_gen": T8_ANGLE_GEN_MAX_TOKENS,
    "t9_write": min(T9_WRITE_DEFAULT_MAX_TOKENS, T9_WRITE_BLOG_MAX_TOKENS),
    "t9_repair": T9_REPAIR_MAX_TOKENS,
    "t9_faq_filter": T9_FAQ_FILTER_MAX_TOKENS,
}


def retry_budget(max_tokens: int) -> int:
    """The ceiling of the gateway's one retry: double, capped at RETRY_CAP, never below the first ceiling (the
    gateway also caps at the catalog's max_output_tokens)."""
    return tokens.retry_budget(max_tokens)


def is_cut(resp: Any) -> bool:
    """True when a reply stopped on its token ceiling (the gateway's TRUNCATED stop reasons)."""
    return is_truncated(getattr(resp, "stop_reason", None))


def _positive_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _money(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


@dataclass
class BudgetedReply:
    """The reply a caller uses and the gateway's retry facts on it. ``retried`` = the gateway retried a cut on the
    same model (``LLMResponse.truncation_retry_max_tokens``); ``still_cut`` = the reply the caller got is cut too (no
    third call is made, or no retry could run: at the model's cap / past the route deadline)."""
    resp: Any
    max_tokens: int = 0

    @property
    def retry_max_tokens(self) -> Optional[int]:
        return _positive_int(getattr(self.resp, "truncation_retry_max_tokens", None))

    @property
    def retried(self) -> bool:
        return self.retry_max_tokens is not None

    @property
    def retry_reason(self) -> Optional[str]:
        reason = getattr(self.resp, "truncation_retry_reason", None)
        return reason if isinstance(reason, str) and reason else None

    @property
    def still_cut(self) -> bool:
        return is_cut(self.resp)

    @property
    def cost_usd(self) -> float:
        """Every billed reply of the request: the one the caller got plus the cut attempt the gateway retried."""
        total = _money(getattr(self.resp, "cost_usd", 0.0))
        if self.retried:
            total += _money(getattr(self.resp, "truncation_cut_cost_usd", 0.0))
        return total

    def signal(self) -> dict:
        """The quality_signal keys of the caller's row: {} when no retry ran (``billed_fields`` carries the same keys;
        a caller passing both writes them once)."""
        if not self.retried:
            return {}
        out = {"truncation_retry": True, "max_tokens": self.retry_max_tokens}
        if self.retry_reason:
            out["retry_reason"] = self.retry_reason
        return out


def generate_with_budget_retry(generate: Callable[[LLMRequest], Any], request: LLMRequest) -> BudgetedReply:
    """Sync. ONE ``generate`` call (the caller passes ``LLMClient().generate``, so the AA-927 call-site guard still
    sees the call in the caller, which writes its row): the gateway owns the truncation retry (S271 W0). Any error
    propagates."""
    return BudgetedReply(resp=generate(request), max_tokens=request.max_tokens)


async def agenerate_with_budget_retry(generate: Callable[[LLMRequest], Any], request: LLMRequest) -> BudgetedReply:
    """Async twin: the blocking gateway call runs in ``asyncio.to_thread`` (never on the event loop)."""
    return BudgetedReply(resp=await asyncio.to_thread(generate, request), max_tokens=request.max_tokens)


class ReplyStillCut(Exception):
    """The reply was cut at its ceiling twice (the request and its one budget retry). Raised by callers that need a
    complete reply; never retried again."""

    def __init__(self, stage: Optional[str], max_tokens: int, retry_max_tokens: Optional[int]):
        super().__init__(stage, max_tokens, retry_max_tokens)
        self.stage, self.max_tokens, self.retry_max_tokens = stage, max_tokens, retry_max_tokens

    def __str__(self) -> str:
        if self.retry_max_tokens is None:
            return (f"reply cut at its token ceiling on stage {self.stage!r} (max_tokens {self.max_tokens}; no retry: "
                    "at the model's output cap or past the route deadline)")
        return (f"reply cut at its token ceiling twice on stage {self.stage!r} "
                f"(max_tokens {self.max_tokens}, then {self.retry_max_tokens})")


__all__ = ["BudgetedReply", "RETRY_CAP", "RETRY_FACTOR", "ReplyStillCut", "STAGE_FLOORS", "T8_ANGLE_GEN_MAX_TOKENS",
           "T9_FAQ_FILTER_MAX_TOKENS", "T9_REPAIR_MAX_TOKENS", "T9_WRITE_BLOG_MAX_TOKENS",
           "T9_WRITE_DEFAULT_MAX_TOKENS", "agenerate_with_budget_retry", "generate_with_budget_retry", "is_cut",
           "retry_budget"]

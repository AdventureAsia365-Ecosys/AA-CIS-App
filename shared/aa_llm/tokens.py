"""Output-token budgets of the AI gateway: the floor of a reasoning-capable model and the one truncation retry
(S271, the AA-741 / AA-900 max_tokens failures).

Why: the 5.x Claude models and the GPT-5.x / 6.x models reason inside ``max_tokens`` (the thinking is billed as output
and counts against the ceiling). A ceiling sized for a 4.x reply is cut before the answer starts: the S271 ledger (30
days to 06/11/2026) had the R2 eval judge at 1200 cut on 33 of 33 calls on gpt-6-luna-openai, the route test at 16
tokens cut on every reasoning model, and T8 / T9 cut until each call site raised its own constant. Each call site
sizing its own ceiling is how the bug kept coming back, so the gateway now owns the two rules below.

1. **Floor.** A reasoning-capable model never gets a ceiling below ``REASONING_FLOOR`` (or the catalog row's
   ``capabilities.min_output_tokens``). The floor only raises a ceiling, never lowers one: the higher per-stage
   ceilings stay. A ceiling is head-room, not the expected size: a complete reply costs the same under a higher one.
2. **One truncation retry.** A reply cut at its ceiling (the TRUNCATED stop reasons), or a provider that refused to
   return a cut reply, is asked once more on the same model with ``retry_budget`` (double, capped). Never a third
   call.

Pure: standard library only, no I/O (this module is copied byte for byte into the other apps, see ``__init__``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The safe minimum ceiling of a reasoning-capable model: room for the thinking plus a JSON verdict / short reply.
REASONING_FLOOR = 4096
# The one retry doubles the ceiling, up to RETRY_CAP (the adapter also caps at the catalog's max_output_tokens).
RETRY_FACTOR = 2
RETRY_CAP = 24000

# Catalog capability keys read as data (each falls back to the code rule below when the row does not state it).
CAP_REASONING = "reasoning"  # bool: the model reasons inside max_tokens
CAP_MIN_OUTPUT_TOKENS = "min_output_tokens"  # int: the model's own floor (wins over REASONING_FLOOR)

# The code rule (every catalog row today states neither capability). Claude 5.x and later (sonnet / haiku / opus /
# fable, with or without the "claude-" prefix) and GPT-5.x and later (gpt-5, gpt-5.6-luna, gpt-6-luna-openai,
# gpt-6.1-terra, ...). Claude 4.x, GPT-4.1, Nova, Llama and the embedding models do not reason unless asked.
_CLAUDE_REASONING = re.compile(r"^(?:claude-)?(?:sonnet|haiku|opus|fable)-(?:[5-9]|[1-9]\d+)(?:[.-]|$)")
_GPT_REASONING = re.compile(r"^gpt-(?:[5-9]|[1-9]\d+)(?:[.-]|$)")


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def reasoning_capable(model_key: str | None, capabilities: Mapping[str, Any] | None = None) -> bool:
    """True when ``model_key`` reasons inside its output ceiling: the catalog row's ``capabilities.reasoning`` when it
    states a bool, else the code rule. Pure."""
    caps = capabilities if isinstance(capabilities, Mapping) else {}
    stated = caps.get(CAP_REASONING)
    if isinstance(stated, bool):
        return stated
    key = (model_key or "").strip().lower()
    return bool(_CLAUDE_REASONING.match(key) or _GPT_REASONING.match(key))


def floor_for(model_key: str | None, capabilities: Mapping[str, Any] | None = None) -> int | None:
    """The minimum ceiling of ``model_key`` (None = no floor): ``capabilities.min_output_tokens`` when the row states
    a positive int, else ``REASONING_FLOOR`` for a reasoning-capable model. Pure."""
    caps = capabilities if isinstance(capabilities, Mapping) else {}
    own = _int(caps.get(CAP_MIN_OUTPUT_TOKENS))
    if own is not None and own > 0:
        return own
    return REASONING_FLOOR if reasoning_capable(model_key, caps) else None


def floored(
    max_tokens: int, model_key: str | None, capabilities: Mapping[str, Any] | None = None, model_cap: int | None = None
) -> int:
    """``max_tokens`` raised to the model's floor (never lowered), and the floor itself never above the model's own
    output cap (``model_cap``, the catalog's max_output_tokens). Pure."""
    floor = floor_for(model_key, capabilities)
    if floor is None:
        return int(max_tokens)
    cap = _int(model_cap)
    if cap is not None and cap > 0:
        floor = min(floor, cap)
    return max(int(max_tokens), floor)


def retry_budget(max_tokens: int, model_cap: int | None = None) -> int:
    """The ceiling of the one truncation retry: double, capped at ``RETRY_CAP`` and at the model's own output cap,
    never below the first ceiling. Pure."""
    first = int(max_tokens)
    out = min(first * RETRY_FACTOR, RETRY_CAP)
    cap = _int(model_cap)
    if cap is not None and cap > 0:
        out = min(out, cap)
    return max(first, out)


def retry_helps(max_tokens: int, model_cap: int | None = None) -> bool:
    """True when the retry's ceiling is higher than the first one (a request already at the cap is not retried)."""
    return retry_budget(max_tokens, model_cap) > int(max_tokens)


__all__ = [
    "CAP_MIN_OUTPUT_TOKENS",
    "CAP_REASONING",
    "REASONING_FLOOR",
    "RETRY_CAP",
    "RETRY_FACTOR",
    "floor_for",
    "floored",
    "reasoning_capable",
    "retry_budget",
    "retry_helps",
]

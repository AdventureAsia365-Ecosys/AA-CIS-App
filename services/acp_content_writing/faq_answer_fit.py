"""services.acp_content_writing.faq_answer_fit — AA-779 (S247) optional LLM check that each blog FAQ answer
answers its question (T9).

The live 8-blog checkpoint (S247, 31 FAQ pairs, 3-judge panel) found 3 answers that do not answer their
question, all passed by the deterministic subject test (quality_gates.faq_answer_problem) because each answer
names a place or "the institute": a country's average altitude answered with one town's elevation (wrong
subject), "What is <place> called in English?" never giving a name (non-answer), and a writer-invented question
about one institute answered with another (wrong subject).

This module is a REPAIR / DROP step, not a gate: it only removes FAQ pairs, it never holds, blocks, rewrites or
regenerates a piece. It runs under the same flag as the question filter (faq_filter.T9_FAQ_LLM_FILTER):

    off     (default) no model call, FAQ unchanged;
    shadow  call the model, record its verdicts (writer_input.faq_answer_fit), deliver the FAQ unchanged;
    on      drop every pair the model marks NON_ANSWER or WRONG_SUBJECT. Fewer than FAQ_MIN_PAIRS left is
            recorded (`below_min`), never held or regenerated.

One call per piece (every pair in one numbered list) through the Model Gateway, stage `t9_faq_answer_fit`, role
`validate` (ADR 0008; a `judge` stage must be non-Anthropic), one `shared.llm_call_log` row per reply in the
AA-927 shape. Any error, unusable reply or timeout leaves the FAQ unchanged (fail open, recorded). Every pair
is checked, whatever its source (real search question or writer-invented).
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import structlog

from services.acp_content_writing.faq_filter import MODE_OFF, MODE_ON, faq_filter_mode
from services.acp_content_writing.quality_gates import (FAQ_MIN_PAIRS, drop_faq_pairs, faq_pairs,
                                                        strip_citation_tags)
from shared.llm_client.call_log import record_call_sync, record_unusable_sync
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

FIT_STAGE = "t9_faq_answer_fit"
FIT_ROLE = "validate"
ANSWERS, NON_ANSWER, WRONG_SUBJECT = "ANSWERS", "NON_ANSWER", "WRONG_SUBJECT"
VERDICTS = (ANSWERS, NON_ANSWER, WRONG_SUBJECT)
FAILING = frozenset({NON_ANSWER, WRONG_SUBJECT})

FIT_MAX_PAIRS = 10      # the FAQ cap is 5 (FAQ_MAX_PAIRS); a pair past this one is sent nowhere and never dropped
ANSWER_MAX = 1200       # chars of one answer sent to the model
MAX_TOKENS = 1500       # ~30 output tokens per pair x 10 + the JSON envelope
TIMEOUT_S = 60.0        # the whole step; on timeout the FAQ is unchanged

# Examples are NOT pairs of the S247 checkpoint set (keep it that way, or the offline eval on those 31 pairs
# stops being a fair test). Kept short: the system prompt is most of the input tokens.
SYSTEM_PROMPT = """You check the FAQ of one travel blog post. For each numbered pair (Q = question, A = answer) \
decide whether the answer answers its question. Judge fit only: do not check whether facts are true, and do not \
judge style, tone or length.

Verdicts:
- ANSWERS: the answer gives what the question asks, about the thing the question names. A short, general or \
partial answer still ANSWERS ("What can you see at the market?" answered with what the market sells). A yes/no \
question answered yes or no with a reason ANSWERS. An answer that says no exact figure exists but still gives \
the asked kind of information about the asked subject ANSWERS.
- NON_ANSWER: the answer is about the right subject but never gives what was asked. The question asks for a \
name, a number, a date, a place, a reason, a way or a choice, and the answer talks around it (describes the \
place, gives background, restates the question) without that information. Example: "How long is the Great Wall?" \
answered only with when it was built and why it is famous.
- WRONG_SUBJECT: the answer is about a different thing than the one the question names: another place, \
building, institution, person or event; one part instead of the whole (asked about a country, answered with one \
town in it; asked about a national park, answered with one village near it); or something with a similar name. \
Example: "How tall is Mount Fuji?" answered with the height of a hotel at its foot. A generic reference ("the \
museum", "the temple") that describes something other than what the question names is WRONG_SUBJECT.

Rules:
- Judge each pair on its own; the other pairs do not matter.
- Use your general knowledge of well-known places and institutions to tell whether the answer describes the \
thing the question names.
- When unsure, choose ANSWERS: a pair is removed only on a clear failure.

Answer with JSON only: {"items": [{"n": <pair number>, "verdict": "ANSWERS"|"NON_ANSWER"|"WRONG_SUBJECT", \
"reason": "<= 15 words"}]}, one entry per pair, in the given order."""


@dataclass
class FitOutcome:
    verdicts: list = field(default_factory=list)  # [{n, q, verdict, reason}], pair order
    model: Optional[str] = None
    cost_usd: float = 0.0


class FitReplyError(ValueError):
    """The model replied (and was billed) but the reply has no usable verdict for every pair."""


def build_user_prompt(pairs: Sequence[tuple[str, str]]) -> str:
    """The numbered Q/A pairs, citation tags removed, each answer cut at ANSWER_MAX chars."""
    lines = ["FAQ PAIRS:"]
    for n, (q, a) in enumerate(pairs, 1):
        ans = " ".join(strip_citation_tags(a or "").split())[:ANSWER_MAX]
        lines += [f"{n}. Q: {' '.join(strip_citation_tags(q or '').split())}", f"   A: {ans}"]
    return "\n".join(lines)


def parse_fit_verdicts(content: str, n: int) -> list[dict]:
    """One {verdict, reason} per pair 1..n, or FitReplyError. A missing answer or an unknown verdict is an
    error (never guessed)."""
    m = re.search(r"\{.*\}", (content or "").strip(), re.S)
    if not m:
        raise FitReplyError("no JSON object in the reply")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise FitReplyError(f"invalid JSON: {e}") from e
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise FitReplyError("no items list")
    by_n: dict[int, dict] = {}
    for it in items:
        verdict = str(it.get("verdict") or "").strip().upper() if isinstance(it, dict) else ""
        if verdict not in VERDICTS:
            raise FitReplyError(f"bad item {str(it)[:80]}")
        try:
            k = int(it.get("n"))
        except (TypeError, ValueError) as e:
            raise FitReplyError(f"bad item number {it.get('n')!r}") from e
        if not 1 <= k <= n:
            raise FitReplyError(f"item n={k} out of range 1..{n}")
        by_n[k] = {"verdict": verdict, "reason": str(it.get("reason") or "")[:200]}
    missing = [k for k in range(1, n + 1) if k not in by_n]
    if missing:
        raise FitReplyError(f"no verdict for pair(s) {missing}")
    return [by_n[k] for k in range(1, n + 1)]


def check_answer_fit(pairs: Sequence[tuple[str, str]], *, stage: str = FIT_STAGE, role: str = FIT_ROLE,
                     model_tier: Optional[str] = None, tenant_id=None, content_piece_id=None,
                     angle_gate_request_id=None, quality_signal: Optional[dict] = None,
                     client: Optional[LLMClient] = None) -> FitOutcome:
    """Sync. One gateway call for the first FIT_MAX_PAIRS of `pairs`; a verdict per pair sent, in order.

    llm_call_log, AA-927 canonical shape: a usable reply writes one row with `json_parsed: true`; a billed reply
    that is unusable, or any raise after generate() returned, writes one `billed_unusable` row and re-raises, so
    the caller keeps the FAQ unchanged. `stage` / `model_tier` are overridable for an ad-hoc measurement (stage
    `adhoc_aa779`, no route row → `model_tier` names the model)."""
    sent = [(q, a) for q, a in pairs][:FIT_MAX_PAIRS]
    if not sent:
        return FitOutcome()
    request = LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_user_prompt(sent), max_tokens=MAX_TOKENS,
                         temperature=0.0, stage=stage, model_tier=model_tier)
    resp = (client or LLMClient()).generate(request)
    ids = {"tenant_id": tenant_id, "content_piece_id": content_piece_id,
           "angle_gate_request_id": angle_gate_request_id}
    signal = {"issue": "AA-779", "n_pairs": len(sent), **(quality_signal or {})}
    try:
        verdicts = parse_fit_verdicts(resp.content, len(sent))
        rows = [{"n": n, "q": q, **v} for n, ((q, _a), v) in enumerate(zip(sent, verdicts), 1)]
        counts = dict(collections.Counter(v["verdict"] for v in verdicts))
        cost = float(resp.cost_usd or 0.0)
    except Exception as e:
        # AA-927: the reply was billed; its billed_unusable row (no ok-rate key) before the caller falls back.
        record_unusable_sync(resp, stage=stage, role=role, error=e, quality_signal_extra=signal, **ids)
        raise
    record_call_sync(
        stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal=signal | {"json_parsed": True, "verdicts": counts},
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None), **ids,
    )
    return FitOutcome(verdicts=rows, model=resp.model_used, cost_usd=cost)


async def apply_answer_fit(content_text: str, *, context: dict, piece_id=None, request_id=None,
                           mode: Optional[str] = None) -> tuple[str, dict]:
    """The blog text after the answer-fit step, and the admin record of it ({} when the flag is off).

    `off`: no call, text unchanged. No FAQ pair: no call, a record with `pairs: 0`. `shadow`: verdicts recorded,
    text unchanged. `on`: the failing pairs' blocks are cut from the FAQ (quality_gates.drop_faq_pairs, the rest of
    the text verbatim); `below_min` says fewer than FAQ_MIN_PAIRS are left; a failing LAST pair with more than
    one paragraph is kept and listed in `kept_multi_paragraph`. Any failure or timeout: text unchanged,
    `fallback: true`."""
    mode = mode or faq_filter_mode()
    if mode == MODE_OFF:
        return content_text, {}
    pairs = faq_pairs(content_text)
    record: dict = {"mode": mode, "stage": FIT_STAGE, "pairs": len(pairs)}
    if not pairs:
        return content_text, record
    try:
        outcome = await asyncio.wait_for(asyncio.to_thread(
            check_answer_fit, pairs, tenant_id=context.get("tenant_id"),
            content_piece_id=str(piece_id) if piece_id else None,
            angle_gate_request_id=str(request_id) if request_id else None,
            quality_signal={"mode": mode}), timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — any failure: the FAQ is delivered unchanged
        err = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("t9_faq_answer_fit_fallback", mode=mode, error=err,
                       request_id=str(request_id) if request_id else None)
        return content_text, record | {"fallback": True, "error": err}
    failing = [v for v in outcome.verdicts if v["verdict"] in FAILING]
    record |= {"fallback": False, "model": outcome.model, "cost_usd": round(outcome.cost_usd, 6),
               "verdicts": outcome.verdicts, "failing": [v["q"] for v in failing]}
    if mode != MODE_ON or not failing:
        return content_text, record
    # Cut the failing blocks out and keep every other part of the text verbatim (drop_faq_pairs): rebuilding the
    # FAQ from first paragraphs (replace_faq_section) would move a dropped answer's later paragraph into the body.
    text, kept_multi = drop_faq_pairs(content_text, {v["n"] for v in failing})
    dropped = [v for v in failing if v["n"] not in kept_multi]
    pairs_after = len(faq_pairs(text))
    record |= {"dropped": [v["q"] for v in dropped], "dropped_n": [v["n"] for v in dropped],
               "pairs_after": pairs_after, "below_min": pairs_after < FAQ_MIN_PAIRS}
    if kept_multi:
        # The last pair runs past one paragraph to the end of the text (a CTA may follow it): kept, recorded.
        record["kept_multi_paragraph"] = [v["q"] for v in failing if v["n"] in kept_multi]
    return text, record


_KIND_TEXT = {NON_ANSWER: "the answer does not give what the question asks",
              WRONG_SUBJECT: "the answer is about something else"}


def fit_log_lines(record: dict) -> list[str]:
    """Admin-only F7_faq_autofix lines for what the `on` step removed (none for off / shadow / fallback)."""
    if not record.get("dropped") and not record.get("kept_multi_paragraph"):
        return []
    dropped_n = set(record.get("dropped_n") or [])
    lines = [f"dropped FAQ pair (answer fit: {_KIND_TEXT.get(v['verdict'], v['verdict'])}): '{v['q'][:160]}'"
             for v in record.get("verdicts") or [] if v["n"] in dropped_n]
    lines += [f"kept FAQ pair the answer-fit check failed (last pair, more than one paragraph, may hold a CTA): "
              f"'{q[:160]}'" for q in record.get("kept_multi_paragraph") or []]
    if record.get("dropped") and record.get("below_min"):
        lines.append(f"FAQ has {record.get('pairs_after', 0)} pair(s) after the answer-fit drop, under the "
                     f"{FAQ_MIN_PAIRS}-pair minimum (kept, not held)")
    return lines

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

Two call modes (FIT_MODE, named by ADR 0011): `batched`, one call per piece with every pair in one numbered
list; `isolated`, one call per pair (the model sees only that question and answer), FIT_CONCURRENCY at a time.
Every call goes through the Model Gateway, stage `t9_faq_answer_fit` (the stage row, migration 266, picks the
model), role `validate` (ADR 0008; a `judge` stage must be non-Anthropic), one `shared.llm_call_log` row per
reply in the AA-927 shape. Fail open: in batched mode any error, unusable reply or timeout leaves the FAQ
unchanged; in isolated mode a pair whose own call fails is kept (recorded) and the others still count, and the
FAQ is unchanged only when no pair got a verdict or the step times out. Every pair is checked, whatever its
source (real search question or writer-invented).
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import json
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from typing import Optional, Sequence

import structlog

from services.acp_content_writing.faq_filter import MODE_OFF, MODE_ON, faq_filter_mode
from services.acp_content_writing.quality_gates import (FAQ_MIN_PAIRS, drop_faq_pairs, faq_pairs,
                                                        strip_citation_tags)
from shared.llm_client.call_log import billed_fields, record_call_sync, record_unusable_sync
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

FIT_STAGE = "t9_faq_answer_fit"
FIT_ROLE = "validate"
ANSWERS, NON_ANSWER, WRONG_SUBJECT = "ANSWERS", "NON_ANSWER", "WRONG_SUBJECT"
VERDICTS = (ANSWERS, NON_ANSWER, WRONG_SUBJECT)
FAILING = frozenset({NON_ANSWER, WRONG_SUBJECT})

# How the pairs reach the model (ADR 0011, "Answer-fit call mode"):
#   batched   ONE call per piece, every pair in one numbered list (SYSTEM_PROMPT).
#   isolated  ONE call PER PAIR (ISOLATED_SYSTEM_PROMPT): the model sees only that question and answer, so it
#             cannot infer the blog topic from the other pairs (the S247 round-1/2 false flags). The calls run
#             concurrently, at most FIT_CONCURRENCY at a time; a pair whose call fails is kept and recorded.
# FIT_MODE is the production default. It stays `batched` until the S247 round-3 eval (batched on the Sonnet tier
# vs isolated on Haiku, both sets) is written into ADR 0011; the ADR names the value this constant must have.
FIT_MODE_BATCHED, FIT_MODE_ISOLATED = "batched", "isolated"
FIT_MODES = (FIT_MODE_BATCHED, FIT_MODE_ISOLATED)
FIT_MODE = FIT_MODE_BATCHED
FIT_CONCURRENCY = 3     # isolated: calls in flight at once for one piece (≤ 5 pairs → ≤ 2 rounds)

FIT_MAX_PAIRS = 10      # the FAQ cap is 5 (FAQ_MAX_PAIRS); a pair past this one is sent nowhere and never dropped
ANSWER_MAX = 1200       # chars of one answer sent to the model
MAX_TOKENS = 1500       # batched: ~30 output tokens per pair x 10 + the JSON envelope
ISOLATED_MAX_TOKENS = 150   # isolated: one item (~40 tokens) with room to spare
TIMEOUT_S = 60.0        # the whole step; on timeout the FAQ is unchanged
PAIR_DEADLINE_S = 50.0  # isolated: a pair with no reply by then is kept (inside TIMEOUT_S, so the rest still count)

# Generic rules only: no example, place or question from the S247 checkpoint set or the held-out set (keep it
# that way, or the offline eval stops being a fair test). Kept short: the system prompt is most of the input
# tokens. Revised after the in-sample eval (ADR 0011): pairs judged against the blog topic, and a name question
# answered by explaining around the name. Both modes share the verdict rules (_VERDICT_RULES); SYSTEM_PROMPT is
# byte-identical to the round-2 batched prompt.
_VERDICT_RULES = """Verdicts:
- ANSWERS: the answer gives what its question asks, about the thing the question names. A short, general or \
partial answer still ANSWERS. A question about a country or region may be answered at that level. A question \
about "this route", "this trip" or "the area" is answered by naming the route's places and saying what is there.
- NON_ANSWER: the question asks for a specific datum (a name, a translation, a number, a date, a yes or no) and \
the answer neither states it nor says plainly that it does not exist. Explaining around it (background, how a \
word is used, what the place is like) is NON_ANSWER. So is an answer that stays on the subject but never \
addresses what was asked.
- WRONG_SUBJECT: the answer is about a different thing than the one the question names (another place, \
building, institution, person or event, or something with a similar name), or it answers a question about a \
whole (a country, a region, a park) with a smaller place's fact instead of the whole's.

Use your general knowledge of well-known places and institutions to tell whether the answer describes the thing \
the question names. When unsure, choose ANSWERS: a pair is removed only on a clear failure."""

SYSTEM_PROMPT = """You check the FAQ of one travel blog post. For each numbered pair (Q = question, A = answer) \
decide whether the answer answers its own question. Judge each pair only against its own question: the blog's \
topic, the other pairs and whether the question suits the post do not matter. Do not check whether facts are \
true, and do not judge style, tone or length.

""" + _VERDICT_RULES + """

Answer with JSON only: {"items": [{"n": <pair number>, "verdict": "ANSWERS"|"NON_ANSWER"|"WRONG_SUBJECT", \
"reason": "<= 15 words"}]}, one entry per pair, in the given order."""

ISOLATED_SYSTEM_PROMPT = """You check one question-and-answer pair (Q = question, A = answer) from the FAQ of a \
travel blog post. Decide whether the answer answers its question. Do not check whether facts are true, and do \
not judge style, tone or length.

""" + _VERDICT_RULES + """

Answer with JSON only: {"items": [{"n": 1, "verdict": "ANSWERS"|"NON_ANSWER"|"WRONG_SUBJECT", \
"reason": "<= 15 words"}]}."""

BATCHED_HEADER, ISOLATED_HEADER = "FAQ PAIRS:", "FAQ PAIR:"


@dataclass
class FitOutcome:
    verdicts: list = field(default_factory=list)  # [{n, q, verdict, reason}], pair order (isolated: + model;
    #                                               a failed pair: verdict None, + error)
    model: Optional[str] = None
    cost_usd: float = 0.0   # the replies read before returning; a pair past the deadline whose call still finishes
    #                         in the background is billed (and writes its own llm_call_log row) but not counted here
    fit_mode: str = FIT_MODE_BATCHED
    calls: int = 0          # calls started (isolated: a queued pair cancelled at the deadline is not counted)
    errors: list = field(default_factory=list)    # isolated: [{n, q, error}] for the pairs kept on a failure


class FitReplyError(ValueError):
    """The model replied (and was billed) but the reply has no usable verdict for every pair."""


class FitAllPairsFailed(RuntimeError):
    """Isolated mode: no pair got a verdict (every call failed); the caller keeps the FAQ unchanged."""


def build_user_prompt(pairs: Sequence[tuple[str, str]], header: str = BATCHED_HEADER) -> str:
    """The numbered Q/A pairs, citation tags removed, each answer cut at ANSWER_MAX chars."""
    lines = [header]
    for n, (q, a) in enumerate(pairs, 1):
        ans = " ".join(strip_citation_tags(a or "").split())[:ANSWER_MAX]
        lines += [f"{n}. Q: {' '.join(strip_citation_tags(q or '').split())}", f"   A: {ans}"]
    return "\n".join(lines)


def build_request(sent: Sequence[tuple[str, str]], *, fit_mode: str = FIT_MODE_BATCHED, stage: str = FIT_STAGE,
                  model_tier: Optional[str] = None) -> LLMRequest:
    """The gateway request for `sent` (batched: every pair; isolated: exactly one). `model_tier` None lets the
    stage row (migration 266) choose the model; an ad-hoc eval passes a tier name."""
    isolated = fit_mode == FIT_MODE_ISOLATED
    return LLMRequest(system_prompt=ISOLATED_SYSTEM_PROMPT if isolated else SYSTEM_PROMPT,
                      user_prompt=build_user_prompt(sent, ISOLATED_HEADER if isolated else BATCHED_HEADER),
                      max_tokens=ISOLATED_MAX_TOKENS if isolated else MAX_TOKENS, temperature=0.0, stage=stage,
                      model_tier=model_tier)


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


def _fit_call(sent: Sequence[tuple[str, str]], *, fit_mode: str, stage: str, role: str, model_tier: Optional[str],
              ids: dict, signal: dict, client) -> tuple[list[dict], object]:
    """One gateway call over `sent` and its llm_call_log row, AA-927 canonical shape: a usable reply writes one row
    with `json_parsed: true`; a billed reply that is unusable, or any raise after generate() returned, writes one
    `billed_unusable` row and re-raises (the exception carries `fit_billed_cost_usd`). Returns (verdicts, reply)."""
    resp = client.generate(build_request(sent, fit_mode=fit_mode, stage=stage, model_tier=model_tier))
    signal = {"issue": "AA-779", "fit_mode": fit_mode, "n_pairs": len(sent), **signal}
    try:
        verdicts = parse_fit_verdicts(resp.content, len(sent))
        counts = dict(collections.Counter(v["verdict"] for v in verdicts))
    except Exception as e:
        # AA-927: the reply was billed; its billed_unusable row (no ok-rate key) before the caller falls back.
        record_unusable_sync(resp, stage=stage, role=role, error=e, quality_signal_extra=signal, **ids)
        try:
            e.fit_billed_cost_usd = float(getattr(resp, "cost_usd", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 — an exception type that refuses attributes only loses the cost note
            pass
        raise
    record_call_sync(
        stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal=signal | {"json_parsed": True, "verdicts": counts},
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), provider=getattr(resp, "provider", None),
        **ids, **billed_fields(resp),
    )
    return verdicts, resp


def check_answer_fit(pairs: Sequence[tuple[str, str]], *, fit_mode: Optional[str] = None, stage: str = FIT_STAGE,
                     role: str = FIT_ROLE, model_tier: Optional[str] = None, tenant_id=None, content_piece_id=None,
                     angle_gate_request_id=None, quality_signal: Optional[dict] = None,
                     client: Optional[LLMClient] = None, concurrency: Optional[int] = None,
                     deadline_s: Optional[float] = None) -> FitOutcome:
    """Sync. A verdict per pair for the first FIT_MAX_PAIRS of `pairs`, in order; `fit_mode` None = FIT_MODE.

    batched: one gateway call; any error raises (the caller keeps the FAQ unchanged).
    isolated: one call per pair, at most `concurrency` (FIT_CONCURRENCY) at once, each in a copy of the caller's
    contextvars. A pair whose call raises, or that has no reply after `deadline_s` (None = wait), keeps verdict
    None and is listed in `errors`; FitAllPairsFailed only when no pair got a verdict.

    One llm_call_log row per call (`_fit_call`). `stage` / `model_tier` are overridable for an ad-hoc measurement
    (stage `adhoc_aa779`, no route row → `model_tier` names the model); otherwise the stage row decides."""
    fit_mode = fit_mode or FIT_MODE
    if fit_mode not in FIT_MODES:
        raise ValueError(f"unknown answer-fit mode {fit_mode!r}")
    sent = [(q, a) for q, a in pairs][:FIT_MAX_PAIRS]
    if not sent:
        return FitOutcome(fit_mode=fit_mode)
    client = client or LLMClient()
    ids = {"tenant_id": tenant_id, "content_piece_id": content_piece_id,
           "angle_gate_request_id": angle_gate_request_id}
    call = dict(fit_mode=fit_mode, stage=stage, role=role, model_tier=model_tier, ids=ids, client=client)
    if fit_mode == FIT_MODE_BATCHED:
        verdicts, resp = _fit_call(sent, signal=dict(quality_signal or {}), **call)
        rows = [{"n": n, "q": q, **v} for n, ((q, _a), v) in enumerate(zip(sent, verdicts), 1)]
        return FitOutcome(verdicts=rows, model=resp.model_used, cost_usd=float(resp.cost_usd or 0.0),
                          fit_mode=fit_mode, calls=1)
    return _check_isolated(sent, call=call, quality_signal=quality_signal or {},
                           concurrency=concurrency or FIT_CONCURRENCY, deadline_s=deadline_s)


def _check_isolated(sent: list, *, call: dict, quality_signal: dict, concurrency: int,
                    deadline_s: Optional[float]) -> FitOutcome:
    def one(n: int, pair: tuple[str, str]):
        verdicts, resp = _fit_call([pair], signal={**quality_signal, "pair_n": n, "piece_pairs": len(sent)},
                                   **call)
        return verdicts[0], resp

    pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(sent))))
    try:
        futures = [pool.submit(contextvars.copy_context().run, one, n, pair) for n, pair in enumerate(sent, 1)]
        done, _pending = futures_wait(futures, timeout=deadline_s)
    finally:
        # never block on a slow pair past the deadline: queued pairs are cancelled, a running one finishes (and
        # writes its own row) in the background, its verdict unused. Those threads are not daemons and nothing
        # bounds them across pieces (at most FIT_CONCURRENCY per piece; the batched to_thread path is the same).
        pool.shutdown(wait=False, cancel_futures=True)
    started = sum(1 for f in futures if not f.cancelled())
    rows, errors, models, cost = [], [], [], 0.0
    for n, ((q, _a), fut) in enumerate(zip(sent, futures), 1):
        if fut not in done:
            err = f"TimeoutError: no reply within {deadline_s}s"
        elif fut.exception() is None:
            v, resp = fut.result()
            rows.append({"n": n, "q": q, **v, "model": resp.model_used})
            models.append(resp.model_used)
            cost += float(resp.cost_usd or 0.0)
            continue
        else:
            exc = fut.exception()
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            cost += float(getattr(exc, "fit_billed_cost_usd", 0.0) or 0.0)
        rows.append({"n": n, "q": q, "verdict": None, "reason": "", "error": err})
        errors.append({"n": n, "q": q, "error": err})
    if not models:
        raise FitAllPairsFailed(f"no pair got a verdict ({len(errors)} failed): {errors[0]['error']}")
    model = collections.Counter(models).most_common(1)[0][0]
    return FitOutcome(verdicts=rows, model=model, cost_usd=cost, fit_mode=FIT_MODE_ISOLATED, calls=started,
                      errors=errors)


async def apply_answer_fit(content_text: str, *, context: dict, piece_id=None, request_id=None,
                           mode: Optional[str] = None, fit_mode: Optional[str] = None) -> tuple[str, dict]:
    """The blog text after the answer-fit step, and the admin record of it ({} when the flag is off).

    `mode` is the flag (T9_FAQ_LLM_FILTER), `fit_mode` how the pairs are sent (None = FIT_MODE).
    `off`: no call, text unchanged. No FAQ pair: no call, a record with `pairs: 0`. `shadow`: verdicts recorded,
    text unchanged. `on`: the failing pairs' blocks are cut from the FAQ (quality_gates.drop_faq_pairs, the rest of
    the text verbatim); `below_min` says fewer than FAQ_MIN_PAIRS are left; a failing LAST pair with more than
    one paragraph is kept and listed in `kept_multi_paragraph`. Any failure or timeout of the step: text
    unchanged, `fallback: true`. Isolated mode: a pair whose own call failed is kept and listed in `pair_errors`."""
    mode = mode or faq_filter_mode()
    if mode == MODE_OFF:
        return content_text, {}
    fit_mode = fit_mode or FIT_MODE
    pairs = faq_pairs(content_text)
    record: dict = {"mode": mode, "fit_mode": fit_mode, "stage": FIT_STAGE, "pairs": len(pairs)}
    if not pairs:
        return content_text, record
    try:
        outcome = await asyncio.wait_for(asyncio.to_thread(
            check_answer_fit, pairs, fit_mode=fit_mode, tenant_id=context.get("tenant_id"),
            content_piece_id=str(piece_id) if piece_id else None,
            angle_gate_request_id=str(request_id) if request_id else None,
            quality_signal={"mode": mode},
            deadline_s=min(PAIR_DEADLINE_S, TIMEOUT_S * 0.9) if fit_mode == FIT_MODE_ISOLATED else None),
            timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — any failure: the FAQ is delivered unchanged
        err = f"{type(e).__name__}: {str(e)[:200]}"
        logger.warning("t9_faq_answer_fit_fallback", mode=mode, fit_mode=fit_mode, error=err,
                       request_id=str(request_id) if request_id else None)
        return content_text, record | {"fallback": True, "error": err}
    failing = [v for v in outcome.verdicts if v["verdict"] in FAILING]
    record |= {"fallback": False, "calls": outcome.calls, "model": outcome.model,
               "cost_usd": round(outcome.cost_usd, 6), "verdicts": outcome.verdicts,
               "failing": [v["q"] for v in failing]}
    if outcome.errors:
        record["pair_errors"] = outcome.errors
        logger.warning("t9_faq_answer_fit_pair_errors", mode=mode, kept=len(outcome.errors),
                       error=outcome.errors[0]["error"], request_id=str(request_id) if request_id else None)
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

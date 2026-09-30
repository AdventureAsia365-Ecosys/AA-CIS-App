"""AA-701 — Jev questions next to the T10 gates, for Jev allow-listed tenants (design §4.3 T10-1…5).

Observe only: every answer lands in `shared.decision_log` next to the gate results of the same
attempt, and nothing here changes a gate outcome. Each question starts in shadow and is calibrated
from the rerun's log; wiring an enforced verdict into its gate is a follow-up per question, because
each gate acts differently (F8/F9-style warn, F9-CTA / cannibalization block, promises flag).

- T10-1 `t10_rubric_met`       per F8 rubric item   (replaces the GPT F8 call once calibrated)
- T10-2 `t10_brand_voice`      F9 style half        (warn today)
        `t10_cta_clear`        F9 CTA half          (blocking today)
- T10-3 `t10_same_piece`       nearest other-tenant piece at cosine ≥ 0.85 (the gate blocks at 0.92)
- T10-4 `t10_offer_as_certain` each sentence gate_promises_an_option flags
- T10-5 `t10_faq_restates`     each FAQ answer of a blog piece

Tenant guard: `decide()` skips non-allow-listed tenants. T10-3 sends a second tenant's piece, so it
is asked only when that tenant is allow-listed too. Never raises.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Optional

import structlog

from services.acp_content_writing.framework_rubrics import get_framework_rubric
from services.acp_content_writing.quality_gates import (
    _FAQ_ANSWER_RE, promised_sentences, strip_citation_tags,
)

logger = structlog.get_logger()

STAGE = "t10_gates"
Q_RUBRIC = "t10_rubric_met"
Q_VOICE = "t10_brand_voice"
Q_CTA = "t10_cta_clear"
Q_SAME = "t10_same_piece"
Q_OFFER = "t10_offer_as_certain"
Q_FAQ = "t10_faq_restates"
SAME_PIECE_FLOOR = 0.85
_CONCURRENCY = 4
_PIECE_CHARS = 14000


def _h(text: str, n: int = 12) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


def build_asks(*, content_text: str, atom_text: str, goal_key: str, cta: Optional[str],
               brand_rubric_text: str, channel: str,
               route_segments: Optional[list[tuple[str, str]]] = None) -> list[tuple[str, str, dict]]:
    """(question_key, subject_key, state) for one attempt, same-tenant questions only."""
    piece = strip_citation_tags(content_text or "")[:_PIECE_CHARS]
    ph = _h(piece)
    asks: list[tuple[str, str, dict]] = []
    for item in get_framework_rubric(goal_key) or []:
        asks.append((Q_RUBRIC, f"t10:rubric:{ph}:{_h(item, 8)}", {"post": piece, "criterion": item}))
    if brand_rubric_text:
        asks.append((Q_VOICE, f"t10:voice:{ph}:{_h(brand_rubric_text, 8)}",
                     {"post": piece, "brand": brand_rubric_text[:4000]}))
    if cta and cta.strip():
        asks.append((Q_CTA, f"t10:cta:{ph}:{_h(cta, 8)}", {"post": piece, "call_to_action": cta}))
    for sent, _, offered in promised_sentences(content_text, atom_text, route_segments):
        asks.append((Q_OFFER, f"t10:offer:{_h(offered, 8)}:{_h(sent)}",
                     {"offered_moment": offered[:2000], "sentence": sent}))
    if channel == "blog" and "## FAQ" in piece:
        body, _, faq = piece.partition("## FAQ")
        for m in _FAQ_ANSWER_RE.finditer(faq):
            answer = m.group(1).strip()
            if answer:
                asks.append((Q_FAQ, f"t10:faq:{_h(body)}:{_h(answer)}", {"body": body[:10000], "faq_answer": answer}))
    return asks


async def _other_piece_text(pool, piece_id: str) -> Optional[str]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT content_text FROM acp_shared.content_piece WHERE piece_id = $1::uuid", str(piece_id))


async def observe_t10(*, tenant_id: Optional[str], attempt_key: str, content_text: str, atom_text: str,
                      goal_key: str, cta: Optional[str], brand_rubric_text: str, channel: str,
                      route_segments: Optional[list[tuple[str, str]]] = None,
                      nearest_other: Optional[dict] = None, pool=None) -> dict:
    """Ask the T10 questions for one attempt. `nearest_other` = {piece_id, tenant_id, similarity}
    of the closest other-tenant piece (any similarity). Returns {asked, skipped}. Never raises."""
    if not tenant_id:
        return {"asked": 0, "skipped": True}
    try:
        from shared.llm_client.decide import decide, tenant_allowed

        asks = build_asks(content_text=content_text, atom_text=atom_text, goal_key=goal_key, cta=cta,
                          brand_rubric_text=brand_rubric_text, channel=channel, route_segments=route_segments)
        if not asks:
            return {"asked": 0, "skipped": False}
        first = await decide(STAGE, asks[0][1], asks[0][2], [asks[0][0]], tenant_id=tenant_id, pool=pool)
        v = first.verdicts.get(asks[0][0])
        if v is not None and v.zone == "skipped" and v.mode != "off":
            return {"asked": 0, "skipped": True}             # tenant not allow-listed
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def one(q, key, state):
            async with sem:
                await decide(STAGE, key, state, [q], tenant_id=tenant_id, pool=pool)

        await asyncio.gather(*[one(*a) for a in asks[1:]])
        asked = len(asks)
        if (nearest_other and pool is not None and (nearest_other.get("similarity") or 0) >= SAME_PIECE_FLOOR
                and tenant_allowed(nearest_other.get("tenant_id"))):
            other = await _other_piece_text(pool, nearest_other["piece_id"])
            if other:
                piece = strip_citation_tags(content_text or "")[:_PIECE_CHARS]
                await decide(STAGE, f"t10:same:{_h(piece)}:{nearest_other['piece_id']}",
                             {"post_a": piece, "post_b": strip_citation_tags(other)[:_PIECE_CHARS],
                              "cosine": round(float(nearest_other["similarity"]), 3)},
                             [Q_SAME], tenant_id=tenant_id, pool=pool)
                asked += 1
        logger.info("t10_jev_observed", attempt=attempt_key, asked=asked)
        return {"asked": asked, "skipped": False}
    except Exception as exc:
        logger.warning("t10_jev_observe_failed", attempt=attempt_key, error=str(exc)[:200])
        return {"asked": 0, "skipped": False}


__all__ = ["observe_t10", "build_asks", "SAME_PIECE_FLOOR"]

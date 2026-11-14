"""AA-1077 — the blog length step after the normalizer: the F4 re-check of the text a blog stores, and the one
post-normalizer lengthen pass.

`recheck_length` (S277, moved here from service._recheck_length unchanged; service keeps the name): F4 runs again on
the text the normalizer (service._normalize_final_text) hands back. A fresh failure replaces the ledger's F4 entry
and holds an approved piece (`length_short`); the record goes to `writer_input.length_trace` (admin-only).

`lengthen_after_normalizer` (S280). The normalizer's MANDATORY steps (length_contract.NORMALIZER_STEPS: the
usage-policy cut and the final integrity pass) are allowed to take a blog under the floor (truth and integrity before
length), and the re-check then holds a piece the loop had approved. Before S280 that piece stayed held. Now, when the
lengthening switch is on for the piece (the same pinned value as the in-loop expand, setting
cis.t9.blog_length_extend), it gets ONE targeted lengthen pass:

1. Eligible only when ALL hold: blog; the re-check turned the loop's `approved` into `held` (so F4 short is the only
   hold reason); the text was at or over the floor at the end of the loop and the step that took it under is a
   mandatory one; the switch is on; the piece has an attempt left in its attempt budget (setting cis.t9.max_attempts:
   the pass counts as the piece's next attempt, `attempts_left` > 0).
2. Planned by `length_extend.plan_after_repair` on the stored text with `before` = the loop's text, so the sections
   the cut shortened come first ("a repair cut text here"); facts = the seed's untold fact sentences only.
3. One `generate.extend_sections` call (output capped by its max_tokens; through the gateway, so the tenant AI budget
   applies: a budget stop is re-raised, never swallowed), each paragraph code-checked by `length_extend.extend`.
4. Then the final integrity pass (`text_integrity.final_pass`, the normalizer's last mandatory step) and EVERY gate
   (`run_quality_gates`, the loop's own arguments, attempt > 1) on that text. Accepted only when the integrity pass
   leaves no repeat and every gate passes: the gated text is the stored text. Anything else keeps the normalizer's
   result byte for byte (held `length_short` as before).

The record is `writer_input.length_trace.post_normalizer` (admin-only): why it ran or not, the step that crossed the
floor, the extend log, the cost, the outcome and the failing gates. Switch off = main (no read, no call, the record
says `off`).
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

import structlog

from services.acp_content_writing import length_contract, length_extend, text_integrity
from services.acp_content_writing.quality_gates import expand_ledger, gate_extreme_length
from services.acp_shared.citation_tags import strip_citation_tags
from services.quality import registry as qr
from shared.llm_client.tenant_budget import approval_stop_in, counter_unavailable_in

logger = structlog.get_logger()

LENGTH_TRACE_STEP = "length_trace"
TRACE_KEY = "post_normalizer"
STEP = "post_normalizer"   # the extend call's quality_signal.step (llm_call_log), next to mode length_extend
MANDATORY = "mandatory"


def recheck_length(content_text: str, gate_ledger: list, status: str, held_reason: Optional[str], *,
                   trace: dict, floor_kept: Optional[list] = None) -> tuple[list, str, Optional[str], dict]:
    """AA-1077 — F4 again on the text a blog stores (the last step of _normalize_final_text). S276: 3 of 7 approved
    phase 2 blogs were stored under the 4,700-char floor while their ledger said F4 passed, and 10 of 13 F4 holds
    were stored 192–557 chars shorter than the F4 reading. A fresh F4 failure replaces the ledger's F4 entry and
    holds an approved piece (its label then reads `length_short`); a fresh pass leaves a failed entry as it is
    (status never moves from held to approved here: the hold was decided on the gated text). Returns (ledger,
    status, held_reason, the `length_trace` record for writer_input: chars after each normalizer step, the verdict)."""
    fresh = gate_extreme_length(strip_citation_tags(content_text), "blog")
    side = length_contract.entry_side(fresh)
    entry = {"step": LENGTH_TRACE_STEP, "chars": dict(trace), "floor": length_contract.floor_chars(),
             "target": length_contract.target_chars(), "words": length_contract.words(content_text),
             "verdict": length_contract.code_for(fresh) or "in", "held": False}
    if floor_kept:   # S278 — the optional normalizer steps whose edit the floor stopped (the ledger note)
        entry["floor_kept"] = list(floor_kept)
    if side is None:
        return gate_ledger, status, held_reason, entry
    ledger = list(gate_ledger)
    k = next((i for i, g in enumerate(ledger) if isinstance(g, dict) and g.get("gate") == qr.F4_EXTREME_LENGTH), None)
    if k is None:
        ledger.append(fresh)
    else:
        ledger[k] = {**ledger[k], **fresh}
    if status == "approved":
        status, held_reason = "held", f"{fresh['gate']}: {'; '.join(fresh['violations'][:3])}"
        entry["held"] = True
        logger.info("t9_length_recheck_held", verdict=entry["verdict"], chars=entry["chars"])
    return ledger, status, held_reason, entry


def crossing_step(chars: dict) -> Optional[str]:
    """The normalizer step that took the text from at or over the floor to under it (trace order), else None."""
    floor = length_contract.floor_chars()
    prev = chars.get("loop")
    for step in length_contract.NORMALIZER_STEPS:
        now = chars.get(step)
        if now is None:
            continue
        if prev is not None and prev >= floor > now:
            return step
        prev = now
    return None


def eligibility(trace: Optional[dict], *, loop_status: str, switch: str,
                attempts_left: int) -> tuple[str, Optional[str]]:
    """(reason, crossing step): reason 'lengthen' when the pass runs, else why not: 'not_held_by_recheck', 'off',
    'not_loop_approved', 'not_a_mandatory_cut' or 'no_attempt_left'."""
    if not trace or not trace.get("held"):
        return "not_held_by_recheck", None
    step = crossing_step(trace.get("chars") or {})
    if switch != length_extend.EXTEND_ON:
        return "off", step
    if loop_status != "approved":
        return "not_loop_approved", step
    if step is None or length_contract.NORMALIZER_STEPS.get(step) != MANDATORY:
        return "not_a_mandatory_cut", step
    if attempts_left <= 0:
        return "no_attempt_left", step
    return "lengthen", step


def _guarded(call: Callable, caught: list) -> Callable:
    """`call` whose tenant-budget stop is kept aside: length_extend.extend swallows every call error (the text stays
    as it was), but a budget approval stop or an unreadable counter must still end the job as it does elsewhere."""
    def run(**kw):
        try:
            return call(**kw)
        except Exception as e:  # noqa: BLE001 — re-raised: extend records it and the caller re-raises a budget stop
            if approval_stop_in(e) or counter_unavailable_in(e):
                caught.append(e)
            raise
    return run


def _failing(ledger: list) -> list[str]:
    """The blocking gates a ledger fails (sorted names)."""
    return sorted({g.get("gate") for g in expand_ledger(ledger or [])
                   if not g.get("passed") and g.get("blocking", True)})


def _merged_flags(gate_flags: list, kept: list, final_log: list) -> list:
    """The re-run's own warn flags, then the normalizer's admin flags of gates the re-run does not produce (FAQ
    autofix, final pass, usage-policy cut evidence, …), then this pass's final-pass log."""
    own = {f.get("gate") for f in gate_flags or [] if isinstance(f, dict)}
    out = list(gate_flags or []) + [f for f in kept or [] if not (isinstance(f, dict) and f.get("gate") in own)]
    if final_log:
        out.append({"gate": qr.F13_FINAL_PASS, "passed": False, "blocking": False, "repairable": False,
                    "violations": list(final_log)})
    return out


async def _attempt(norm: dict, plan: dict, record: dict, *, attempt: int, source: str,
                   gates: Callable, call: Callable, hyg, cta: Optional[str], ignore: frozenset, trip_ctx,
                   voice: Optional[str], embed: Optional[Callable], ids: dict) -> dict:
    """The extend call, the final integrity pass and every gate; the normalizer's result when anything fails."""
    caught: list = []
    text, cost, ext = await asyncio.to_thread(
        length_extend.extend, norm["content_text"], plan, call=_guarded(call, caught), source=source, cta=cta,
        ignore=ignore, moments=getattr(hyg, "moments", None), trip_ctx=trip_ctx, voice=voice, channel="blog",
        attempt=attempt + 1, step=STEP, **ids)
    if caught:
        raise caught[0]
    record.update(extend=ext, cost_usd=round(cost, 6))
    out = {**norm, "cost_usd": norm.get("cost_usd", 0.0) + cost}
    if not ext.get("added"):
        record["outcome"] = "held:nothing_added"
        return out
    text, final_log, residue = text_integrity.final_pass(text, hyg)
    if residue:
        record.update(outcome="held:integrity", residue=list(residue)[:5])
        return out
    outcome = await asyncio.to_thread(gates, content_text=text)
    record["chars_after"] = length_contract.measure(text)
    if not outcome.get("passed"):
        record.update(outcome="held:gates", failing=_failing(outcome.get("gate_ledger")))
        return out
    record["outcome"] = "approved"
    embedding = norm.get("embedding")
    if embed is not None and ids.get("tenant_id"):   # as service._after_cut: no tenant, no embedding
        fresh = await asyncio.to_thread(embed, strip_citation_tags(text))
        embedding = fresh if fresh is not None else embedding
    return {**out, "content_text": text, "gate_ledger": outcome["gate_ledger"], "status": "approved",
            "held_reason": None, "embedding": embedding,
            "flags": _merged_flags(outcome.get("flags") or [], norm.get("flags") or [], final_log)}


async def lengthen_after_normalizer(norm: dict, *, loop_text: str, loop_status: str, switch: str,
                                    channel: Optional[str], attempt: int, attempts_left: int, source: str,
                                    context: dict, gates: Callable, call: Callable, hyg, cta: Optional[str] = None,
                                    ignore: frozenset = frozenset(), trip_ctx=None, brand_pack: Optional[dict] = None,
                                    embed: Optional[Callable] = None, tenant_id: Optional[str] = None,
                                    request_id=None, piece_id=None) -> dict:
    """The normalizer's result `norm` (service._normalize_final_text), or that result with the lengthened text, the
    re-run gate ledger and status `approved` (see the module docstring). `gates(content_text=...)` is
    run_quality_gates with the loop's own arguments; `call` is generate.extend_sections; `embed` re-makes the
    embedding of an accepted text (compute_embedding; None = keep). Records `length_trace.post_normalizer`."""
    trace = context.get("length_trace")
    if channel != "blog" or not trace:
        return norm
    reason, step = eligibility(trace, loop_status=loop_status, switch=switch, attempts_left=attempts_left)
    if reason == "not_held_by_recheck":
        return norm
    record: dict = {"reason": reason, "crossed_at": step, "attempt": attempt + 1}
    trace[TRACE_KEY] = record
    if reason != "lengthen":
        return norm
    plan, why = length_extend.plan_after_repair(norm["content_text"], before=loop_text, channel=channel,
                                                source=source, switch=switch)
    record["plan"] = why
    if plan is None:
        record["outcome"] = f"held:{why}"
        return norm
    voice = ", ".join((brand_pack or {}).get("voice") or []) or None
    ids = {"tenant_id": tenant_id, "angle_gate_request_id": str(request_id) if request_id else None,
           "content_piece_id": str(piece_id) if piece_id else None}
    out = await _attempt(norm, plan, record, attempt=attempt, source=source, gates=gates,
                         call=call, hyg=hyg, cta=cta, ignore=ignore, trip_ctx=trip_ctx, voice=voice, embed=embed,
                         ids=ids)
    if out["status"] == "approved":
        trace.update(held=False, verdict="in")
        trace.setdefault("chars", {})[TRACE_KEY] = record.get("chars_after")
    logger.info("t9_length_post_normalizer", outcome=record.get("outcome"), crossed_at=step,
                cost_usd=record.get("cost_usd"), chars_after=record.get("chars_after"))
    return out


__all__ = ["LENGTH_TRACE_STEP", "TRACE_KEY", "STEP", "recheck_length", "crossing_step", "eligibility",
           "lengthen_after_normalizer"]

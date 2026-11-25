"""AA-1154 phase A0 — the CIS admin assistant (ops assist): evidence, action catalogue and rule-based playbooks, no LLM.

Tech lead decisions AI1-AI12 (S285 advisory, root docs/audits/2026-11-19-S285-session/advisory-ai-assistants.md
sections 3-8). For a Needs-attention item of the Ecosystem status page (AA-1140) or a signal the assistant sees on
its own (an AI connection refusing at account level, throttled, or spending well above its median), it:

1. gathers evidence from existing sources only, read-only and $0 (:mod:`.evidence`: the status page's own build, the
   AI gateway breaker and ledger, the route config, jobs, waves, the connection probe, the ops events);
2. picks a playbook by rule and turns the evidence into ranked action cards (:mod:`.playbooks`), each with its reason,
   evidence links, risk R0-R4 computed by rule (:mod:`.risk`: direction tighten / loosen with
   ``settings_registry.loosened()``, scope, money, reversible), predictions with a sourced confidence, "if we do
   nothing", the way back, the cost and who approves (the capability of the API it calls, :mod:`.catalog`);
3. adds a plain-words Summary for the business owner (:mod:`.summary`; AI10: the business owner gets the Summary and
   no cards).

Nothing is executed (AI5): the cards name the API call a person would make; A1 adds the buttons, which call the
existing APIs with the clicker's own session. A2 (:mod:`.outcome`): a card the person ran is marked as done, its
measures are read again at the minutes of ``cis.ops.assist_check_after_min`` by the status sampler, and the hit rate
("right n of m") feeds the status page, the OPS-12 digest and each card's track record. Each evidence read is bounded
by ``cis.ops.assist_build_timeout_s`` (:func:`collect_in_time`, fail soft: 503 "try again", never a hang). No LLM
call in A0 (``"llm": {"calls": 0}`` in every answer). Every limit is a setting (``shared/ops_assist_settings.py``,
CIS 481, 487, 491).

``analyse_snapshot`` is pure, so a recorded snapshot replays to the same cards: :mod:`.replays` holds the 29/10
incident (Bedrock refused every model at account level) set on today's routes, served by
``GET /admin/ops-assist/replays/{case}`` and checked in CI.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from api.services.ops_assist import catalog, evidence, playbooks, replays, risk, summary
from shared import ops_assist_settings


class EvidenceTimeout(RuntimeError):
    """A2 (fail soft): the evidence of one item took longer than ``cis.ops.assist_build_timeout_s`` (S292: one hang of
    more than 5 minutes while a deploy replaced the API tasks). The API answers 503 with a "try again" message."""


def limits() -> dict:
    return ops_assist_settings.snapshot()


async def collect_in_time(pool: Any, redis: Any, lim: dict, **kw: Any) -> dict:
    """``evidence.collect`` bounded by the setting ``cis.ops.assist_build_timeout_s``; :class:`EvidenceTimeout` past
    it (every read of the snapshot is cancelled, nothing is half-shown)."""
    try:
        return await asyncio.wait_for(evidence.collect(pool, redis, limits=lim, **kw),
                                      timeout=float(lim["build_timeout_s"]))
    except asyncio.TimeoutError as e:
        raise EvidenceTimeout(f"evidence not read within {lim['build_timeout_s']} s") from e


def _signal_item(sig: dict) -> dict:
    return {"id": sig["id"], "severity": sig["severity"], "app": sig["app"], "title": sig["title"],
            "detail": sig.get("detail"), "since": None, "source": "assistant signal", "status": "open", "links": [],
            "msg": None}


def analyse_snapshot(snapshot: dict, lim: dict, *, business_owner: bool = False) -> dict:
    """Cards, evidence and Summary of one snapshot. Pure: the live endpoint and the replay both use it."""
    s = dict(snapshot)
    if s.get("item") is None:
        sig = next((x for x in playbooks.signals(s, lim) if x["id"] == s.get("item_id")), None)
        if sig is not None:
            s["item"] = _signal_item(sig)
    ev = evidence.evidence_items(s)
    out = playbooks.cards(s, lim, {e["id"] for e in ev})
    title = (s.get("item") or {}).get("title")
    full = {
        "item_id": s.get("item_id"), "as_of": s.get("as_of"), "range": s.get("range"),
        "item": s.get("item"), "playbook": out["playbook"], "context": out["context"], "signal": out["signal"],
        "evidence": ev, "cards": out["cards"], "not_proposed": out["not_proposed"],
        "summary": summary.build(out["playbook"], out["cards"], title=title),
        "limits": lim, "limit_keys": ops_assist_settings.keys(), "unavailable": s.get("unavailable") or {},
        "llm": {"used": False, "calls": 0},
    }
    if business_owner:   # AI10: plain words only, no action cards, no buttons
        return {**{k: full[k] for k in ("item_id", "as_of", "range", "item", "playbook", "summary", "llm")},
                "audience": "summary", "cards": [], "evidence": [], "not_proposed": []}
    return {**full, "audience": "full"}


async def analyse(pool: Any, redis: Any, item_id: str, *, range_key: str = "24h", authorization: Optional[str] = None,
                  business_owner: bool = False, now: Optional[datetime] = None) -> dict:
    lim = limits()
    snap = await collect_in_time(pool, redis, lim, item_id=item_id, range_key=range_key, authorization=authorization,
                                 now=now)
    return analyse_snapshot(snap, lim, business_owner=business_owner)


async def list_signals(pool: Any, redis: Any, *, now: Optional[datetime] = None) -> dict:
    """The assistant's own signals (not on the status page's Needs-attention list yet), from the gateway reads."""
    lim = limits()
    snap = await collect_in_time(pool, redis, lim, item_id="", now=now, include_status=False)
    return {"as_of": snap["as_of"], "signals": playbooks.signals(snap, lim), "unavailable": snap["unavailable"],
            "window_min": lim["evidence_window_min"]}


def catalogue() -> dict:
    """The action catalogue with each action's base risk (no stages, no estimate) and who approves it."""
    lim = limits()
    rows = []
    for a in catalog.ACTIONS:
        rk = risk.assess(a, lim)
        rows.append({**a.view(), "risk": rk, "approval": playbooks.approval(a, rk["level"], lim)})
    return {"actions": rows, "limits": lim, "limit_keys": ops_assist_settings.keys(),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def replay(case: str, *, business_owner: bool = False) -> dict:
    """A recorded incident replayed through today's rules (KeyError for an unknown case)."""
    snap = replays.load(case)
    out = analyse_snapshot(snap["snapshot"], limits(), business_owner=business_owner)
    return {**out, "replay": {k: v for k, v in snap.items() if k != "snapshot"}}


__all__ = ["EvidenceTimeout", "analyse", "analyse_snapshot", "catalogue", "collect_in_time", "list_signals", "replay"]

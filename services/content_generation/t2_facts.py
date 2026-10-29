"""AA-814 follow-up — Facts Entries for the T2 tenant tour rewrite, behind the T2_FACTS switch (default off).

The T9 post writer reads `acp_shared.facts` since AA-529 (services/acp_content_writing/facts.py). T2 had no facts
input. With T2_FACTS on, the t2_rewrite job reads the same facts T9 would for the tour's country (the platform facts
plus the writing tenant's own, never another tenant's; expired facts left out; a tenant fact replaces the platform
facts on its subject; the AA-814 budget) and puts them on the T2 source as ``tour["facts"]``:

- the writer USER prompt gains a FACTS block (prompts.build_rewrite_prompt ``facts``), only on a T2 rewrite;
- the T3 grounding baseline (every value of the source dict) and the AA-795 brand-claim check read it too, so a
  price or season copied from a fact is supported, not a novel number;
- the T3 repair rounds re-enter the graph with the same source dict, so they keep the block.

Off (unset or any value but 1/true/on/yes), or a tenant and country with no facts: the source dict has no "facts"
key and every T2 prompt is byte-identical to before. A facts read error is logged and the rewrite runs without
facts: the facts are optional input, unlike the brand pack (AA-741). Switch: worker task def env, read per job.

T2 has no citations, so the block lists ``<subject>: <fact>`` lines without the T9 ``[Fact id=…]`` head or the
``Source:`` line: no id or source label can leak into a tour page, and a fact id's hex digits never reach the
grounding baseline as numbers.
"""
from __future__ import annotations

import os
from uuid import UUID

import structlog

logger = structlog.get_logger()

T2_FACTS_ENV = "T2_FACTS"
FACTS_KEY = "facts"   # the key on the T2 source dict (t2_rewrite_job.source_tour_dict)
_ON = ("1", "true", "on", "yes")


def enabled() -> bool:
    """T2_FACTS (worker task def env, read per call): on only for 1 / true / on / yes. Default off."""
    return os.environ.get(T2_FACTS_ENV, "").strip().lower() in _ON


def facts_lines(facts: list[dict]) -> str:
    """The facts as one ``- <title>: <body>`` line each (whitespace collapsed), in the order given. "" for none."""
    lines = []
    for f in facts:
        title = " ".join(str(f.get("title") or "").split())
        body = " ".join(str(f.get("body") or "").split())
        if title and body:
            lines.append(f"- {title}: {body}")
        elif title or body:
            lines.append(f"- {title or body}")
    return "\n".join(lines)


async def with_facts(pool, tour: dict, tenant_id: str) -> tuple[dict, dict]:
    """(source dict, diag). With the switch on and facts for this tenant and the tour's country, a copy of ``tour``
    with ``tour["facts"]`` set; otherwise ``tour`` itself. A tour without a country gets the any-country facts only
    (never every country's). ``diag`` goes into job.result (``facts``) and is ``{"mode": "off"}`` with the switch
    off."""
    if not enabled():
        return tour, {"mode": "off"}
    from services.acp_content_writing.facts import fetch_facts_for_writing
    country = str(tour.get("country") or "").strip()
    try:
        facts = await fetch_facts_for_writing(UUID(str(tenant_id)), pool, country=country)
    except Exception as e:  # noqa: BLE001 — optional input: rewrite without facts, say so in job.result
        logger.warning("t2_facts_failed", tenant_id=str(tenant_id), country=country, error=str(e)[:200])
        return tour, {"mode": "on", "error": str(e)[:300]}
    text = facts_lines(facts)
    diag = {"mode": "on", "facts": len(facts), "tenant_facts": sum(1 for f in facts if f.get("scope") == "tenant"),
            "chars": len(text)}
    # Same trace as T9's t9_facts_seed: the facts block size per write, to explain T2 input cost in llm_call_log.
    logger.info("t2_facts_seed", tenant_id=str(tenant_id), country=country or None, **diag)
    if not text:
        return tour, diag
    return {**tour, FACTS_KEY: text}, diag


__all__ = ["FACTS_KEY", "T2_FACTS_ENV", "enabled", "facts_lines", "with_facts"]

"""AA-772 — place cards: what a real place is, where it is, how to name it, and a few checked
background facts the T8/T9 writer may cite (migration 220: acp_shared.place_card +
acp_shared.atom_place_card).

Why: the A-series has no city, region or display name for a place — an atom carries the landmark
as the itinerary names it (`tour_atoms.place`) and the tour carries one country. S232: 12/20
Pawangka posts put the monastery in Luang Prabang, Ladakh or "a monastery in China"; the writer
padded thin seeds with unsourced "widely known background" that the F9 judge then flagged.

How a card is made (once per atom, in a job — never in an HTTP request):
1. `a3_place_card` (writer route) gets the atom's place, its itinerary sentence, the tour name and
   country, and returns the place's identity (display name, locality, region, country, naming
   note) plus 6-10 short background facts.
2. `a3_place_card_check` (judge route, another vendor) checks the identity and each fact; only
   facts it marks "supported" are kept. Nothing about the trip itself (prices, hours, what the
   tour does) is ever a card fact.
3. Same real place → same card: `place_key` = normalised display name | locality-or-region |
   country. The atom is linked to its card in `atom_place_card`.
Admins can mark a card verified or rejected (Content Quality page); a rejected card is never
shown to a writer.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

import structlog
from json_repair import repair_json

from shared.llm_client.call_log import record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

MAX_FACTS = 10
_CONCURRENCY = 4
FACT_KINDS = ("what_it_is", "history", "setting", "visiting", "practical", "naming")

_CARD_SYSTEM = """You write a short reference card about one real place that appears in a travel \
itinerary. The card is used by writers who must not get basic facts wrong.

Return ONLY JSON:
{"display_name": "...", "locality": "...", "region": "...", "country": "...",
 "naming_note": "...", "facts": [{"text": "...", "kind": "..."}]}

- display_name: the place's usual English name (fix spelling variants, e.g. "Pawangka" -> the \
common spelling, mentioning the variant in naming_note).
- locality: the town or city it is in or next to; region: province / state / historical region. \
Empty string when it does not apply or you are not sure.
- country: the country it is in today.
- naming_note: one sentence on how to name the location for an international travel audience \
(e.g. a widely used region name), and any spelling variants. Empty string if nothing to say.
- facts: 6-10 short, standalone, well-established facts about the PLACE ITSELF (what it is, its \
history and era, its setting, what visitors typically see there, practical or safety notes that \
hold year-round). kind is one of: what_it_is, history, setting, visiting, practical, naming.
- Only facts you are confident are widely documented. No prices, fees, opening hours, \
visitor numbers, rankings or superlatives, and nothing about the tour or trip itself. \
Write a number only if it is a well-established fact (e.g. a founding century).
- If the itinerary name is too generic to identify one real place (e.g. "the market", \
"hotel"), return {"display_name": "", "facts": []}."""

_CHECK_SYSTEM = """You check a reference card about a real place. For the place identity and \
for each numbered fact, answer whether it is accurate and well-established.

Return ONLY JSON: {"identity_ok": true/false, "verdicts": ["supported" | "unsure" | "wrong", ...]}
- identity_ok: the display name, locality, region and country are correct for the place the \
itinerary means.
- One verdict per numbered fact, same order. "supported" only when you are confident the fact \
is correct and widely documented; "unsure" when you cannot confirm it; "wrong" when it is false."""


_ATOM_PLACE_QUERY = """
    SELECT ta.atom_id, ta.place, ta.evidence, ta.itinerary_day, rt.country, pt.aa_name AS tour_name
    FROM acp_contract.v_active_tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = ta.tour_id
    WHERE ta.atom_id = ANY($1::text[]) AND NOT ta.deleted AND NOT ta.is_empty_marker
      AND ta.place IS NOT NULL AND btrim(ta.place) <> ''
"""

CARD_FOR_ATOM_QUERY = """
    SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note,
           pc.facts, pc.status
    FROM acp_shared.atom_place_card apc
    JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id
    WHERE apc.atom_id = $1 AND pc.status <> 'rejected'
"""


def _norm(s: Optional[str]) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def place_key(display_name: str, locality: Optional[str], region: Optional[str], country: str) -> str:
    return f"{_norm(display_name)}|{_norm(locality or region)}|{_norm(country)}"


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = repair_json(text, return_objects=True)
    return data if isinstance(data, dict) else {}


def _clean_facts(raw_facts) -> list[dict]:
    out = []
    for f in raw_facts if isinstance(raw_facts, list) else []:
        if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip():
            kind = f.get("kind") if f.get("kind") in FACT_KINDS else "what_it_is"
            out.append({"text": " ".join(f["text"].split()), "kind": kind})
    return out[:MAX_FACTS]


def _trim(text: Optional[str], limit: int = 600) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def draft_card(atom: dict) -> tuple[dict, float, object]:
    """One `a3_place_card` call. Returns (card dict, cost, response)."""
    user = (f"PLACE AS THE ITINERARY NAMES IT: {atom['place']}\n"
            f"COUNTRY OF THE TOUR: {atom.get('country') or 'unknown'}\n"
            f"TOUR: {atom.get('tour_name') or 'unknown'}\n"
            f"ITINERARY SENTENCE: {_trim(atom.get('evidence'))}")
    resp = LLMClient().generate(LLMRequest(system_prompt=_CARD_SYSTEM, user_prompt=user,
                                           stage="a3_place_card", max_tokens=2048))
    return _parse_json(resp.content), resp.cost_usd, resp


def check_card(card: dict, atom: dict) -> tuple[bool, list[str], float, object]:
    """One `a3_place_card_check` call. Returns (identity_ok, verdict per fact, cost, response)."""
    facts = card.get("facts") or []
    numbered = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1))
    user = (f"ITINERARY NAME: {atom['place']} (tour in {atom.get('country') or 'unknown'}; "
            f"sentence: {_trim(atom.get('evidence'), 300)})\n"
            f"CARD IDENTITY: {card.get('display_name')} — locality: {card.get('locality') or '-'}, "
            f"region: {card.get('region') or '-'}, country: {card.get('country')}\n"
            f"FACTS:\n{numbered}")
    resp = LLMClient().generate(LLMRequest(system_prompt=_CHECK_SYSTEM, user_prompt=user,
                                           stage="a3_place_card_check", max_tokens=1024))
    data = _parse_json(resp.content)
    verdicts = data.get("verdicts") if isinstance(data.get("verdicts"), list) else []
    verdicts = [str(v).strip().lower() for v in verdicts]
    if len(verdicts) != len(facts):
        verdicts = ["unsure"] * len(facts)  # misaligned reply → keep nothing
    return bool(data.get("identity_ok")), verdicts, resp.cost_usd, resp


async def _log(pool, stage: str, role: str, resp, quality_signal: dict) -> None:
    await record_call_with_pool(
        pool, stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal=quality_signal,
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
    )


async def build_card_for_atom(atom: dict, pool) -> Optional[str]:
    """Draft + check one card and link the atom to it. Returns card_id, or None when the place
    could not be identified or the checker rejected the identity (the atom stays unlinked and
    is tried again next time)."""
    card, cost, resp = await asyncio.to_thread(draft_card, atom)
    await _log(pool, "a3_place_card", "writer", resp, {"atom_id": atom["atom_id"], "place": atom["place"]})
    name = (card.get("display_name") or "").strip() if isinstance(card.get("display_name"), str) else ""
    country = (card.get("country") or atom.get("country") or "").strip()
    card["facts"] = _clean_facts(card.get("facts"))
    if not name or not country:
        return None
    ok, verdicts, cost2, resp2 = await asyncio.to_thread(check_card, card, atom)
    kept = [f for f, v in zip(card["facts"], verdicts) if v == "supported"]
    await _log(pool, "a3_place_card_check", "judge", resp2,
               {"atom_id": atom["atom_id"], "identity_ok": ok, "facts": len(card["facts"]), "kept": len(kept)})
    if not ok:
        logger.info("place_card_identity_rejected", atom_id=atom["atom_id"], place=atom["place"], card=name)
        return None
    locality = (card.get("locality") or "").strip() or None
    region = (card.get("region") or "").strip() or None
    key = place_key(name, locality, region, country)
    async with pool.acquire() as conn, conn.transaction():
        card_id = await conn.fetchval(
            """
            INSERT INTO acp_shared.place_card
                (place_key, display_name, locality, region, country, naming_note, facts, cost_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            ON CONFLICT (place_key) DO UPDATE SET updated_at = acp_shared.place_card.updated_at
            RETURNING card_id
            """,
            key, name, locality, region, country, (card.get("naming_note") or "").strip() or None,
            json.dumps(kept), round(cost + cost2, 6),
        )
        await conn.execute(
            """
            INSERT INTO acp_shared.atom_place_card (atom_id, card_id) VALUES ($1, $2)
            ON CONFLICT (atom_id) DO UPDATE SET card_id = EXCLUDED.card_id, resolved_at = now()
            """,
            atom["atom_id"], card_id,
        )
    return str(card_id)


async def ensure_place_cards(atom_ids: list[str], pool) -> int:
    """Build a card for every atom in `atom_ids` that has none yet. Soft-fails per atom (a card
    is an aid, never a reason to fail a write). Returns how many atoms got a new card."""
    ids = [a for a in dict.fromkeys(atom_ids) if a]
    if not ids:
        return 0
    async with pool.acquire() as conn:
        linked = {r["atom_id"] for r in await conn.fetch(
            "SELECT atom_id FROM acp_shared.atom_place_card WHERE atom_id = ANY($1::text[])", ids)}
        atoms = [dict(r) for r in await conn.fetch(_ATOM_PLACE_QUERY, [a for a in ids if a not in linked])]
    if not atoms:
        return 0
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(atom: dict) -> bool:
        async with sem:
            try:
                return await build_card_for_atom(atom, pool) is not None
            except Exception as e:  # noqa: BLE001 — soft-fail per atom
                logger.warning("place_card_build_failed", atom_id=atom["atom_id"], error=str(e)[:200])
                return False

    return sum(await asyncio.gather(*(one(a) for a in atoms)))


def render_card(card) -> tuple[str, list[str]]:
    """(location line, background fact lines) for the seed."""
    where = ", ".join(p for p in (card["display_name"], card["locality"], card["region"], card["country"]) if p)
    facts = card["facts"]
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return where, [f["text"] for f in facts or [] if isinstance(f, dict) and f.get("text")]


__all__ = ["ensure_place_cards", "build_card_for_atom", "render_card", "place_key", "CARD_FOR_ATOM_QUERY",
           "draft_card", "check_card"]

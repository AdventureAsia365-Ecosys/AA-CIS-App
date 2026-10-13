"""AA-772 — place cards: what a real place is, where it is, how to name it, and a few checked
background facts the T8/T9 writer may cite (migration 220: acp_shared.place_card +
acp_shared.atom_place_card; migration 221: place_card_miss + place_card_alias).

Why: the A-series has no city, region or display name for a place — an atom carries the landmark
as the itinerary names it (`tour_atoms.place`) and the tour carries one country. S232: 12/20
Pawangka posts put the monastery in Luang Prabang, Ladakh or "a monastery in China"; the writer
padded thin seeds with unsourced "widely known background" that the F9 judge then flagged.

How a card is made (once per PLACE, in a job — never in an HTTP request, and never in T9):
0. Free filters first (S234): `card_skip_reason()` drops unnamed places ("ruins of an ancient
   monastery", "your hotel" — the ranking rule `names_somewhere`) and logistics places (hotels,
   camps, airports, border gates); atoms are grouped by (`place_norm`, tour country) so one place
   is drafted once; an alias or core-name match links an existing card with no call; a stored
   miss (`place_card_miss`) is not retried before its `retry_after`.
1. `a3_place_card` (writer route) gets the place, its itinerary sentence(s), the tour name and
   the country the tour is sold as, and returns the place's identity (display name, locality,
   region, country, naming note, aliases) plus 4-6 short background facts.
2. `a3_place_card_check` (judge route, another vendor) checks the identity and each fact; only
   facts it marks "supported" are kept. Nothing about the trip itself (prices, hours, what the
   tour does) is ever a card fact.
3. Same real place → same card: `place_key` = core name | country (S234: no locality — it is LLM
   free text and split one lake into 5 cards). Every atom of the group is linked in
   `atom_place_card`; the itinerary spelling and the draft's aliases go to `place_card_alias`.
Admins can mark a card verified or rejected, and merge duplicates (Content Quality page); a
rejected card is never shown to a writer.

S233 baseline (llm_call_log): 169 Sonnet drafts for 74 cards, $1.30; one unnamed atom was
drafted 35 times by T9 jobs (no negative cache). Guard: ≤1 draft per place group per call.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from json_repair import repair_json

from services.acp_contract.ranking_reference import names_somewhere
from shared.llm_client.call_log import record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

MAX_FACTS = 6
MAX_ALIASES = 6
DRAFT_MAX_TOKENS = 900
_CONCURRENCY = 4
FACT_KINDS = ("what_it_is", "history", "setting", "visiting", "practical", "naming")

# Bump when card_skip_reason()/place_norm() change: stored misses written under an older rule are
# then tried again (the PROMPT_SHA below does the same for a prompt change).
RULE_VERSION = 1
# How long a paid miss is trusted before the place may be drafted again.
MISS_RETRY = {
    "no_name": timedelta(days=30),            # the writer could not identify one real place
    "identity_rejected": timedelta(days=14),  # the checker rejected the identity
    "error": timedelta(days=1),               # exception / timeout — retry soon
}

_CARD_SYSTEM = """You write a short reference card about one real place that appears in a travel \
itinerary. The card is used by writers who must not get basic facts wrong.

Return ONLY JSON:
{"display_name": "...", "locality": "...", "region": "...", "country": "...",
 "naming_note": "...", "aliases": ["..."], "facts": [{"text": "...", "kind": "..."}]}

- display_name: the place's usual English name (fix spelling variants, e.g. "Pawangka" -> the \
common spelling, mentioning the variant in naming_note).
- locality: the town or city it is in or next to; region: province / state / historical region. \
Short names only. Empty string when it does not apply or you are not sure.
- country: the one country it is in today (a route may cross a border, so this can differ from \
the country the tour is sold as).
- naming_note: one sentence on how to name the location for an international travel audience \
(e.g. a widely used region name), and any spelling variants. Empty string if nothing to say.
- aliases: up to 4 other names or spellings people use for this same place (may be empty).
- facts: 4-6 short, standalone, well-established facts about the PLACE ITSELF (what it is, its \
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
itinerary means. A tour may cross a border: do not reject only because the card's country is a \
neighbouring country of the one the tour is sold as.
- One verdict per numbered fact, same order. "supported" only when you are confident the fact \
is correct and widely documented; "unsure" when you cannot confirm it; "wrong" when it is false."""

# A changed prompt invalidates every stored miss (a new prompt may identify the place).
PROMPT_SHA = hashlib.sha256(f"{_CARD_SYSTEM}\n{_CHECK_SYSTEM}\nfacts={MAX_FACTS}".encode()).hexdigest()[:16]


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


# ── Names: fold, place_norm, core key ─────────────────────────────────────────────────────────

# Letters NFKD does not decompose (Vietnamese đ, Nordic ø, …).
_FOLD_MAP = str.maketrans({"đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss",
                           "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i", "’": "'"})
_LEADING_FILLER = frozenset({"the", "a", "an", "your", "our", "local", "standard"})
# Kind words that do not change WHICH place is meant ("Lake Khövsgöl" = "Khövsgöl Lake" =
# "Khövsgöl Nuur"; "Paro Town" = "Paro"). Deliberately NOT dzong/monastery/museum/river/valley:
# "Punakha" (town) and "Punakha Dzong" are two places.
_NEUTRAL_KIND_RE = re.compile(r"\b(sand dunes|lake|nuur|town|city|els|volcano|uul)\b")
_PAREN_RE = re.compile(r"\(([^)]*)\)")


def fold(s: Optional[str]) -> str:
    """Accent-folded, lowercased, punctuation-free text: 'Điện Biên Phủ' == 'dien bien phu',
    'Khövsgöl' == 'khovsgol' (the S233 `_norm` kept ASCII only: 'kh vsg l')."""
    text = unicodedata.normalize("NFKD", (s or "").translate(_FOLD_MAP))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def place_norm(place: Optional[str]) -> str:
    """The grouping key of an itinerary place: fold() without leading articles/fillers
    ('the Standard Ger Camp' == 'ger camp')."""
    words = fold(place).split()
    while len(words) > 1 and words[0] in _LEADING_FILLER:
        words = words[1:]
    return " ".join(words)


def core_key(name: Optional[str]) -> str:
    """place_norm() of the name outside any parenthetical, minus the neutral kind words; the
    place_norm() itself when nothing else is left ('Lake' stays 'lake')."""
    base = place_norm(_PAREN_RE.sub(" ", name or ""))
    core = " ".join(_NEUTRAL_KIND_RE.sub(" ", base).split())
    return core or base


def name_forms(name: Optional[str]) -> set[str]:
    """Every place_norm() a name stands for: the whole name, the name without its parenthetical,
    and the parenthetical itself ('Bayanzag (Flaming Cliffs)' → also 'bayanzag', 'flaming cliffs')."""
    out = {place_norm(name), place_norm(_PAREN_RE.sub(" ", name or ""))}
    out |= {place_norm(p) for p in _PAREN_RE.findall(name or "")}
    return {f for f in out if f}


def place_key(display_name: str, country: str) -> str:
    """S234: core name | country. Locality is no longer part of the key (LLM free text)."""
    return f"{core_key(display_name)}|{fold(country)}"


# ── Free filter (C9: one rule with ranking) ───────────────────────────────────────────────────

# Logistics places: where the tour sleeps, eats or changes transport. "Base camp" is an attraction
# (Everest Base Camp), so it is kept.
_LOGISTICS_PLACE_RE = re.compile(
    r"\b(hotels?|resorts?|guest ?houses?|lodges?|accommodations?|airports?|stations?|terminals?|"
    r"piers?|jetty|jetties|(?<!base )camps?|homestays?|restaurants?|cafes?|"
    r"border (gate|crossing|post)s?|checkpoints?|information cent(er|re)s?)$")
_POSSESSIVE_RE = re.compile(r"^(your|our)\b")


def card_skip_reason(place: Optional[str]) -> Optional[str]:
    """Why a place gets no card without any LLM call, or None to card it.

    'unnamed' — the ranking rule (`names_somewhere`, ADR 0020: the same function
    `atom_ranking.classify_exclusion` uses for `unnamed_place`) or a 'your/our …' place;
    'logistics_place' — the folded place ends in a lodging/transport/eating noun."""
    if not (place or "").strip():
        return "empty"
    folded = fold(place)
    if not names_somewhere(place) or _POSSESSIVE_RE.match(folded):
        return "unnamed"
    if _LOGISTICS_PLACE_RE.search(folded):
        return "logistics_place"
    return None


# ── LLM calls ──────────────────────────────────────────────────────────────────────────────────

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


def _clean_aliases(raw) -> list[str]:
    out = [" ".join(a.split()) for a in raw if isinstance(a, str) and a.strip()] if isinstance(raw, list) else []
    return out[:MAX_ALIASES]


def _trim(text: Optional[str], limit: int = 600) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def draft_card(atom: dict) -> tuple[dict, float, object]:
    """One `a3_place_card` call. Returns (card dict, cost, response)."""
    user = (f"PLACE AS THE ITINERARY NAMES IT: {atom['place']}\n"
            f"COUNTRY THE TOUR IS SOLD AS: {atom.get('country') or 'unknown'} "
            f"(the route may cross into a neighbouring country)\n"
            f"TOUR: {atom.get('tour_name') or 'unknown'}\n"
            f"ITINERARY SENTENCE: {_trim(atom.get('evidence'))}")
    resp = LLMClient().generate(LLMRequest(system_prompt=_CARD_SYSTEM, user_prompt=user,
                                           stage="a3_place_card", max_tokens=DRAFT_MAX_TOKENS))
    return _parse_json(resp.content), resp.cost_usd, resp


def check_card(card: dict, atom: dict) -> tuple[bool, list[str], float, object]:
    """One `a3_place_card_check` call. Returns (identity_ok, verdict per fact, cost, response)."""
    facts = card.get("facts") or []
    numbered = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1))
    user = (f"ITINERARY NAME: {atom['place']} (tour sold as {atom.get('country') or 'unknown'}; "
            f"the place may be in a neighbouring country; tour: {atom.get('tour_name') or 'unknown'}; "
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


# ── Storage helpers ────────────────────────────────────────────────────────────────────────────

_LINK_SQL = """
    INSERT INTO acp_shared.atom_place_card (atom_id, card_id, miss_reason) VALUES ($1, $2::uuid, NULL)
    ON CONFLICT (atom_id) DO UPDATE SET card_id = EXCLUDED.card_id, miss_reason = NULL, resolved_at = now()
"""
# An atom-level miss never overwrites a link; it only records why the atom has no card (admin view).
_ATOM_MISS_SQL = """
    INSERT INTO acp_shared.atom_place_card (atom_id, card_id, miss_reason) VALUES ($1, NULL, $2)
    ON CONFLICT (atom_id) DO UPDATE SET miss_reason = EXCLUDED.miss_reason, resolved_at = now()
    WHERE acp_shared.atom_place_card.card_id IS NULL
      AND acp_shared.atom_place_card.miss_reason IS DISTINCT FROM EXCLUDED.miss_reason
"""
_ALIAS_SQL = """
    INSERT INTO acp_shared.place_card_alias (alias_norm, country_norm, card_id, source)
    VALUES ($1, $2, $3::uuid, $4) ON CONFLICT (alias_norm, country_norm) DO NOTHING
"""
_MISS_SQL = """
    INSERT INTO acp_shared.place_card_miss
        (place_norm, country_norm, reason, attempts, rule_version, prompt_sha, model, last_tried_at, retry_after)
    VALUES ($1, $2, $3, 1, $4, $5, $6, now(), $7)
    ON CONFLICT (place_norm, country_norm) DO UPDATE SET
        reason = EXCLUDED.reason, attempts = acp_shared.place_card_miss.attempts + 1,
        rule_version = EXCLUDED.rule_version, prompt_sha = EXCLUDED.prompt_sha, model = EXCLUDED.model,
        last_tried_at = now(), retry_after = EXCLUDED.retry_after
"""


def miss_is_active(row, now: Optional[datetime] = None) -> bool:
    """A stored miss blocks a new draft only while it is fresh and was written by the current rule
    and prompt."""
    if not row:
        return False
    now = now or datetime.now(timezone.utc)
    return (row["rule_version"] == RULE_VERSION and row["prompt_sha"] == PROMPT_SHA
            and row["retry_after"] is not None and row["retry_after"] > now)


async def _record_miss(pool, group_key: tuple[str, str], atoms: list[dict], reason: str,
                       model: Optional[str]) -> None:
    retry_after = datetime.now(timezone.utc) + MISS_RETRY[reason]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(_MISS_SQL, group_key[0], group_key[1], reason, RULE_VERSION, PROMPT_SHA,
                           model, retry_after)
        await conn.executemany(_ATOM_MISS_SQL, [(a["atom_id"], reason) for a in atoms])


def _alias_rows(card_id: str, atoms: list[dict], group_country: str, card: dict, card_country: str) -> list:
    """(alias_norm, country_norm, card_id, source) rows for a new card: the itinerary spelling under
    the tour's country, and the display name / parenthetical / draft aliases under both the card's
    and the tour's country (a Vietnamese place on a 'Laos' tour is looked up with 'laos')."""
    rows = {}
    for a in atoms:
        for form in name_forms(a["place"]):
            rows.setdefault((form, group_country), "itinerary")
    countries = {c for c in (fold(card_country), group_country) if c}
    for source, names in (("display", [card.get("display_name")]), ("draft_alias", card.get("aliases") or [])):
        for name in names:
            for form in name_forms(name):
                for c in countries:
                    rows.setdefault((form, c), source)
    return [(form, c, card_id, source) for (form, c), source in rows.items()]


async def find_existing_cards(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """(place_norm, country_norm) → card_id, with no LLM call: an alias row first, then a card whose
    core name matches (new-format `place_key`, or any legacy 3-part key — S233 cards — compared
    in Python until the key backfill runs). Rejected cards are never matched."""
    if not keys:
        return {}
    found: dict[tuple[str, str], str] = {}
    rows = await conn.fetch(
        """
        SELECT a.alias_norm, a.country_norm, a.card_id
        FROM acp_shared.place_card_alias a
        JOIN acp_shared.place_card pc ON pc.card_id = a.card_id
        WHERE a.alias_norm = ANY($1::text[]) AND a.country_norm = ANY($2::text[]) AND pc.status <> 'rejected'
        """, [k[0] for k in keys], list({k[1] for k in keys}))
    by_alias = {(r["alias_norm"], r["country_norm"]): str(r["card_id"]) for r in rows}
    wanted = {}
    for k in keys:
        if k in by_alias:
            found[k] = by_alias[k]
        else:
            wanted[k] = f"{core_key(k[0])}|{k[1]}"
    if not wanted:
        return found
    cards = await conn.fetch(
        """
        SELECT card_id, display_name, country FROM acp_shared.place_card
        WHERE status <> 'rejected' AND (place_key = ANY($1::text[]) OR place_key LIKE '%|%|%')
        """, list(set(wanted.values())))
    by_core: dict[str, str] = {}
    for c in cards:
        for form in name_forms(c["display_name"]):
            by_core.setdefault(f"{core_key(form)}|{fold(c['country'])}", str(c["card_id"]))
    for k, ck in wanted.items():
        if ck in by_core:
            found[k] = by_core[ck]
    return found


# ── Build ──────────────────────────────────────────────────────────────────────────────────────

def _representative(atoms: list[dict]) -> dict:
    """The atom whose itinerary sentence says most, with up to 2 distinct sentences of the group."""
    rep = dict(max(atoms, key=lambda a: len(_trim(a.get("evidence")))))
    sentences = list(dict.fromkeys(_trim(a.get("evidence"), 300) for a in atoms if (a.get("evidence") or "").strip()))
    if len(sentences) > 1:
        rep["evidence"] = " / ".join(sentences[:2])
    return rep


async def build_card_for_group(group_key: tuple[str, str], atoms: list[dict], pool) -> tuple[Optional[str], str]:
    """Draft + check ONE card for every atom of one (place_norm, country) group and link them.
    Returns (card_id, 'linked') or (None, miss reason); a miss is stored so the place is not
    drafted again before its retry_after."""
    rep = _representative(atoms)
    signal = {"atom_id": rep["atom_id"], "place": rep["place"], "group_atoms": len(atoms)}
    card, cost, resp = await asyncio.to_thread(draft_card, rep)
    await _log(pool, "a3_place_card", "writer", resp, signal)
    model = getattr(resp, "model_used", None)
    name = (card.get("display_name") or "").strip() if isinstance(card.get("display_name"), str) else ""
    raw_country = card.get("country") if isinstance(card.get("country"), str) else ""
    country = (raw_country or rep.get("country") or "").strip()
    card["facts"] = _clean_facts(card.get("facts"))
    card["aliases"] = _clean_aliases(card.get("aliases"))
    if not name or not country:
        await _record_miss(pool, group_key, atoms, "no_name", model)
        return None, "no_name"
    ok, verdicts, cost2, resp2 = await asyncio.to_thread(check_card, card, rep)
    kept = [f for f, v in zip(card["facts"], verdicts) if v == "supported"]
    await _log(pool, "a3_place_card_check", "judge", resp2,
               dict(signal, identity_ok=ok, facts=len(card["facts"]), kept=len(kept)))
    if not ok:
        logger.info("place_card_identity_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name)
        await _record_miss(pool, group_key, atoms, "identity_rejected", model)
        return None, "identity_rejected"
    locality = (card.get("locality") or "").strip() or None
    region = (card.get("region") or "").strip() or None
    async with pool.acquire() as conn, conn.transaction():
        # On a key conflict the existing card wins, but the money spent is still added to it.
        card_id = await conn.fetchval(
            """
            INSERT INTO acp_shared.place_card
                (place_key, display_name, locality, region, country, naming_note, facts, cost_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            ON CONFLICT (place_key) DO UPDATE
                SET cost_usd = acp_shared.place_card.cost_usd + EXCLUDED.cost_usd, updated_at = now()
            RETURNING card_id
            """,
            place_key(name, country), name, locality, region, country,
            (card.get("naming_note") or "").strip() or None, json.dumps(kept), round(cost + cost2, 6),
        )
        card_id = str(card_id)
        await conn.executemany(_LINK_SQL, [(a["atom_id"], card_id) for a in atoms])
        await conn.executemany(_ALIAS_SQL, _alias_rows(card_id, atoms, group_key[1], card, country))
        await conn.execute("DELETE FROM acp_shared.place_card_miss WHERE place_norm = $1 AND country_norm = $2",
                           group_key[0], group_key[1])
    return card_id, "linked"


async def build_card_for_atom(atom: dict, pool) -> Optional[str]:
    """One atom as a one-atom group (kept for callers/tests of the S233 API)."""
    key = (place_norm(atom["place"]), fold(atom.get("country")))
    card_id, _ = await build_card_for_group(key, [atom], pool)
    return card_id


def _empty_counters(atoms: int) -> dict:
    return {"atoms": atoms, "already_linked": 0, "skipped_by_rule": 0, "places": 0, "linked_existing": 0,
            "skipped_cached": 0, "over_cap": 0, "drafted": 0, "linked_new": 0, "missed": 0, "errors": 0,
            "built": 0}


async def ensure_place_cards(atom_ids: list[str], pool, *, force: bool = False,
                             max_places: Optional[int] = None) -> dict:
    """Give every atom in `atom_ids` its place card where one can be had, paying for at most one
    draft per (place_norm, country) group (S234 guard). Order of work: free rule skip → existing
    card by alias / core name (no call) → stored miss (no call unless `force`) → draft. Soft-fails
    per group (a card is an aid, never a reason to fail a write).

    Returns counters: atoms, already_linked, skipped_by_rule, places (groups to resolve),
    linked_existing, skipped_cached, over_cap (groups left for a later run by `max_places`),
    drafted, linked_new, missed, errors, built (atoms newly linked by a draft)."""
    ids = [a for a in dict.fromkeys(atom_ids) if a]
    counters = _empty_counters(len(ids))
    if not ids:
        return counters
    async with pool.acquire() as conn:
        linked = {r["atom_id"] for r in await conn.fetch(
            "SELECT atom_id FROM acp_shared.atom_place_card WHERE atom_id = ANY($1::text[]) AND card_id IS NOT NULL",
            ids)}
        counters["already_linked"] = len(linked)
        rows = [dict(r) for r in await conn.fetch(_ATOM_PLACE_QUERY, [a for a in ids if a not in linked])]
    order = {a: i for i, a in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["atom_id"], len(order)))

    groups: dict[tuple[str, str], list[dict]] = {}
    skipped: list[tuple[str, str]] = []
    for atom in rows:
        reason = card_skip_reason(atom["place"])
        if reason:
            skipped.append((atom["atom_id"], reason))
            continue
        groups.setdefault((place_norm(atom["place"]), fold(atom.get("country"))), []).append(atom)
    counters["skipped_by_rule"] = len(skipped)
    counters["places"] = len(groups)

    async with pool.acquire() as conn:
        if skipped:
            await conn.executemany(_ATOM_MISS_SQL, skipped)
        existing = await find_existing_cards(conn, list(groups))
        for key, card_id in existing.items():
            await conn.executemany(_LINK_SQL, [(a["atom_id"], card_id) for a in groups[key]])
            await conn.executemany(_ALIAS_SQL, [(f, key[1], card_id, "itinerary")
                                                for a in groups[key] for f in name_forms(a["place"])])
            counters["linked_existing"] += len(groups[key])
        todo = [k for k in groups if k not in existing]
        misses = {}
        if todo and not force:
            misses = {(r["place_norm"], r["country_norm"]): r for r in await conn.fetch(
                """
                SELECT place_norm, country_norm, reason, rule_version, prompt_sha, retry_after
                FROM acp_shared.place_card_miss
                WHERE place_norm = ANY($1::text[]) AND country_norm = ANY($2::text[])
                """, [k[0] for k in todo], list({k[1] for k in todo}))}
    now = datetime.now(timezone.utc)
    to_draft = [k for k in todo if not miss_is_active(misses.get(k), now)]
    counters["skipped_cached"] = len(todo) - len(to_draft)
    if max_places is not None and len(to_draft) > max_places:
        counters["over_cap"] = len(to_draft) - max_places
        to_draft = to_draft[:max_places]
    if not to_draft:
        return counters

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(key: tuple[str, str]) -> None:
        async with sem:
            counters["drafted"] += 1
            try:
                card_id, outcome = await build_card_for_group(key, groups[key], pool)
            except Exception as e:  # noqa: BLE001 — soft-fail per place
                counters["errors"] += 1
                logger.warning("place_card_build_failed", place=key[0], country=key[1], error=str(e)[:200])
                try:
                    await _record_miss(pool, key, groups[key], "error", None)
                except Exception as e2:  # noqa: BLE001
                    logger.warning("place_card_miss_write_failed", place=key[0], error=str(e2)[:200])
                return
            if card_id:
                counters["linked_new"] += 1
                counters["built"] += len(groups[key])
            else:
                counters["missed"] += 1

    await asyncio.gather(*(one(k) for k in to_draft))
    return counters


# ── Duplicates + merge (admin) ─────────────────────────────────────────────────────────────────

def _contains_words(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a != b and f" {a} " in f" {b} "


def duplicate_clusters(cards: list[dict], aliases: list[dict] = ()) -> list[dict]:
    """Candidate duplicate groups for the admin to review — never merged automatically. Two cards
    of the same (folded) country pair up when they share a core name ('Lake Khövsgöl' / 'Khövsgöl
    Lake'), one name contains the other as whole words ('Paro' / 'Paro Town' — but also 'Punakha' /
    'Punakha Dzong', which is why a human decides), or an alias of one is a name of the other.
    `cards`: card_id, display_name, country (+ any display fields, passed through)."""
    parent = {c["card_id"]: c["card_id"] for c in cards}
    reasons: dict[str, set] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, why):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            reasons.setdefault(ra, set()).update(reasons.pop(rb, set()))
        reasons.setdefault(ra, set()).add(why)

    info = {c["card_id"]: (fold(c.get("country")), name_forms(c["display_name"]),
                           {core_key(f) for f in name_forms(c["display_name"])}) for c in cards}
    alias_of: dict[str, set] = {}
    for a in aliases or []:
        alias_of.setdefault(str(a["card_id"]), set()).add((a["alias_norm"], a["country_norm"]))
    ids = list(parent)
    for i, a in enumerate(ids):
        ca, fa, ka = info[a]
        for b in ids[i + 1:]:
            cb, fb, kb = info[b]
            if ca != cb:
                continue
            if ka & kb:
                union(a, b, "same_core_name")
            elif any(_contains_words(x, y) or _contains_words(y, x) for x in fa for y in fb):
                union(a, b, "name_contains")
            elif any((f, cb) in alias_of.get(a, ()) for f in fb) or any((f, ca) in alias_of.get(b, ()) for f in fa):
                union(a, b, "alias")
    groups: dict[str, list] = {}
    for c in cards:
        groups.setdefault(find(c["card_id"]), []).append(c)
    return [{"country": g[0].get("country"), "reasons": sorted(reasons.get(root, ())), "cards": g}
            for root, g in groups.items() if len(g) > 1]


def _facts_list(raw) -> list[dict]:
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return [f for f in raw or [] if isinstance(f, dict) and f.get("text")]


def merged_facts(keep_facts, other_facts: list) -> list[dict]:
    """The kept card's facts, then the merged cards' facts not already there (folded text), added
    only while the card holds fewer than MAX_FACTS — a merge never drops a fact the card had."""
    out = _facts_list(keep_facts)
    seen = {fold(f["text"]) for f in out}
    for facts in other_facts:
        for f in _facts_list(facts):
            if len(out) >= MAX_FACTS:
                return out
            if fold(f["text"]) not in seen:
                seen.add(fold(f["text"]))
                out.append(f)
    return out


class MergeError(ValueError):
    pass


async def merge_cards(conn, keep_id: str, from_ids: list[str]) -> dict:
    """Merge `from_ids` into `keep_id` (call inside a transaction): re-link their atoms, point
    their aliases at the kept card and add their names as aliases, add their facts (up to
    MAX_FACTS) and cost, then delete them. Cards must share a country. Raises MergeError /
    LookupError."""
    from_ids = [f for f in dict.fromkeys(from_ids) if f and f != keep_id]
    if not from_ids:
        raise MergeError("from_ids must name at least one card other than keep_id")
    rows = {str(r["card_id"]): r for r in await conn.fetch(
        """
        SELECT card_id, display_name, country, facts, cost_usd FROM acp_shared.place_card
        WHERE card_id = ANY($1::uuid[]) FOR UPDATE
        """, [keep_id, *from_ids])}
    missing = [c for c in [keep_id, *from_ids] if c not in rows]
    if missing:
        raise LookupError(f"place card(s) not found: {', '.join(missing)}")
    keep = rows[keep_id]
    country = fold(keep["country"])
    if any(fold(rows[f]["country"]) != country for f in from_ids):
        raise MergeError("cards from different countries cannot be merged")
    moved = await conn.fetchval(
        """
        WITH m AS (
            UPDATE acp_shared.atom_place_card SET card_id = $1::uuid, miss_reason = NULL, resolved_at = now()
            WHERE card_id = ANY($2::uuid[]) RETURNING 1
        ) SELECT count(*) FROM m
        """, keep_id, from_ids)
    await conn.execute("UPDATE acp_shared.place_card_alias SET card_id = $1::uuid WHERE card_id = ANY($2::uuid[])",
                       keep_id, from_ids)
    alias_rows = [(form, country, keep_id, "merge")
                  for f in from_ids for form in name_forms(rows[f]["display_name"])]
    await conn.executemany(_ALIAS_SQL, alias_rows)
    facts = merged_facts(keep["facts"], [rows[f]["facts"] for f in from_ids])
    cost = sum(float(rows[c]["cost_usd"] or 0) for c in [keep_id, *from_ids])
    await conn.execute(
        """
        UPDATE acp_shared.place_card SET facts = $2::jsonb, cost_usd = $3, updated_at = now()
        WHERE card_id = $1::uuid
        """, keep_id, json.dumps(facts), round(cost, 6))
    await conn.execute("DELETE FROM acp_shared.place_card WHERE card_id = ANY($1::uuid[])", from_ids)
    return {"keep_id": keep_id, "merged_ids": from_ids,
            "merged_names": [rows[f]["display_name"] for f in from_ids],
            "atoms_moved": int(moved or 0), "aliases_added": len(alias_rows), "facts": len(facts),
            "cost_usd": round(cost, 6)}


def render_card(card) -> tuple[str, list[str]]:
    """(location line, background fact lines) for the seed."""
    where = ", ".join(p for p in (card["display_name"], card["locality"], card["region"], card["country"]) if p)
    facts = card["facts"]
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return where, [f["text"] for f in facts or [] if isinstance(f, dict) and f.get("text")]


__all__ = ["ensure_place_cards", "build_card_for_atom", "build_card_for_group", "render_card", "place_key",
           "CARD_FOR_ATOM_QUERY", "draft_card", "check_card", "fold", "place_norm", "core_key", "name_forms",
           "card_skip_reason", "find_existing_cards", "miss_is_active", "RULE_VERSION", "PROMPT_SHA",
           "duplicate_clusters", "merged_facts", "merge_cards", "MergeError"]

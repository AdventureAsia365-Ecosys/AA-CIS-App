"""AA-768 — the source text T8/T9 write from for one atom.

T8/T9 used `tour_atoms.text` alone, which is the atom's short label ("Pawangka Monastery —
visit", 26 characters). A 200-word post written from that is almost all invention: in the S231
pilot the LinkedIn pieces placed a Tibetan monastery "above Luang Prabang" and the F9 fact check
held every one. The atom already carries its source sentence (`evidence`, quoted from the tour
itinerary), an `emotional_hook` and a `season_note`, and its Segment groups the same place+action
across other tours — real, citable source the writer never saw.

`atom_seed_text()` = the label + this atom's evidence/hook/season + up to `MAX_SIBLINGS` distinct
evidence sentences from other live atoms of the same Segment (platform or this tenant's own).
The same text feeds the writer prompt and the T10 gates, so what the writer may cite is exactly
what the gates check against.

S234 (AA-768) — the sibling lines were the source of borrowed places and facts (S233 r1: 'Erdene
Zuu' / 'thousands of monks' in 5/8 Mongolia blogs). Siblings are now added only when the place
card has fewer than `RICH_CARD_FACTS` facts, at most `MAX_SIBLINGS` (2), and never a line naming a
place the moment's own lines do not name. Route moments get no siblings at all
(`route_moment_seeds`), are merged by place card or core place name (one block per place, C10), and a moment with
fewer than `THIN_FACTS` facts is tagged thin so it is not made the hook.

S234 round 2 (AA-772) — a route seed keeps one value per fact across its cards
(`seed_canon.canonical_facts`: 'two percent' vs '0.4 percent' of the world's fresh water), and an
unverified overnight moment whose card region no other moment of that day shares, and that the next
day travels on to a lodging in, loses its card (`seed_canon.overnight_card_off_route`: the Day 3
'Sod Camp' card sat on the Khövsgöl lakeshore that the route only reaches on Day 4).

AA-774 (S235) — only `WRITER_FACT_KINDS` card facts (what the place is, its history, its setting,
its naming) are rendered as "Place background". 'visiting' / 'practical' facts (permits, "access
requires a multi-hour horse ride", dress codes, hike durations) read as trip promises: S234 r4 put
the permit sentence in 7/8 blogs. They stay on the card (admin view, FACT_KINDS) and are filtered at
read time only. The sibling gate (`RICH_CARD_FACTS`) still counts EVERY checked fact on the card, so a
card that loses its practical facts does not bring back the S233 'Other trips' lines.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Optional

import structlog

from services.acp_content_writing.hygiene import fold, phrase_tokens, proper_nouns
from services.acp_shared.place_cards import core_key, is_generic_name, place_key
from services.acp_shared.place_cards import fold as place_fold
from services.acp_shared.seed_canon import canonical_facts, overnight_card_off_route

logger = structlog.get_logger()

MAX_SIBLINGS = 2
MAX_EVIDENCE_CHARS = 500
RICH_CARD_FACTS = 3  # a card with this many checked facts needs no "other trips" lines
THIN_FACTS = 3  # a moment with fewer facts (card facts + source sentences) is thin
# AA-774 — the card fact kinds a writer may see. 'visiting' / 'practical' are trip facts: they come
# only from the itinerary. A fact without a kind counts as what_it_is (place_cards._clean_facts).
WRITER_FACT_KINDS = frozenset({"what_it_is", "history", "setting", "naming"})

# AA-768 round 5 — + the atom's place and its source tour's country.
# S233 — reads acp_contract.v_active_tour_atoms (a retracted/trashed Master tour never feeds a
# tenant post) and carries itinerary_day ("Day N of the trip").
_ATOM_QUERY = """
    SELECT ta.text, ta.evidence, ta.emotional_hook, ta.season_note, ta.place, ta.itinerary_day,
           rt.country
    FROM acp_contract.v_active_tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.atom_id = $1 AND ta.owner_scope IN ('platform', $2) AND NOT ta.deleted AND NOT ta.is_empty_marker
"""

# Other live atoms of the same Segment, same visibility rule as the atom itself. S233: only from
# tours in the same country (pre-S218 Segments mixed countries — Ladakh/Mongolia evidence reached
# a Tibet seed), never alias members, never another day of the same tour, active tours only;
# shortest first so a whole-day-body fallback evidence does not crowd out real sentences.
_SIBLING_EVIDENCE_QUERY = """
    SELECT DISTINCT ON (ta.evidence) ta.evidence, length(ta.evidence) AS n
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms me ON me.atom_id = m.atom_id
    JOIN silver_aa_internal.raw_tours mrt ON mrt.tour_id = me.tour_id
    JOIN acp_contract.atom_segment_member sib
         ON sib.segment_id = m.segment_id AND sib.atom_id <> m.atom_id AND NOT sib.is_alias
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = sib.atom_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE m.atom_id = $1 AND ta.owner_scope IN ('platform', $2)
      AND NOT ta.deleted AND NOT ta.is_empty_marker AND ta.tour_id <> me.tour_id
      AND rt.country IS NOT DISTINCT FROM mrt.country
      AND ta.evidence IS NOT NULL AND btrim(ta.evidence) <> ''
    LIMIT 40
"""

_CARD_QUERY = """
    SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts,
           pc.status
    FROM acp_shared.atom_place_card apc
    JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id
    WHERE apc.atom_id = $1 AND pc.status <> 'rejected'
"""

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def trim_evidence(evidence: Optional[str], place: Optional[str]) -> Optional[str]:
    """A3 stores the WHOLE day body as `evidence` when the model's quote fails the containment
    check (atom_extraction.checkable_evidence). Keep the sentences that name the place (plus the
    next one), else the first sentences, up to MAX_EVIDENCE_CHARS."""
    if not evidence or len(evidence) <= MAX_EVIDENCE_CHARS:
        return evidence
    sents = [x.strip() for x in _SENT_RE.split(" ".join(evidence.split())) if x.strip()]
    words = [w for w in re.findall(r"[a-z]{4,}", (place or "").lower())]
    picked: list[int] = []
    for i, sent in enumerate(sents):
        if words and any(w in sent.lower() for w in words):
            picked += [i, i + 1]
    order = sorted({i for i in picked if i < len(sents)}) or list(range(len(sents)))
    out = ""
    for i in order:
        if len(out) + len(sents[i]) + 1 > MAX_EVIDENCE_CHARS:
            break
        out = f"{out} {sents[i]}".strip()
    return out or sents[0][:MAX_EVIDENCE_CHARS]


def _card_fact_dicts(card: Optional[dict]) -> list[dict]:
    facts = (card or {}).get("facts") or []
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return [f for f in facts if isinstance(f, dict) and f.get("text")]


def _card_facts(card: Optional[dict]) -> list[str]:
    """Every checked fact on the card, any kind — the count the sibling gate uses."""
    return [f["text"] for f in _card_fact_dicts(card)]


def writer_facts(card: Optional[dict]) -> list[str]:
    """AA-774 — the card facts rendered for the writer: `WRITER_FACT_KINDS` only (a missing kind
    counts as what_it_is)."""
    return [f["text"] for f in _card_fact_dicts(card) if (f.get("kind") or "what_it_is") in WRITER_FACT_KINDS]


def _location_lines(place: Optional[str], country: Optional[str], card: Optional[dict]) -> list[str]:
    lines: list[str] = []
    if card and card.get("display_name"):
        where = ", ".join(p for p in (card["display_name"], card.get("locality"), card.get("region"),
                                       card.get("country")) if p)
        if place and place.strip() and place.strip().lower() != card["display_name"].lower():
            where += f" (the itinerary calls it {place.strip()})"
        lines.append(f"Location: {where} (do not place it in any other town, region or country)")
        if card.get("naming_note"):
            lines.append(f"Naming: {card['naming_note']}")
    elif country and country.strip():
        where = f"{place.strip()}, {country.strip()}" if place and place.strip() else country.strip()
        lines.append(f"Location: {where} (do not name any other country or region for it)")
    return lines


def _background_block(fact_lines: list[str]) -> str:
    return ("Place background (checked facts about the place itself — you may use these; "
            "they say nothing about what this trip does):\n" + "\n".join(f"- {f}" for f in fact_lines))


def foreign_places(line: str, own_text: str) -> set[str]:
    """Proper nouns in `line` with a word the moment's own text never uses ('Erdene Zuu' in a
    sibling line of an unnamed-ruins moment)."""
    own = set()
    for p in proper_nouns(own_text):
        own |= set(phrase_tokens(p))
    own |= set(re.findall(r"[a-z0-9]+", fold(own_text)))
    return {p for p in proper_nouns(line) if any(t not in own for t in phrase_tokens(p))}


def pick_siblings(sibling_evidence: list[str], own_text: str, *, own_evidence: Optional[str],
                  card_fact_count: int) -> list[str]:
    """S234 sibling rule: none when the card already has `RICH_CARD_FACTS` facts; otherwise up to
    `MAX_SIBLINGS` distinct lines that name no place the moment's own text does not name."""
    if card_fact_count >= RICH_CARD_FACTS:
        return []
    seen = {(own_evidence or "").strip().lower()}
    others: list[str] = []
    for ev in sibling_evidence:
        key = (ev or "").strip().lower()
        if not key or key in seen or foreign_places(ev, own_text):
            continue
        seen.add(key)
        others.append(ev.strip())
        if len(others) >= MAX_SIBLINGS:
            break
    return others


def compose_seed(text: str, evidence: Optional[str], hook: Optional[str], season: Optional[str],
                 sibling_evidence: list[str], *, place: Optional[str] = None,
                 country: Optional[str] = None, day: Optional[int] = None,
                 card: Optional[dict] = None) -> str:
    """Pure: the seed block from an atom's fields. Empty fields are left out; sibling evidence
    follows `pick_siblings` (S234). `card` (AA-772) is the atom's place card: its location
    replaces the bare place + country line, and its checked facts of `WRITER_FACT_KINDS` (AA-774)
    become the PLACE BACKGROUND the writer may cite. The sibling gate counts every checked fact.

    The line prefixes ('Location:', 'Naming:', 'Day N', 'Source itinerary:', 'Why it matters:',
    'Season:', 'Place background', 'Other trips that include') are a parsing contract for F9 v2
    (`claim_rules.number_source_lines`, S234 PR5) — a rename needs the round-trip test updated."""
    lines = [text.strip()] + _location_lines(place, country, card)
    if day:
        lines.append(f"Day {day} of the trip")
    if evidence and evidence.strip():
        lines.append(f"Source itinerary: {evidence.strip()}")
    if hook and hook.strip():
        lines.append(f"Why it matters: {hook.strip()}")
    if season and season.strip():
        lines.append(f"Season: {season.strip()}")
    fact_lines = writer_facts(card)
    if fact_lines:
        lines.append(_background_block(fact_lines))
    # AA-774: the UNFILTERED count — a card rich in checked facts needs no 'Other trips' lines even
    # when some of them are visiting/practical and not shown (S233 borrowed-places regression).
    others = pick_siblings(sibling_evidence, "\n".join(lines), own_evidence=evidence,
                           card_fact_count=len(_card_facts(card)))
    if others:
        lines.append("Other trips that include this same moment (for context only — never promise "
                     "these details for this trip):\n" + "\n".join(f"- {o}" for o in others))
    return "\n".join(lines)


_SOURCE_RE = re.compile(r"Source itinerary: (.*)$", re.M)
_SENTS_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def moment_fact_count(seed_block: str) -> int:
    """Facts a moment block gives the writer: its "Place background" bullets plus the sentences
    of its "Source itinerary" text ("Why it matters" is framing, not a fact). S233: the Mongolia
    ruins moment had 1; every other moment 8–11."""
    n = sum(len([x for x in _SENTS_RE.split(m.group(1).strip()) if x.strip()])
            for m in _SOURCE_RE.finditer(seed_block or ""))
    in_bg = False
    for line in (seed_block or "").splitlines():
        if line.startswith("Place background"):
            in_bg = True
            continue
        if in_bg and line.startswith("- "):
            n += 1
        else:
            in_bg = False
    return n


def thin_note(fact_count: int) -> str:
    return (f"Thin moment: only {fact_count} source fact{'s' if fact_count != 1 else ''} — mention it in "
            "one sentence at most; never make it the hook, the title or the climax.")


def merge_keys(parts: dict) -> list[str]:
    """C10 — the keys a route moment merges on: its place card id, and its card's identity key
    from AA-772 (`place_cards.place_key`: core name + country, with neutral kind words cut at the
    edges, so 'Lake Khövsgöl' == 'Khövsgöl Lake' == 'Khövsgöl Nuur'). Two moments merge when ANY
    key matches, so the 5 S233 Khövsgöl cards become one block before the card merge/backfill
    lands, while 'Sod Camp, Khövsgöl Lake area' stays its own and 'Punakha' != 'Punakha Dzong'.
    A generic name ('City Palace') keys on its locality too and, with no locality, never merges
    by name. A moment with no card never merges."""
    card = parts.get("card") or {}
    keys: list[str] = []
    if card.get("card_id"):
        keys.append(f"card:{card['card_id']}")
    name = card.get("display_name") or ""
    if core_key(name) and not (is_generic_name(name) and not place_fold(card.get("locality"))):
        keys.append("name:" + place_key(name, card.get("country") or parts.get("country") or "",
                                        card.get("locality")))
    return keys


def group_route_moments(parts: list[dict]) -> list[list[dict]]:
    """Route moments (day order) grouped by `merge_keys` with union-find (card id OR core name):
    groups ordered by their first moment, members in the given order."""
    parent = list(range(len(parts)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, p in enumerate(parts):
        for key in merge_keys(p):
            if key in owner:
                a, b = root(i), root(owner[key])
                parent[max(a, b)] = min(a, b)
            else:
                owner[key] = i
    groups: dict[int, list[dict]] = {}
    for i, p in enumerate(parts):
        groups.setdefault(root(i), []).append(p)
    return [groups[k] for k in sorted(groups)]


def compose_route_moment(group: list[dict]) -> str:
    """One seed block for one or more route moments at the same place (day order): the first
    moment's label and location, one "Day N: <label> — Source itinerary: …" line per moment, the
    hooks and seasons de-duplicated, and the background of the richest card only (S233: the 5
    Khövsgöl cards disagreed — 2% / 0.4% / 70% of the fresh water; AA-774: richest in WRITER facts).
    No sibling lines. Tagged thin when it has fewer than `THIN_FACTS` facts."""
    first = group[0]
    if len(group) == 1:
        block = compose_seed(first["text"], first.get("evidence"), first.get("hook"), first.get("season"), [],
                             place=first.get("place"), country=first.get("country"), day=first.get("day"),
                             card=first.get("card"))
    else:
        lines = [first["text"].strip()] + _location_lines(first.get("place"), first.get("country"), first.get("card"))
        for part in group:
            day = f"Day {part['day']}" if part.get("day") else "Another day"
            ev = (part.get("evidence") or "").strip()
            lines.append(f"{day} of the trip: {part['text'].strip()}" + (f" — Source itinerary: {ev}" if ev else ""))
        for key, label in (("hook", "Why it matters"), ("season", "Season")):
            for value in dict.fromkeys((p.get(key) or "").strip() for p in group):
                if value:
                    lines.append(f"{label}: {value}")
        richest = max((p.get("card") for p in group), key=lambda c: len(writer_facts(c)))
        if writer_facts(richest):
            lines.append(_background_block(writer_facts(richest)))
        block = "\n".join(lines)
    n = moment_fact_count(block)
    return block + ("\n" + thin_note(n) if n < THIN_FACTS else "")


async def atom_seed_parts(conn, atom_id: str, tenant_id: str, *, siblings: bool = True) -> Optional[dict]:
    """The atom's seed fields (None when not live/visible for this tenant). The sibling query
    runs only when `siblings` is set and the place card is not already rich (every checked fact
    counts, AA-774)."""
    row = await conn.fetchrow(_ATOM_QUERY, atom_id, str(tenant_id))
    if row is None:
        return None
    card = await conn.fetchrow(_CARD_QUERY, atom_id)
    card = dict(card) if card else None
    sibling_lines: list[str] = []
    if siblings and len(_card_facts(card)) < RICH_CARD_FACTS:
        rows = await conn.fetch(_SIBLING_EVIDENCE_QUERY, atom_id, str(tenant_id))
        sibling_lines = [s for s in (trim_evidence(r["evidence"], row["place"])
                                     for r in sorted(rows, key=lambda r: (r["n"], r["evidence"]))) if s]
    return {"atom_id": atom_id, "text": row["text"], "evidence": trim_evidence(row["evidence"], row["place"]),
            "hook": row["emotional_hook"], "season": row["season_note"], "place": row["place"],
            "country": row["country"], "day": row["itinerary_day"], "card": card, "siblings": sibling_lines}


async def atom_seed_text(conn, atom_id: str, tenant_id: str) -> Optional[str]:
    """None when the atom is not live/visible for this tenant (callers raise their own error)."""
    p = await atom_seed_parts(conn, atom_id, tenant_id)
    if p is None:
        return None
    return compose_seed(p["text"], p["evidence"], p["hook"], p["season"], p["siblings"], place=p["place"],
                        country=p["country"], day=p["day"], card=p["card"])


def canonicalize_route_cards(parts: list[dict]) -> tuple[list[dict], dict]:
    """S234 round 2 — pure: the route's moment parts (day order) with (1) the card removed from an
    overnight moment the day's geography contradicts and (2) every remaining card's facts reduced to
    one value per fact across the seed (`seed_canon.canonical_facts`; a verified card wins, then the
    card with more moments in this route, then the earlier one). Returns (new parts, report)."""
    by_day: dict = {}
    for p in parts:
        if p.get("day") is not None:
            by_day.setdefault(p["day"], []).append(p)
    days = sorted(by_day)
    next_of = {d: by_day[days[i + 1]] if i + 1 < len(days) and days[i + 1] == d + 1 else []
               for i, d in enumerate(days)}
    out: list[dict] = []
    off_route: list[str] = []
    for p in parts:
        if (p.get("card") and p.get("day") is not None
                and overnight_card_off_route(p, by_day[p["day"]], next_of[p["day"]])):
            off_route.append(p["card"].get("display_name") or "")
            p = {**p, "card": None}
        out.append(p)
    cards = [p["card"] for p in out if p.get("card")]
    counts = Counter(c.get("card_id") for c in cards)
    kept, dropped = canonical_facts(cards, counts)
    # `facts` comes back from canonical_facts as a list even when asyncpg gave the card a jsonb str.
    out = [{**p, "card": {**p["card"], "facts": kept[p["card"]["card_id"]]}}
           if p.get("card") and p["card"].get("card_id") in kept else p for p in out]
    return out, {"overnight_cards_dropped": off_route, "facts_dropped": dropped}


async def route_moment_seeds(conn, atoms: list, tenant_id: str) -> list[tuple[str, str]]:
    """S234 — a route's moments as [(first atom_id, seed block)], in the given (day) order, with
    same-place moments merged into one block (C10) and no sibling lines. `atoms` is
    [(atom_id, label)]; an atom whose seed cannot be read keeps its own block with the label
    alone (the pre-S234 fallback)."""
    parts: list[dict] = []
    for atom_id, label in atoms:
        p = await atom_seed_parts(conn, atom_id, tenant_id, siblings=False)
        parts.append(p if p is not None else {"atom_id": atom_id, "text": label or "", "card": None,
                                              "label_only": True})
    parts, report = canonicalize_route_cards(parts)
    if report["overnight_cards_dropped"] or report["facts_dropped"]:
        logger.info("t9_route_seed_canon", overnight_cards_dropped=report["overnight_cards_dropped"],
                    facts_dropped=[d["fact"][:120] for d in report["facts_dropped"]])
    out = []
    for group in group_route_moments(parts):
        block = group[0]["text"] if group[0].get("label_only") else compose_route_moment(group)
        out.append((group[0]["atom_id"], block))
    return out

"""services/acp_contract/segment_matching.py — AA-509 Segment, redesigned platform-wide (AA-545).

Groups tour_atoms describing the same real-world moment across the WHOLE platform catalog (not
merging content — an atom told two different ways stays two atoms, sharing one Segment).
Foundation for T6 group-by-Segment curation, Route (T7 Blog), Atom Score, Slate.

Ported near-verbatim from Ms. Thư's aa-social-media (`src/aa_social/segments.py`) per the build
prompt and STEP0 (docs/claude_audit/AA-509-step0-schema-matching-investigation.md). The pure
grouping functions below (`derive_segments`/`reconcile_ids`/everything they call) are an
unmodified port of that algorithm's SHAPE — same Jaccard-on-place + verb-match-on-action logic,
same deterministic-derive-then-reconcile id strategy (ADR 0002, same repo:
docs/adr/0002-vector-store-scoped-to-search-matching.md — grouping must stay deterministic, no
embeddings, so a re-run never silently regroups Atoms out from under a Calendar/Slot built on the
old ids).

**AA-545 — `tenant_id` REMOVED from `_mint()`/`derive_segments()`, restoring the exact origin
formula (`sha256(place|verb)` alone).** AA-509 originally folded `tenant_id` into the hash to
avoid 2 tenants colliding on one PK row — confirmed (AA-543/ADR-0001) this was a pure PK-collision
workaround, not a design requirement: Segment never reads a tenant's brand voice or a
tenant-specific signal. Under a genuinely platform-wide model that same collision is the FEATURE
(2 tours/tenants describing "walk the Nakasendo trail" dedup into one shared Segment — restoring
the `recurrence` rank-sum axis, ADR 0014, which the redesign's own first draft nearly killed by
mistakenly keying on `tour_id` instead — see docs/implementation-notes/AA-545.md and the AA-545
Linear issue's own grill trail for the full reasoning).

**`run_segment_matching(tour_id, pool)` is now INCREMENTAL, not a full per-tenant recompute**
(AA-545 Q2 — `derive_segments()`'s own O(n²) warning made a full platform-wide recompute on every
trigger unsafe once the pool is the whole catalog, not one tenant's atoms). Only the triggering
tour's own atoms are freshly derived; every EXISTING platform Segment is represented by one cheap
"pseudo-atom" (reconstructed from its stored `canonical_place`/`canonical_action` — exactly the
raw text whose own derived `Key` equals that Segment's canonical Key, so this is lossless, not an
approximation) rather than reloading every atom that segment already has. The existing
`_connected()`/Jaccard logic decides, over this much smaller pool, whether the new atoms: mint a
brand new Segment (touch 0 existing), extend exactly one existing Segment (its real
`atom_segment_member` rows are left alone, only the new atoms get new member rows), or bridge 2+
existing Segments into one (handled by the EXISTING, unmodified `reconcile_ids()`/`_claims()`
tie-break — this module does not add a second, separate merge-arbitration rule).

`atom_segment` rows are UPSERT-only, never deleted, by design (see migration 129's own comment
for the FK reasoning: `atom_segment_alias.segment_id_old` references `atom_segment(segment_id)`,
so an id that "gave way" to another has to keep existing as a row for that FK to hold — matching
ADR 0002's own framing that the old id "still resolves", not that it disappears). Pre-AA-545 rows
keep their OLD-formula segment_id string values unchanged forever (an opaque TEXT PK, never
rehashed — see docs/implementation-notes/AA-545.md Decision 1); only Segments minted from this
point on use the new formula.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import NamedTuple

# ── reference table, ported from Ms. Thư's aa-social-media (reference/action-verbs.toml) ───
# Deliberately narrow (see that file's own comment) — only add a class when a real export shows
# two itineraries splitting one moment across two words. Not re-derived here; copied verbatim.
_ACTION_SYNONYM_CLASSES: dict[str, list[str]] = {
    "eat": ["eat", "have", "dine", "taste", "sample"],
    "walk": ["walk", "hike", "stroll", "cross", "ramble", "trek"],
    "view": ["view", "observe", "watch", "see"],
    "bathe": ["bathe", "soak"],
}
_REACHED_BY_OPENERS = frozenset({"descend", "ascend", "climb", "continue", "proceed"})
_REACHED_BY_CONNECTORS = ("to and", "and then", "and")

# Connectors carry no information about which place is meant. Dropping them lets
# "Magome to Tsumago" meet "the Nakasendo post road between Magome and Tsumago".
CONNECTORS = frozenset(
    """a an and at between by for from in into of on over the through to via with""".split()
)

# Two places match when half their distinctive words agree, or when one is written out of the
# other — the long name for a walk contains the short one.
SIMILARITY = 0.5


class Key(NamedTuple):
    """What an Atom says, reduced to what grouping compares.

    `verb` is the moment's verb in its own words, so "have" and "eat" arrive here as one. `said`
    is the verb as the itinerary actually wrote it, and `about` is what the action names apart
    from its verb — both kept because a synonym is only safe where the two actions are about the
    same thing.
    """

    place: tuple[str, ...]
    verb: str
    said: str = ""
    about: frozenset = frozenset()


@dataclass(frozen=True)
class SegmentAtom:
    """One tour_atoms row as segment_matching sees it — maps onto aa_social.models.Atom's shape:
    trip_code -> tour_id, day -> itinerary_day (may be None: pre-migration-129 rows, or a row
    the legacy whole-tour path wrote before AA-352/migration 093)."""

    atom_id: str
    tour_id: str
    day: int | None
    place: str
    action: str
    # AA-695 — the tour's country. A real place is in one country, so atoms of different countries
    # never share a Segment. Empty = unknown (joins anything, as before).
    country: str = ""


@dataclass(frozen=True)
class Segment:
    """One real-world moment, and the Atoms that describe it."""

    id: str
    place: str
    action: str
    atom_ids: tuple[str, ...]


def place_norm(place: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (place or "").lower()))


def place_pair(left: str, right: str) -> frozenset:
    return frozenset((place_norm(left), place_norm(right)))


def may_join(left: SegmentAtom, right: SegmentAtom, apart: frozenset = frozenset()) -> bool:
    """AA-695 — two Atoms the moment rule matches may still not share a Segment: different
    countries (deterministic), or a place pair Jev confidently said are different places."""
    if left.country and right.country and left.country != right.country:
        return False
    return place_pair(left.place, right.place) not in apart


def pairs_to_ask(atoms: list[SegmentAtom], new_ids: set[str]) -> dict[frozenset, tuple[str, str, str]]:
    """AA-695 — the place pairs the moment rule would join although their places are written
    differently, involving at least one new Atom: {pair: (place_a, place_b, country)}. Same-country
    (or unknown) only — a country mismatch is already kept apart without asking."""
    keys = {atom.atom_id: _key(atom) for atom in atoms}
    new = [a for a in atoms if a.atom_id in new_ids]
    out: dict[frozenset, tuple[str, str, str]] = {}
    for a in new:
        for b in atoms:
            if a.atom_id == b.atom_id or keys[a.atom_id].place == keys[b.atom_id].place:
                continue
            if a.country and b.country and a.country != b.country:
                continue
            pair = place_pair(a.place, b.place)
            if len(pair) < 2 or pair in out:
                continue
            if _looks_like_one_moment(keys[a.atom_id], keys[b.atom_id]):
                first, second = sorted((a.place, b.place), key=place_norm)
                out[pair] = (first, second, a.country or b.country)
    return out


def derive_segments(atoms: list[SegmentAtom], apart: frozenset = frozenset()) -> list[Segment]:
    """Sort Atoms into Segments. Same Atoms in, same Segments out.

    Quadratic in the number of Atoms given — the DB-facing wrapper below keeps this pool small
    (one tour's real atoms + one cheap pseudo-atom per EXISTING platform Segment, AA-545 Q2),
    not the whole catalog's atoms every run.
    """
    keys = {atom.atom_id: _key(atom) for atom in atoms}
    by_id = {atom.atom_id: atom for atom in atoms}
    memberships = _connected(sorted(keys.items()),
                             lambda a, b: may_join(by_id[a], by_id[b], apart))

    segments = []
    for members in memberships:
        canonical = _canonical(keys[atom_id] for atom_id in members)
        # Total for the same reason the reference repo's own comment gives: two Atoms of one
        # trip on one day reaching the same canonical key would otherwise be separated by list
        # order (a `set` iterates in a `PYTHONHASHSEED`-dependent order in CPython).
        label = min(
            (atom for atom in atoms if atom.atom_id in members and keys[atom.atom_id] == canonical),
            key=lambda atom: (atom.tour_id, _sort_day(atom.day), atom.place, atom.action),
        )
        segments.append(
            Segment(
                id=_mint(canonical, label.country),
                place=label.place,
                action=label.action,
                atom_ids=tuple(sorted(members)),
            )
        )
    return sorted(segments, key=lambda segment: segment.id)


def _sort_day(day: int | None) -> int:
    """A day-less Atom (NULL itinerary_day) sorts before every real day, deterministically —
    just needs to be a total order, not any particular one."""
    return -1 if day is None else day


def _canonical(keys: Iterable[Key]) -> Key:
    """The member key a Segment is named and identified by.

    The most economical naming of the moment — fewest distinctive words, ties broken
    alphabetically, then by verb/said/about so the ordering is total (mirrors the reference
    repo's own fix for a real cross-run instability it found — see its `_canonical()` docstring).
    """
    return min(
        keys,
        key=lambda key: (
            len(key.place),
            key.place,
            key.verb,
            key.said,
            tuple(sorted(key.about)),
        ),
    )


def _mint(canonical: Key, country: str = "") -> str:
    """A new Segment's identity, derived from what its members are — `sha256(place|verb)`, the
    exact origin (Ms. Thư) formula, no `tenant_id`/`tour_id` folded in (AA-545 — see this
    module's own docstring for why: a platform-wide Segment WANTS 2 tours/tenants describing the
    same real-world moment to collide onto one id, not avoid it).

    Only ever used for a Segment nothing platform-wide has seen before. Once minted, an id is
    held: see `reconcile_ids`.

    AA-695 — with a known country it is folded in (`place|verb|country`): the same generic moment
    ("local restaurant — eat lunch") in two countries is now two Segments and needs two ids.
    Without a country the origin formula is unchanged.
    """
    base = f"{' '.join(canonical.place)}|{canonical.verb}"
    return hashlib.sha256((f"{base}|{country}" if country else base).encode()).hexdigest()[:16]


def reconcile_ids(
    derived: list[Segment], assigned: Mapping[str, str]
) -> tuple[list[Segment], dict[str, str]]:
    """Keep the id a Segment already had, rather than re-deriving it.

    Deriving the id from the current members alone cannot survive a re-atomize: a tour that
    names a moment more briefly than anything already grouped re-identifies it, and a tour that
    bridges two Segments makes one id vanish. ADR 0002 requires a stable `segment_id` across
    re-runs — a Slate/Route/Slot built on an old id has to still resolve.

    So an id is derived from member identity when a Segment is first seen — never from arrival
    order — and held from then on. `assigned` maps atom_id to the Segment id it already belongs
    to (for THIS tenant only — the caller scopes it). Where a new Atom bridges two Segments, one
    id has to give way; the surviving id is chosen by content, not by order, and the other is
    returned as an alias so old references still resolve.
    """
    by_id = {segment.id: segment for segment in derived}
    ids = {segment.id: segment.id for segment in derived}
    taken = set(ids.values())
    aliases: dict[str, str] = {}

    # Sorted so the answer never depends on the order Segments were derived in.
    for prior, claimant in sorted(_claims(derived, assigned).items()):
        current = ids[claimant]
        if current == prior:
            continue
        if prior in taken:
            # Another Segment already answers to this id — it derived it from its own members.
            # The claimant keeps what it has and the old id points at whoever holds it.
            aliases[prior] = prior
            continue
        taken.discard(current)
        taken.add(prior)
        ids[claimant] = prior
        aliases[prior] = prior

    kept = [replace(by_id[identifier], id=ids[identifier]) for identifier in by_id]
    for prior, claimant in sorted(_claims(derived, assigned).items()):
        settled = ids[claimant]
        if prior != settled:
            aliases[prior] = settled
    return kept, _flatten(
        {was: target for was, target in aliases.items() if was != target}
    )


def _claims(
    derived: list[Segment], assigned: Mapping[str, str]
) -> dict[str, str]:
    """Which derived Segment has the best claim on each id that already existed.

    Re-extraction can split one Segment into several — a day that arrived as one run-on place
    becomes several Atoms — and then every part inherits the same old id. Only one may have it.
    The part holding most of the old Segment's Atoms wins, and a Segment that already is that id
    keeps it outright.
    """
    contenders: dict[str, dict[str, int]] = {}
    for segment in derived:
        for atom_id in segment.atom_ids:
            prior = assigned.get(atom_id)
            if prior is not None:
                held = contenders.setdefault(prior, {})
                held[segment.id] = held.get(segment.id, 0) + 1

    claims = {}
    for prior, held in contenders.items():
        if prior in held:
            claims[prior] = prior
            continue
        claims[prior] = min(held, key=lambda identifier: (-held[identifier], identifier))
    return claims


def _flatten(aliases: dict[str, str]) -> dict[str, str]:
    """Point every alias at the id that is actually live."""
    resolved = {}
    for start, target in aliases.items():
        seen = {start}
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        resolved[start] = target
    return resolved


def _key(atom: SegmentAtom) -> Key:
    words = re.findall(r"[a-z0-9]+", atom.action.lower())
    rest = _past_the_approach(words)
    return Key(
        place=_place_tokens(atom.place),
        verb=_leading_verb(atom.action),
        said=_stem(rest[0]) if rest else "",
        about=frozenset(_stem(word) for word in rest[1:] if word not in CONNECTORS),
    )


def _place_tokens(place: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", place.lower())
    kept = [word for word in words if word not in CONNECTORS]
    return tuple(sorted(set(kept or words)))


def _leading_verb(action: str) -> str:
    """The verb this action is about, stemmed and put in its own words.

    The first word is the verb — unless it says how the moment was reached rather than what it
    is: "descend to and visit" is a visit. Two itineraries writing "have dinner" and "eat dinner"
    at the same place describe one evening, so the verb is read through the synonym classes above
    (narrow by design — merging two genuinely different moments deletes content).
    """
    words = re.findall(r"[a-z0-9]+", action.lower())
    if not words:
        return ""
    return _in_its_own_words(_stem(_past_the_approach(words)[0]))


def _past_the_approach(words: list[str]) -> list[str]:
    """What is left of an action once it stops saying how you got there."""
    if not words or words[0] not in _REACHED_BY_OPENERS:
        return words
    for connector in _REACHED_BY_CONNECTORS:
        joined = connector.split()
        at = len(joined) + 1
        if words[1: 1 + len(joined)] == joined and len(words) > at:
            return words[at:]
    return words


@lru_cache(maxsize=1)
def _synonyms() -> dict[str, str]:
    """The synonym table, with both sides stemmed the way a verb is."""
    return {
        _stem(verb): _stem(canonical)
        for canonical, verbs in _ACTION_SYNONYM_CLASSES.items()
        for verb in verbs
    }


def _in_its_own_words(verb: str) -> str:
    return _synonyms().get(verb, verb)


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _looks_like_one_moment(left: Key, right: Key) -> bool:
    """A verb match and enough shared place words. An approximation, by design."""
    if left.verb != right.verb or not left.verb:
        return False
    if not _about_the_same_thing(left, right):
        return False
    one, two = set(left.place), set(right.place)
    if not one or not two:
        return False
    if one <= two or two <= one:
        return True
    return len(one & two) / len(one | two) >= SIMILARITY


def _about_the_same_thing(left: Key, right: Key) -> bool:
    """Whether a synonym may stand in for the word the itinerary used.

    Two itineraries that wrote the same verb are taken at their word. Where the words differ and
    only the synonym table made them meet, the actions have to be about the same thing as well —
    the verb is not the moment, the object is.
    """
    if left.said == right.said:
        return True
    if not left.about and not right.about:
        return True
    return bool(left.about & right.about)


def _connected(keyed: list[tuple[str, Key]], allowed=None) -> list[set[str]]:
    """Connected components over `_looks_like_one_moment`, independent of order. `allowed(a, b)`
    (AA-695) can veto a pair the moment rule would join."""
    parent = {atom_id: atom_id for atom_id, _ in keyed}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for index, (atom_id, key) in enumerate(keyed):
        for other_id, other_key in keyed[index + 1:]:
            if _looks_like_one_moment(key, other_key) and (allowed is None or allowed(atom_id, other_id)):
                left, right = find(atom_id), find(other_id)
                if left != right:
                    parent[max(left, right)] = min(left, right)

    memberships: dict[str, set[str]] = {}
    for atom_id, _ in keyed:
        memberships.setdefault(find(atom_id), set()).add(atom_id)
    return list(memberships.values())


# ── DB-facing wrapper (impure) — everything above this line is a pure function ─────────────

_PSEUDO_PREFIX = "__existing_segment__"

# AA-695 A3-7 — "same real-world place?" for place pairs the moment rule would join although they are
# written differently (Bukchon vs Jeonju Hanok Village, Wat Saket vs Wat Si Saket). Only an enforced,
# confident no keeps them apart (ADR 0007); the Verdict is cached per wording + pair, so reruns
# regroup the same way (ADR 0002).
SAME_STAGE = "a3_segment_match"
SAME_Q = "a3_same_place"
SAME_CONCURRENCY = 8


async def _apart_pairs(atoms: list[SegmentAtom], new_ids: set[str], pool) -> tuple[frozenset, int]:
    import asyncio

    from shared.llm_client.decide import decide
    pairs = pairs_to_ask(atoms, new_ids)
    if not pairs:
        return frozenset(), 0
    sem = asyncio.Semaphore(SAME_CONCURRENCY)

    async def one(pair, spec):
        place_a, place_b, country = spec
        async with sem:
            dec = await decide(SAME_STAGE, f"same:{country}:{place_norm(place_a)[:150]}|{place_norm(place_b)[:150]}",
                               {"place_a": place_a, "place_b": place_b, "country": country}, [SAME_Q], pool=pool)
        return pair, dec.rejected(SAME_Q)

    results = await asyncio.gather(*[one(p, spec) for p, spec in pairs.items()])
    return frozenset(p for p, rejected in results if rejected), len(pairs)


async def run_segment_matching(tour_id: str, pool) -> dict:
    """Incrementally fold ONE tour's atoms into the platform-wide Segment set (AA-545 — replaces
    AA-509's per-tenant, full-recompute wrapper; see this module's own docstring for why and
    how). Triggered once, at A3, right after that tour's own atomize (`services/export/
    handler.py::_run_a3_atomize_background()`) — never per-tenant, per-rewrite.

    Loads only: (a) this tour's own atoms, and (b) a cheap one-row-per-Segment "pseudo-atom" for
    EVERY existing platform Segment (its stored `canonical_place`/`canonical_action`, reused
    as-is — see docstring). Runs the unmodified `derive_segments()`/`reconcile_ids()` pair over
    that combined pool, then strips the pseudo entries back out before writing — a pseudo id is
    never a real `tour_atoms` row and must never reach `atom_segment_member`.

    Idempotent: also loads this tour's atoms' own PRIOR assignment (if this tour was already
    processed — a retry, or a re-atomize) so a repeat run for identical content writes nothing
    new.

    Excludes: soft-deleted atoms, empty-day markers (`is_empty_marker`), and any atom whose
    place/action are still NULL (atomized before migration 129 and not yet re-atomized — STEP0
    mục 4/migration 129 comment: no backfill, same precedent as itinerary_day).
    """
    async with pool.acquire() as conn:
        atom_rows = await conn.fetch("""
            SELECT ta.atom_id, ta.tour_id, ta.itinerary_day, ta.place, ta.action,
                   coalesce(rt.country, '') AS country
            FROM acp_contract.v_active_tour_atoms ta
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
            WHERE ta.tour_id = $1::uuid
              AND ta.place IS NOT NULL AND ta.action IS NOT NULL
        """, tour_id)
        if not atom_rows:
            return {"segments_written": 0, "atoms": 0, "aliases": 0, "existing_segments": 0}

        # AA-695 — each existing Segment's country = its member tours' most common country.
        existing_rows = await conn.fetch("""
            SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
                   coalesce(mode() WITHIN GROUP (ORDER BY rt.country), '') AS country
            FROM acp_contract.atom_segment asg
            LEFT JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
            LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
            GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
        """)
        atom_ids = [r["atom_id"] for r in atom_rows]
        assigned_rows = await conn.fetch("""
            SELECT atom_id, segment_id FROM acp_contract.atom_segment_member
            WHERE atom_id = ANY($1::text[])
        """, atom_ids)

    new_atoms = [
        SegmentAtom(r["atom_id"], str(r["tour_id"]), r["itinerary_day"], r["place"], r["action"],
                    r["country"])
        for r in atom_rows
    ]
    pseudo_atoms = [
        SegmentAtom(f"{_PSEUDO_PREFIX}{r['segment_id']}", "", None,
                    r["canonical_place"], r["canonical_action"], r["country"])
        for r in existing_rows
    ]
    assigned = {r["atom_id"]: r["segment_id"] for r in assigned_rows}
    for r in existing_rows:
        assigned[f"{_PSEUDO_PREFIX}{r['segment_id']}"] = r["segment_id"]

    pool_atoms = new_atoms + pseudo_atoms
    apart, asked = await _apart_pairs(pool_atoms, {a.atom_id for a in new_atoms}, pool)   # AA-695
    derived = derive_segments(pool_atoms, apart)
    segments, aliases = reconcile_ids(derived, assigned)

    # Strip pseudo membership before persisting, and drop any resulting segment whose ONLY
    # membership this run is pseudo (nothing new actually touched it — e.g. an existing Segment
    # that reconcile_ids() re-confirmed but this tour's atoms didn't extend).
    real_segments = [
        replace(s, atom_ids=tuple(a for a in s.atom_ids if not a.startswith(_PSEUDO_PREFIX)))
        for s in segments
    ]
    to_write = [s for s in real_segments if s.atom_ids]

    live_ids = {s.id for s in segments}
    # `aliases` (old segment_id -> surviving segment_id) covers every REAL existing Segment that
    # gave way this run — both the ordinary "id superseded" case AA-509 already had, and a
    # bridging merge (2+ existing Segments joined by one of this tour's new atoms), which
    # `reconcile_ids()`/`_claims()` already arbitrates unmodified (this module adds no second,
    # separate merge rule). A losing id's PRE-EXISTING real `atom_segment_member` rows (never
    # loaded into this run's pool — only its pseudo-atom was) must be re-pointed explicitly; the
    # original per-tenant code never needed this because it rebuilt membership from scratch every
    # run.
    alias_rows = [(was, target) for was, target in aliases.items() if target in live_ids]

    async with pool.acquire() as conn:
        async with conn.transaction():
            if alias_rows:
                await conn.executemany("""
                    UPDATE acp_contract.atom_segment_member SET segment_id = $2
                    WHERE segment_id = $1
                """, alias_rows)

            # atom_segment: UPSERT-only, never DELETEd (module docstring + migration 129 comment
            # — required by atom_segment_alias's own FK, an id that "gave way" still has to
            # exist as a row).
            if to_write:
                await conn.executemany("""
                    INSERT INTO acp_contract.atom_segment
                        (segment_id, canonical_place, canonical_action)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (segment_id) DO UPDATE SET
                        canonical_place = excluded.canonical_place,
                        canonical_action = excluded.canonical_action
                """, [(s.id, s.place, s.action) for s in to_write])
                await conn.executemany("""
                    INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                """, [(s.id, atom_id) for s in to_write for atom_id in s.atom_ids])

            if alias_rows:
                await conn.executemany("""
                    INSERT INTO acp_contract.atom_segment_alias
                        (segment_id_old, segment_id_canonical)
                    VALUES ($1, $2)
                    ON CONFLICT (segment_id_old) DO UPDATE SET
                        segment_id_canonical = excluded.segment_id_canonical
                """, alias_rows)
                # An alias whose target has itself since given way (this run, or a prior one)
                # follows it on — mirrors aa_social.stages.atoms._store_segments()'s own 2-pass
                # chain-resolve, now over the whole platform segment_id space (no tenant filter).
                await conn.execute("""
                    UPDATE acp_contract.atom_segment_alias outer_a
                    SET segment_id_canonical = inner_a.segment_id_canonical
                    FROM acp_contract.atom_segment_alias inner_a
                    WHERE inner_a.segment_id_old = outer_a.segment_id_canonical
                """)
                await conn.execute("""
                    DELETE FROM acp_contract.atom_segment_alias
                    WHERE segment_id_old = segment_id_canonical
                """)

    return {
        "segments_written": len(to_write), "atoms": len(new_atoms), "aliases": len(alias_rows),
        "existing_segments": len(existing_rows), "same_place_asked": asked, "kept_apart": len(apart),
    }

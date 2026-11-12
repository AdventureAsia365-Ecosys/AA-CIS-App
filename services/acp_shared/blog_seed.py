"""AA-1081 — the T7 minimum seed for Blog (the AA-1077 memo, option C), behind `T7_BLOG_MIN_SEED_WORDS`.

The AA-741 phase 3 audit (`o1 PIECE SEED`, root `docs/audits/2026-11-10-S276-aa741-phase3/` §9.3): a Blog piece's
length follows its seed (r = 0.424), and a seed of <= 991 words gave 9 F4 (too short) holds in 12 pieces. The AA-1077
memo recommends that a Subject whose seed cannot carry a ~900-word article is not proposed on Blog (it stays on the
shorter Channels). This module holds the setting and the estimate; `slate._fetch_short_blog_seeds()` applies it.

**Setting**: env `T7_BLOG_MIN_SEED_WORDS` (an integer number of words), read per call. Unset, empty, 0, negative or
not a number = off (the default): the Slate behaves exactly as before. The tech lead's decision only sets the number.

**Blog seed words** (the audit's measure: the writer's seed words + its route moment words) estimated at T7 from what
the pick would hand T9: the seed atom's `atom_seed_text()` (label, evidence, hook, season, siblings, place card) plus
`route_moment_seeds()` over the walk the pick resolves (`slate._segment_walk()`), with the moments A3 excluded from
the walk dropped as T9's `_fetch_route_segments()` does. DB reads only, no LLM.

**Length-sized floor (S279, AA-1077 last slice).** The flat 600-word floor let a Subject through whose seed could not
carry a blog: the S278 v7 trial wrote 748 words / 4,425 chars from a seed that cleared 600 and the extend step then
found no grounded material left (the writer refuses to invent). So the floor is also SIZED from the F4 blog floor and
the measured ratio of a blog's length to its grounded seed: ``sized = ceil(BLOG_FLOOR_CHARS / chars_per_seed_word)``.
The effective floor is ``max(flat, sized)`` (`seed_floor()`), applied only while the Blog minimum seed is on (the
flat setting > 0), so a deployment without it is unchanged.

**Ratio setting**: env `T7_BLOG_CHARS_PER_SEED_WORD` (a positive number of tag-stripped blog characters per seed
word), read per call; unset or empty = the registry threshold `t7.blog_chars_per_seed_word`
(services/quality/thresholds.yaml); 0, negative or not a number = no sizing (the flat floor alone). The registry
default is one written word per seed word at the measured 5.8 characters per word (the no-invention writer cannot
write more than its material); `scripts/aa1077/seed_floor_replay.py` measures the ratio on stored blog pieces ($0,
read-only) and prints the value to set.
"""
from __future__ import annotations

import math
import os
import re
from typing import Optional

from services.acp_content_writing import length_contract
from services.acp_shared.atom_seed import atom_seed_text, route_moment_seeds
from services.quality import registry as _qr

MIN_SEED_ENV = "T7_BLOG_MIN_SEED_WORDS"
RATIO_ENV = "T7_BLOG_CHARS_PER_SEED_WORD"
RATIO_THRESHOLD = "t7.blog_chars_per_seed_word"
# Read at import so a missing registry key fails the build, not a slate job.
DEFAULT_CHARS_PER_SEED_WORD = float(_qr.threshold(RATIO_THRESHOLD))

# Why a Blog candidate was not proposed on Blog (the slate's per-Subject `blog_coverage.skipped`, admin only).
SKIP_THIN_WALK = "thin_walk"                    # the walk the pick would resolve is too thin (slate thin rule)
SKIP_BELOW_MIN_SEED = "seed_below_min"          # the seed is under the flat Blog minimum seed
SKIP_BELOW_LENGTH_FLOOR = "seed_below_length_floor"   # clears the flat floor, cannot carry the F4 blog floor
SKIP_REASONS = (SKIP_THIN_WALK, SKIP_BELOW_MIN_SEED, SKIP_BELOW_LENGTH_FLOOR)

# The audit script's word regex (p3_1_holds_deep.py WORD_RE), so a number set from the memo means the same thing here.
WORD_RE = re.compile(r"[A-Za-zÀ-ɏ0-9]+(?:['’-][A-Za-zÀ-ɏ0-9]+)*")


def min_seed_words() -> int:
    """The Blog minimum seed in words; 0 = off (unset, empty, 0, negative or not an integer)."""
    raw = (os.environ.get(MIN_SEED_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


def chars_per_seed_word() -> float:
    """Blog characters per seed word for the sized floor: env `T7_BLOG_CHARS_PER_SEED_WORD`, else the registry default;
    0.0 = no sizing (set to 0, negative or not a number)."""
    raw = (os.environ.get(RATIO_ENV) or "").strip()
    if not raw:
        return DEFAULT_CHARS_PER_SEED_WORD
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 and math.isfinite(value) else 0.0


def sized_min_seed_words(ratio: Optional[float] = None) -> int:
    """The seed words a blog needs to reach the F4 floor at `ratio` chars per seed word (default the setting); 0 when
    sizing is off. Pure but for the setting read."""
    r = chars_per_seed_word() if ratio is None else ratio
    if not r or r <= 0:
        return 0
    return int(math.ceil(length_contract.BLOG_FLOOR_CHARS / r))


def seed_floor() -> dict:
    """The Blog seed floor in force: {"min_seed_words": the effective floor (0 = off), "flat": the flat setting,
    "sized": the length-sized floor, "chars_per_seed_word", "floor_chars"}. Off (all 0) while the flat setting is off:
    the sized floor supplements the Blog minimum seed, it never switches it on."""
    flat = min_seed_words()
    ratio = chars_per_seed_word() if flat > 0 else 0.0
    sized = sized_min_seed_words(ratio) if flat > 0 else 0
    return {"min_seed_words": max(flat, sized), "flat": flat, "sized": sized,
            "chars_per_seed_word": round(ratio, 3), "floor_chars": length_contract.BLOG_FLOOR_CHARS}


def skip_reason(words: Optional[int], floor: dict) -> Optional[str]:
    """Why a measured seed of `words` is not proposed on Blog under `floor` (`seed_floor()`), or None when it carries
    it (or was not measured: an unmeasured seed is kept). Pure."""
    if words is None or not floor.get("min_seed_words"):
        return None
    if words < (floor.get("flat") or 0):
        return SKIP_BELOW_MIN_SEED
    if words < floor["min_seed_words"]:
        return SKIP_BELOW_LENGTH_FLOOR
    return None


def seed_words(text: Optional[str]) -> int:
    return len(WORD_RE.findall(text or ""))


# One live, non-alias member atom per Segment on the walk's tour (first by atom_id, as T9's _ROUTE_SEGMENT_TEXT_QUERY),
# with A3's exclusion (every current ranking row of the (segment, tour) excluded) and the walk order fields.
_WALK_ATOMS_SQL = """
    SELECT DISTINCT ON (m.segment_id) m.segment_id, m.atom_id, ta.text, ta.itinerary_day, ta.created_at,
           (SELECT bool_and(ar.excluded_reason IS NOT NULL) FROM acp_contract.atom_ranking ar
            WHERE ar.segment_id = m.segment_id AND ar.tour_id = ta.tour_id
              AND ar.superseded_at IS NULL) AS excluded
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
    WHERE m.segment_id = ANY($1::text[]) AND ta.tour_id = $2::uuid
      AND ta.owner_scope IN ('platform', $3::text)
      AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
    ORDER BY m.segment_id, m.atom_id
"""


def walk_atoms(rows: list, segment_ids: list[str]) -> list[tuple[str, str]]:
    """Pure: the walk's (atom_id, label) in day order (no day last, then extraction order, then walk order), with the
    excluded moments dropped unless that leaves nothing (T9's rule)."""
    order = {s: i for i, s in enumerate(segment_ids)}
    rows = [dict(r) for r in rows]
    kept = [r for r in rows if not r.get("excluded")] or rows

    def key(r: dict) -> tuple:
        created = r.get("created_at")
        ts = created.timestamp() if hasattr(created, "timestamp") else float("inf")
        day = r.get("itinerary_day")
        return (day is None, day or 0, ts, order.get(r["segment_id"], len(order)))

    return [(r["atom_id"], r["text"]) for r in sorted(kept, key=key)]


async def blog_seed_words(conn, tenant_id, atom_id: str, tour_id: Optional[str],
                          route_segment_ids: Optional[list[str]]) -> int:
    """Words the T9 writer would get for a Blog pick seeded on `atom_id`: its seed text, plus the walk's moment seeds
    when the pick carries a walk (`route_segment_ids` on `tour_id`). 0 when the atom is not live for the tenant."""
    words = seed_words(await atom_seed_text(conn, atom_id, str(tenant_id)))
    if route_segment_ids and tour_id:
        rows = await conn.fetch(_WALK_ATOMS_SQL, list(route_segment_ids), str(tour_id), str(tenant_id))
        atoms = walk_atoms(rows, list(route_segment_ids))
        if atoms:
            words += sum(seed_words(block) for _aid, block in await route_moment_seeds(conn, atoms, str(tenant_id)))
    return words


__all__ = ["DEFAULT_CHARS_PER_SEED_WORD", "MIN_SEED_ENV", "RATIO_ENV", "RATIO_THRESHOLD", "SKIP_BELOW_LENGTH_FLOOR",
           "SKIP_BELOW_MIN_SEED", "SKIP_REASONS", "SKIP_THIN_WALK", "WORD_RE", "blog_seed_words", "chars_per_seed_word",
           "min_seed_words", "seed_floor", "seed_words", "sized_min_seed_words", "skip_reason", "walk_atoms"]

"""AA-1048 — pooled claim classes: measure a pool first, split it later (tech lead decision S278; root ADR 0020
amendment "Pooled claim classes").

Why. The R1 bar needs n_pos >= 20 labelled violations of ONE class on one writer surface (sample_size.BARS[0]). The
held-out labels of S275 reached at most 9 for any (writer, class), while the unsupported-trip-claim classes of
t9_blog together had 21. An entailment or extraction instrument does not know the class at run time anyway
(r1.py "Measurement"): it calls a sentence unsupported, and its precision is already taken over every fire on the
writer. So a pool measures the instrument's recall on the union of a few closely related classes, with the same bar.

Rules (the bar is not lowered, it is applied to the pool):
  - a pool is DECLARED here, in a reviewed PR (the tech lead decides its members and writers; the record states the
    pool, its members and the declaration's sha). Nothing derives a pool from the data.
  - a pool passes for a writer when that writer's precision over every fire clears the bar (unchanged), the pool's
    n_pos on that writer (the labelled violations of all its members) reaches the bar's n_pos, and at least one of
    them was caught; the instrument conditions and the held-out split apply as for a class.
  - a passing pool calibrates its MEMBERS on that writer and nothing else (`calibrated_keys`): a gate in enforce
    mode holds only on a fire whose class (the instrument's own class in extraction mode) is a member; a fire of
    any other class warns. A class is in at most one pool per writer, so a fire maps to one pool.
  - split later: the record keeps every member's own n_pos / tp / recall per writer and lists the members that
    reach the bar alone (`split_ready`); such a member is then measured as its own class and the pool shrinks or
    retires in a later declaration.

Pure: the standard library; `problems` reads the policy seam (services/ground_truth/policy.py) when no class map is
given.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

POOL_PREFIX = "pool."
_POOL_RE = re.compile(r"^pool\.[a-z0-9_]{1,40}$")


@dataclass(frozen=True)
class Pool:
    id: str                          # "pool.<name>": never a policy class id
    members: tuple[str, ...]         # policy class ids
    writers: tuple[str, ...]         # writer keys (claims.WRITERS) the pool is measured on
    decided: str                     # who declared it and when (the record carries it)

    def declaration(self) -> dict:
        return {"id": self.id, "members": sorted(self.members), "writers": sorted(self.writers),
                "decided": self.decided}


# The declared pools. Tech lead S278 (12/11/2026): the unsupported-trip-claim classes that the claim instrument calls
# from the same reading (a claim about this trip the source does not state): an embellished, an invented included
# service, a fact bound to the wrong subject, a number not in the source. fact.name (names, caught by the S1 names /
# F1 checks) and fact.direction / fact.self_contradiction (other readings) stay separate classes.
POOLS: dict[str, Pool] = {
    "pool.fact_unsupported": Pool(
        id="pool.fact_unsupported",
        members=("fact.embellish", "fact.inclusion", "fact.misbinding", "fact.number"),
        writers=("t2", "t9_blog", "t9_social"),
        decided="tech lead S278 (12/11/2026): pooled class first, split later"),
}


def declared(pools: Optional[Mapping[str, Pool]] = None) -> dict[str, Pool]:
    return dict(POOLS if pools is None else pools)


def pools_sha(pools: Optional[Mapping[str, Pool]] = None) -> str:
    """The sha of the declaration (ids, members, writers, decided): a record and a count script name it, so a record
    measured under another declaration is never read as this one."""
    body = [p.declaration() for _k, p in sorted(declared(pools).items())]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def pool_of(writer: str, claim_class: Optional[str], pools: Optional[Mapping[str, Pool]] = None) -> Optional[Pool]:
    """The pool a (writer, class) belongs to, else None."""
    for p in declared(pools).values():
        if writer in p.writers and claim_class in p.members:
            return p
    return None


def problems(pools: Optional[Mapping[str, Pool]] = None,
             classes: Optional[Mapping[str, Iterable[str]]] = None) -> list[str]:
    """Why a declaration is not usable (empty: usable). `classes` maps a policy class id to its writers (default: the
    policy seam). A pool needs an id of the pool shape that is not a class id, at least two members, members that
    are policy classes of every writer of the pool, and no class in two pools on one writer."""
    if classes is None:
        from services.ground_truth import policy

        classes = {c.id: c.writers for c in policy.policy_classes().values()}
    out: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    for key, p in sorted(declared(pools).items()):
        if key != p.id:
            out.append(f"{key}: the key differs from the pool id {p.id!r}")
        if not _POOL_RE.match(p.id):
            out.append(f"{p.id}: a pool id is 'pool.<name>' (lower case, digits, underscore)")
        if p.id in classes:
            out.append(f"{p.id}: a pool id may not be a policy class id")
        if len(set(p.members)) < 2:
            out.append(f"{p.id}: a pool has at least two distinct members")
        if not p.writers:
            out.append(f"{p.id}: a pool names the writers it is measured on")
        if not str(p.decided or "").strip():
            out.append(f"{p.id}: a pool says who declared it")
        for m in p.members:
            if m not in classes:
                out.append(f"{p.id}: {m!r} is not a policy class")
                continue
            for w in p.writers:
                if w not in set(classes[m]):
                    out.append(f"{p.id}: {m} is not a class of writer {w}")
                other = seen.setdefault((w, m), p.id)
                if other != p.id:
                    out.append(f"{p.id}: {w}:{m} is already in {other}")
    return out


def positives(rows: Iterable[tuple[str, str]], pools: Optional[Mapping[str, Pool]] = None) -> dict[str, int]:
    """'<writer>:<pool>' -> labelled violations of its members, from (writer, class) pairs of violations (a count
    script's n_pos per pool; r1.measure computes the same from its items)."""
    out: Counter = Counter()
    for writer, cls in rows:
        p = pool_of(writer, cls, pools)
        if p is not None:
            out[f"{writer}:{p.id}"] += 1
    return dict(sorted(out.items()))


def calibrated_keys(record: Mapping[str, Any]) -> list[str]:
    """The '<writer>:<class>' keys a measurement record (r1.measure) calibrates: its passing classes plus every member
    of each passing pool on that writer. This is what a gate's calibrated set (claim_shadow.CALIBRATED) may hold: a
    pool never calibrates a class outside its members."""
    keys = set(record.get("passing_classes") or [])
    pools_rec = record.get("pools") or {}
    for entry in record.get("passing_pools") or []:
        writer, _, pid = str(entry).partition(":")
        for m in (pools_rec.get(pid) or {}).get("members") or []:
            keys.add(f"{writer}:{m}")
    return sorted(keys)


__all__ = ["POOLS", "POOL_PREFIX", "Pool", "calibrated_keys", "declared", "pool_of", "pools_sha", "positives",
           "problems"]

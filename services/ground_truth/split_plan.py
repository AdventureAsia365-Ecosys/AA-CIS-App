"""AA-1048 W2 (S273) — how many held-out claims a label set has, how many it needs, and which existing objects close
the gap. Ids, counts and dollars only: no text.

Why CL-1 had 24 held-out claims of 1,602: the split is frozen per tour (claims.split_of over "tour_id:<id>", 30% of
the hash space), and the first export took only the T9 writers of golden fixture 1.3.0, whose 54 T9 pieces sit on 9
tours (the 20 blogs on 3). Two tours fell on the held-out side, both with social posts only (3 pieces, 24 claims). A
30% share of 9 groups is a coin toss, not 30% of the claims. The fixture's S1 and T2 samples (70 objects on 63 tours)
put 1,265 more claims on the held-out side; the T9 writers need pieces of more tours.

The fix keeps the split (frozen: changing it would move labelled claims between sides) and chooses WHAT to label:
`plan` lists, per writer, the existing objects whose tour is on the held-out side (round robin over tours, a cap per
tour so one tour cannot dominate), enough to reach the target, and prices what is still missing as new pieces on
held-out tours. The target is the R1 precision bar on one writer surface (sample_size.held_out_claims_needed at the
pooled violation rate of the first panel round): the instrument's precision is measured over every fire on a writer
surface (r1.measure), and a held-out measurement is the only one that passes.

`live_candidates` reads the T9 pieces and T2 tenant versions a golden-set addendum can freeze (the same filters as
scripts/aa729/select_samples.py `_T9_SQL` / `_T2_SQL`), their tour and nothing else; the staff route
GET /admin/writing-memory/claim-labels/split-plan serves it, `cli split-plan` combines it with an export file and
writes the id list `select_samples --ids` freezes.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from services.ground_truth import claims as cl
from services.ground_truth import sample_size as ss

# Claims per object of each writer in the export of golden fixture 1.3.0 (S273: a1 3,384 / 50, t2 1,187 / 20,
# t9_blog 1,193 / 20, t9_social 409 / 34).
CLAIMS_PER_OBJECT = {"a1": 68, "t2": 59, "t9_blog": 60, "t9_social": 12}
# Writer cost of one new object (S272 log: T9 about 0.11 USD per article, T2 about 0.19 USD per tour). S1 is not
# priced: an S1 rewrite is a catalogue wave, never a label-set purchase.
COST_PER_OBJECT_USD = {"t2": 0.19, "t9_blog": 0.11, "t9_social": 0.11}
# First panel round (CL-1 1.0.0, S272): 9 of 150 claims had a violation majority.
PANEL1_VIOLATION_RATE = 0.06
DEFAULT_OBSERVED_PRECISION = 0.92
DEFAULT_PER_TOUR_CAP = 6
LIVE_WRITERS = ("t2", "t9_blog", "t9_social")


@dataclass(frozen=True)
class Candidate:
    """An object a golden-set addendum can freeze: its id (T9 piece / T2 version), writer and tour."""
    id: str
    writer: str
    tour_id: Optional[str]
    status: str = ""

    @property
    def split(self) -> Optional[str]:
        return cl.split_of(f"tour_id:{self.tour_id}") if self.tour_id else None

    def as_dict(self) -> dict:
        return {"id": self.id, "writer": self.writer, "tour_id": self.tour_id, "status": self.status,
                "split": self.split}


def target_held_out_claims(*, violation_rate: float = PANEL1_VIOLATION_RATE,
                           observed_precision: float = DEFAULT_OBSERVED_PRECISION) -> Optional[int]:
    """Held-out claims one writer surface needs for the R1 bar (llm_instrument: precision >= 0.90, lower bound >= 0.80,
    n_pos >= 20) when the instrument fires about as often as a violation occurs. None when out of reach."""
    return ss.held_out_claims_needed(fire_rate=violation_rate, violation_rate=violation_rate,
                                     observed_precision=observed_precision, bar=ss.BARS[0])


def export_stats(export_lines: Iterable[dict]) -> dict:
    """Per writer of an export file: claims, objects, tours (split groups) and the held-out share. Counts only."""
    claims: dict[str, dict[str, int]] = defaultdict(lambda: {"train": 0, "held_out": 0})
    objects: dict[str, set] = defaultdict(set)
    groups: dict[str, dict[str, set]] = defaultdict(lambda: {"train": set(), "held_out": set()})
    ids: set[str] = set()
    for e in export_lines:
        w = e["writer"]
        group = cl.split_group(e.get("refs"), e["content_sha256"])
        side = cl.split_of(group)
        claims[w][side] += 1
        objects[w].add(e["content_sha256"])
        groups[w][side].add(group)
        refs = e.get("refs") or {}
        ids.update(str(refs[k]).lower() for k in ("piece_id", "version_id", "tour_id") if refs.get(k))
    out = {w: {"claims": c["train"] + c["held_out"], "held_out_claims": c["held_out"], "objects": len(objects[w]),
               "groups": len(groups[w]["train"] | groups[w]["held_out"]), "held_out_groups": len(groups[w]["held_out"])}
           for w, c in sorted(claims.items())}
    return {"writers": out, "ids": ids}


def _round_robin(cands: list[Candidate], cap: Optional[int]) -> list[Candidate]:
    """Held-out candidates ordered one tour at a time (tours and ids sorted, so the order is stable), at most `cap`
    per tour."""
    by_tour: dict[str, list[Candidate]] = defaultdict(list)
    for c in sorted(cands, key=lambda c: (c.tour_id or "", c.id)):
        by_tour[c.tour_id or ""].append(c)
    out: list[Candidate] = []
    depth = 0
    while True:
        added = False
        for tour in sorted(by_tour):
            if (cap is None or depth < cap) and depth < len(by_tour[tour]):
                out.append(by_tour[tour][depth])
                added = True
        if not added:
            return out
        depth += 1


def plan(candidates: Iterable[Candidate], *, target_claims: int, have: Optional[dict[str, int]] = None,
         exclude: Iterable[str] = (), writers: Iterable[str] = LIVE_WRITERS,
         claims_per_object: Optional[dict[str, int]] = None, per_tour_cap: Optional[int] = DEFAULT_PER_TOUR_CAP
         ) -> dict:
    """Per writer: the held-out claims an export already has (`have`), the existing held-out objects to freeze next
    (`picks`: ids, round robin over tours), the objects still missing after them and their writer cost. Objects
    already exported (`exclude`: piece / version ids) and objects without a tour (their split would fall back to the
    item) are never picked. Pure."""
    cpo = {**CLAIMS_PER_OBJECT, **(claims_per_object or {})}
    have = dict(have or {})
    skip = {str(x).lower() for x in exclude}
    pool: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        pool[c.writer].append(c)
    out: dict[str, dict] = {}
    total_cost = 0.0
    for w in writers:
        cands = pool.get(w, [])
        with_tour = [c for c in cands if c.tour_id]
        held = [c for c in with_tour if c.split == "held_out" and c.id.lower() not in skip]
        need_claims = max(0, target_claims - int(have.get(w, 0)))
        need_objects = math.ceil(need_claims / cpo[w]) if need_claims else 0
        picks = _round_robin(held, per_tour_cap)[:need_objects]
        missing = max(0, need_objects - len(picks))
        cost = COST_PER_OBJECT_USD.get(w)
        missing_cost = round(missing * cost, 2) if cost is not None else None
        total_cost += missing_cost or 0.0
        out[w] = {"candidates": len(cands), "without_tour": len(cands) - len(with_tour),
                  "tours": len({c.tour_id for c in with_tour}),
                  "held_out_tours": sorted({c.tour_id for c in with_tour if c.split == "held_out"}),
                  "held_out_candidates": len(held), "have_held_out_claims": int(have.get(w, 0)),
                  "need_held_out_claims": need_claims, "claims_per_object": cpo[w], "need_objects": need_objects,
                  "picks": [c.id for c in picks], "picked_claims_est": len(picks) * cpo[w],
                  "missing_objects": missing, "missing_cost_usd": missing_cost}
    return {"target_held_out_claims_per_writer": target_claims, "per_tour_cap": per_tour_cap, "writers": out,
            "missing_cost_usd": round(total_cost, 2)}


def ids_file(result: dict) -> list[dict]:
    """The picks as the id list scripts/aa729/select_samples.py `--ids` reads ({"id", "fills"} per object)."""
    return [{"id": i, "fills": f"aa1048_held_out:{w}"} for w, row in result["writers"].items() for i in row["picks"]]


_T9_SQL = """
    SELECT cp.piece_id::text AS id, cp.channel, cp.status, agr.trip_id::text AS tour_id
      FROM acp_shared.content_piece cp
      LEFT JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
     WHERE cp.status IN ('approved', 'held') AND cp.writer_input IS NOT NULL AND length(cp.content_text) > 0
       AND COALESCE(cp.writer_input->>'seed', '') <> ''
"""
_T2_SQL = """
    SELECT v.id::text AS id, v.status, pt.tour_id::text AS tour_id
      FROM gold_aa_internal.tenant_tour_versions v
      JOIN gold_aa_internal.published_tours pt ON pt.id = v.published_tour_id
     WHERE v.edit_source IN ('ai_generated', 're_rewrite') AND v.status NOT IN ('pending', 'failed')
"""


async def live_candidates(conn) -> list[Candidate]:
    """The T9 pieces and T2 tenant versions on the DB that a golden-set addendum can freeze: ids, status and tour."""
    out = [Candidate(r["id"], "t9_blog" if (r["channel"] or "blog") == "blog" else "t9_social", r["tour_id"],
                     r["status"]) for r in await conn.fetch(_T9_SQL)]
    out += [Candidate(r["id"], "t2", r["tour_id"], r["status"]) for r in await conn.fetch(_T2_SQL)]
    return out


__all__ = ["CLAIMS_PER_OBJECT", "COST_PER_OBJECT_USD", "Candidate", "DEFAULT_OBSERVED_PRECISION",
           "DEFAULT_PER_TOUR_CAP", "LIVE_WRITERS", "PANEL1_VIOLATION_RATE", "export_stats", "ids_file",
           "live_candidates", "plan", "target_held_out_claims"]

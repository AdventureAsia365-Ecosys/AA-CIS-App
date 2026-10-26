"""AA-990 — carry angle-gate requests over to the atom that replaced theirs.

An angle-gate request stores the atom it was planned from (``acp_shared.angle_gate_request.atom_id``). A later
atomize run soft-deletes that atom (``tour_atoms.deleted``) and writes new ones for the same tour, so T8 / T9 on the
request answer 409 ``atom_replaced`` (``atom_replaced.py``). S252: 14 requests on replaced atoms, all single-atom.

``plan_repoints`` is the pure decision; the one-time run is the ECS-exec script in the S252 runbook (``cis_app``,
DRY RUN first, restore file before the apply). Replacement = a live atom of the same tour (source: every version of
the tour, through ``raw_tours.source_group_id``) that is a platform atom or the request's tenant's own, not an empty
marker, for the same place:

  1. same ``name_norm(place)``; several → the same itinerary day; several → the same activity type;
  2. else the same place card (``atom_place_card``), narrowed the same way.

Exactly one candidate left → ``repoint``; more → ``ambiguous``; none → ``no_match``; the old atom unknown →
``no_atom``. A request that is not repointed keeps its status: angle_gate_request has no status for "atom replaced"
(CHECK: pending_goal, pending_choice, approved, reusable), and T8/T9 already answer the typed 409 with the
"start a new post from Social Content" sentence for it.
"""
from __future__ import annotations

from typing import Optional

from services.places.names import name_norm

OUTCOMES = ("repoint", "ambiguous", "no_match", "no_atom", "already_live")

# Requests whose atom is not live (same filter as atom_replaced._ATOM_LIVE_SQL, per request owner scope).
STALE_REQUESTS_SQL = """
    SELECT r.request_id::text AS request_id, r.tenant_id::text AS tenant_id, r.atom_id, r.status
      FROM acp_shared.angle_gate_request r
     WHERE r.route_segment_ids IS NULL
       AND NOT EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms a
                        WHERE a.atom_id = r.atom_id AND a.owner_scope IN ('platform', r.tenant_id::text)
                          AND NOT a.deleted AND NOT a.is_empty_marker)
     ORDER BY r.created_at, r.request_id
"""

# The replaced atoms (deleted rows included) with their tour's source key and place card.
OLD_ATOMS_SQL = """
    SELECT a.atom_id, a.tour_id::text AS tour_id, a.owner_scope, a.itinerary_day, btrim(a.place) AS place,
           a.activity_type, apc.card_id::text AS card_id,
           COALESCE(rt.source_group_id, rt.tour_id, a.tour_id)::text AS source_id
      FROM acp_contract.tour_atoms a
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = a.tour_id
      LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = a.atom_id
     WHERE a.atom_id = ANY($1::text[])
"""

# Live atoms of the same sources (every version of the tour; v_active_tour_atoms keeps the live one only).
CANDIDATES_SQL = """
    SELECT a.atom_id, a.tour_id::text AS tour_id, a.owner_scope, a.itinerary_day, btrim(a.place) AS place,
           a.activity_type, apc.card_id::text AS card_id,
           COALESCE(rt.source_group_id, rt.tour_id, a.tour_id)::text AS source_id
      FROM acp_contract.v_active_tour_atoms a
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = a.tour_id
      LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = a.atom_id
     WHERE NOT a.deleted AND NOT a.is_empty_marker
       AND COALESCE(rt.source_group_id, rt.tour_id, a.tour_id)::text = ANY($1::text[])
"""

# Guarded: only a request that still points at the atom the plan saw is changed.
REPOINT_SQL = """
    UPDATE acp_shared.angle_gate_request SET atom_id = $3, updated_at = now()
     WHERE request_id = $1::uuid AND atom_id = $2
"""


def _narrow(cands: list[dict], old: dict) -> tuple[Optional[dict], str]:
    """(the one candidate, how it was chosen) or (None, 'ambiguous'/'none')."""
    if not cands:
        return None, "none"
    if len(cands) == 1:
        return cands[0], ""
    same_day = [c for c in cands if c.get("itinerary_day") == old.get("itinerary_day")]
    if len(same_day) == 1:
        return same_day[0], "_day"
    pool = same_day or cands
    act = (old.get("activity_type") or "").strip().lower()
    same_act = [c for c in pool if (c.get("activity_type") or "").strip().lower() == act]
    if len(same_act) == 1:
        return same_act[0], "_day_activity" if same_day else "_activity"
    return None, "ambiguous"


def _same(rule: str, cand: dict, old: dict, place: str) -> bool:
    if rule == "place":
        return bool(place) and name_norm(cand.get("place")) == place
    return bool(old.get("card_id")) and cand.get("card_id") == old.get("card_id")


def plan_repoints(requests: list[dict], old_atoms: dict[str, dict], candidates: list[dict]) -> list[dict]:
    """One decision per request: {request_id, tenant_id, status, old_atom_id, new_atom_id, outcome, rule}.

    requests: STALE_REQUESTS_SQL rows; old_atoms: {atom_id: OLD_ATOMS_SQL row}; candidates: CANDIDATES_SQL rows."""
    by_source: dict[str, list[dict]] = {}
    for c in candidates:
        by_source.setdefault(str(c["source_id"]), []).append(c)
    out = []
    for r in sorted(requests, key=lambda x: str(x["request_id"])):
        d = {"request_id": str(r["request_id"]), "tenant_id": str(r["tenant_id"]), "status": r.get("status"),
             "old_atom_id": r["atom_id"], "new_atom_id": None, "outcome": "no_atom", "rule": None}
        old = old_atoms.get(r["atom_id"])
        if old is None:
            out.append(d)
            continue
        same_source = by_source.get(str(old["source_id"]), [])
        if any(c["atom_id"] == r["atom_id"] for c in same_source):
            d["outcome"] = "already_live"
            out.append(d)
            continue
        scope = {"platform", str(r["tenant_id"])}
        pool = [c for c in same_source if c.get("owner_scope") in scope]
        place = name_norm(old.get("place"))
        d["outcome"] = "no_match"
        for rule in ("place", "card"):
            best, how = _narrow([c for c in pool if _same(rule, c, old, place)], old)
            if best is not None:
                d.update(new_atom_id=best["atom_id"], outcome="repoint", rule=rule + how)
                break
            if how == "ambiguous":            # several equal candidates: never guess, never fall to a weaker rule
                d["outcome"] = "ambiguous"
                break
        out.append(d)
    return out


def summary(plan: list[dict]) -> dict:
    """Counts by outcome and by rule (what the DRY RUN prints)."""
    by_outcome = {k: 0 for k in OUTCOMES}
    by_rule: dict[str, int] = {}
    for d in plan:
        by_outcome[d["outcome"]] = by_outcome.get(d["outcome"], 0) + 1
        if d["rule"]:
            by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
    return {"requests": len(plan), **by_outcome, "by_rule": by_rule}

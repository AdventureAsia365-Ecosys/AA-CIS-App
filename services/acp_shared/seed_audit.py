"""AA-863 — admin view of the Slate seed: which atom a Subject / T9 piece is written from, where that
atom's place and country are, and whether they fit the Subject.

The seed rule itself lives in `subject_seed.py` (AA-857). This module only READS it for admin pages
(`/admin/content-quality` pieces, `/admin/dashboard/slate`); it never decides what a tenant is
offered and is never used on a tenant route.

  * T9 piece: the seed is `angle_gate_request.atom_id` (frozen when the request was created).
  * Subject: the latest request's `atom_id` once picked (`source="request"`), else the seed a pick
    would use now (`source="computed"`, `choose_representative` / `choose_route_anchor` over the same
    inputs `slate._resolve_representative_atom()` reads).

`seed_check()` (pure) flags a seed whose place is not the Subject's place under
`subject_seed.match_grade()` (the very function `choose_representative` grades with — not a second
copy of it), or whose country is not the Segment's country. The Segment's country is the set of
known countries of its live, non-alias members on `owner_scope IN ('platform', <tenant>)` — what the
seed rule sees; a Segment that still spans several countries has no single country, so any seed in
it is flagged. A Route's country is its tour's country, and a Route is never place-checked (its
anchor may be any member of the first Segment on that tour, by design).

All reads are set-based: a fixed number of queries per page, whatever the row count (no N+1).
Any read error leaves the rows without `seed` rather than failing the admin page.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

import structlog

from services.acp_contract.segment_matching import resolve_segment_aliases
from services.acp_shared.place_cards import fold
from services.acp_shared.slate import _tenant_tour_ids
from services.acp_shared.subject_seed import (
    GRADE_CORE, GRADE_EXACT, GRADE_FORM, GRADE_PAREN, GRADE_TOKENS, Member, choose_representative,
    choose_route_anchor, load_alias_cards, load_segment_members, match_grade,
)

logger = structlog.get_logger()

MATCH_NAMES = {GRADE_EXACT: "exact", GRADE_FORM: "name_form", GRADE_CORE: "core_or_card",
               GRADE_PAREN: "parenthetical", GRADE_TOKENS: "loose_words"}

SOURCE_REQUEST = "request"
SOURCE_COMPUTED = "computed"


def _countries(values: Iterable[Optional[str]]) -> list[str]:
    """Distinct known countries, display spelling kept (first seen), compared accent/case-folded."""
    out: dict[str, str] = {}
    for v in values:
        key = fold(v)
        if key and key not in out:
            out[key] = str(v).strip()
    return sorted(out.values(), key=fold)


def seed_check(
    subject_place: Optional[str], seed: Optional[Member], *,
    segment_countries: Iterable[Optional[str]] = (), route: bool = False,
    alias_cards: Optional[Mapping[tuple[str, str], str]] = None,
) -> dict[str, Any]:
    """The mismatch part of one row's `seed` object (pure).

    `place_mismatch`: a Segment Subject with a place, a seed with a place, and
    `match_grade(subject_place, seed)` is None. `country_mismatch`: the seed's country is known, the
    Segment's (Route's) countries are known, and they are not exactly that one country."""
    countries = _countries(segment_countries)
    out: dict[str, Any] = {"segment_countries": countries, "match": None,
                           "place_mismatch": False, "country_mismatch": False}
    if seed is None:
        out["mismatch"] = False
        return out
    if not route and (subject_place or "").strip() and (seed.place or "").strip():
        grade = match_grade(subject_place, seed, alias_cards)
        out["match"] = MATCH_NAMES.get(grade) if grade is not None else None
        out["place_mismatch"] = grade is None
    seed_country = fold(seed.country)
    if seed_country and countries:
        out["country_mismatch"] = {fold(c) for c in countries} != {seed_country}
    out["mismatch"] = out["place_mismatch"] or out["country_mismatch"]
    return out


def _seed_object(*, seed: Optional[Member], source: Optional[str], skip_reason: Optional[str],
                 kind: Optional[str], segment_id: Optional[str], label: Optional[str],
                 subject_place: Optional[str], countries: Iterable[Optional[str]],
                 alias_cards: Optional[Mapping[tuple[str, str], str]]) -> dict[str, Any]:
    return {
        "atom_id": seed.atom_id if seed else None,
        "place": seed.place if seed else None,
        "country": seed.country if seed else None,
        "source": source if seed else None,
        "skip_reason": None if seed else skip_reason,
        "subject_kind": kind,  # "segment" | "route" | None (a request with no Subject)
        "segment_id": segment_id,
        "segment_label": label,
        "subject_place": subject_place,
        **seed_check(subject_place, seed, segment_countries=countries, route=kind == "route",
                     alias_cards=alias_cards),
    }


def _label(place: Optional[str], action: Optional[str]) -> Optional[str]:
    return " — ".join(p for p in (place, action) if p) or None


# ── T9 pieces (cross-tenant) ───────────────────────────────────────────────────────────────────

# The frozen seed (angle_gate_request.atom_id, from tour_atoms — not the active view, so a seed whose
# tour later left the active set still shows its place), plus the piece's Subject and Route.
_PIECE_SEEDS_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, agr.atom_id,
           ta.tour_id::text AS seed_tour_id, ta.place AS seed_place, ta.evidence AS seed_evidence,
           rt.country AS seed_country, apc.card_id::text AS seed_card_id,
           s.segment_id, s.route_id, r.hub_name, rr.country AS route_country
    FROM acp_shared.content_piece cp
    JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
    LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = agr.atom_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = agr.atom_id
    LEFT JOIN acp_shared.subject s ON s.subject_id = agr.subject_id
    LEFT JOIN acp_contract.route r ON r.route_id = s.route_id
    LEFT JOIN silver_aa_internal.raw_tours rr ON rr.tour_id = r.tour_id
    WHERE cp.piece_id = ANY($1::uuid[])
"""

# Each (live) Segment's label and its members' countries per owner_scope; the caller keeps
# 'platform' + the row's own tenant (the scope `load_segment_members` reads).
_SEGMENT_FACTS_SQL = """
    SELECT asg.segment_id, asg.canonical_place, asg.canonical_action, c.owner_scope, c.countries
    FROM acp_contract.atom_segment asg
    LEFT JOIN LATERAL (
        SELECT ta.owner_scope,
               array_agg(DISTINCT rt.country) FILTER (WHERE rt.country IS NOT NULL) AS countries
        FROM acp_contract.atom_segment_member m
        JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
        LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
        WHERE m.segment_id = asg.segment_id AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
        GROUP BY ta.owner_scope
    ) c ON true
    WHERE asg.segment_id = ANY($1::text[])
"""


def _member_from(row: Mapping[str, Any]) -> Optional[Member]:
    if not row.get("atom_id") or row.get("seed_tour_id") is None:
        return None  # the atom row is gone: nothing to show but the id
    return Member(atom_id=row["atom_id"], tour_id=row["seed_tour_id"], place=row.get("seed_place"),
                  country=row.get("seed_country"), evidence=row.get("seed_evidence"),
                  card_id=row.get("seed_card_id"))


async def load_piece_seeds(conn, piece_ids: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """{piece_id: seed object} for the given pieces — 4 set-based reads whatever their number (seed
    rows, Segment alias chain, Segment facts, place-card aliases). A piece whose request has no atom
    row left gets a seed object with `atom_id` only. Fail-soft: {} on any read error."""
    ids = sorted({str(p) for p in piece_ids if p})
    if not ids:
        return {}
    try:
        rows = [dict(r) for r in await conn.fetch(_PIECE_SEEDS_SQL, ids)]
        live = await resolve_segment_aliases(conn, [r["segment_id"] for r in rows])
        facts: dict[str, dict[str, Any]] = {}
        if live:
            for f in await conn.fetch(_SEGMENT_FACTS_SQL, sorted(set(live.values()))):
                entry = facts.setdefault(f["segment_id"], {
                    "label": _label(f["canonical_place"], f["canonical_action"]),
                    "place": f["canonical_place"], "by_scope": {}})
                if f["owner_scope"] is not None:
                    entry["by_scope"][f["owner_scope"]] = list(f["countries"] or [])
        alias_cards = await load_alias_cards(
            conn, [e["place"] for e in facts.values()] + [r.get("seed_place") for r in rows])
    except Exception as exc:  # an admin aid: never the reason a quality page fails to load
        logger.warning("seed_audit_pieces_failed", error=str(exc)[:300], pieces=len(ids))
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        seed = _member_from(r)
        segment_id = live.get(r["segment_id"], r["segment_id"]) if r["segment_id"] else None
        if segment_id:
            f = facts.get(segment_id, {"label": None, "place": None, "by_scope": {}})
            countries = [c for scope in ("platform", r["tenant_id"]) for c in f["by_scope"].get(scope, [])]
            obj = _seed_object(seed=seed, source=SOURCE_REQUEST, skip_reason=None, kind="segment",
                               segment_id=segment_id, label=f["label"], subject_place=f["place"],
                               countries=countries, alias_cards=alias_cards)
        elif r["route_id"]:
            obj = _seed_object(seed=seed, source=SOURCE_REQUEST, skip_reason=None, kind="route",
                               segment_id=None, label=r["hub_name"], subject_place=None,
                               countries=[r["route_country"]], alias_cards=alias_cards)
        else:
            obj = _seed_object(seed=seed, source=SOURCE_REQUEST, skip_reason=None, kind=None,
                               segment_id=None, label=None, subject_place=None, countries=(),
                               alias_cards=alias_cards)
        if seed is None:
            obj["atom_id"] = r["atom_id"]
            obj["skip_reason"] = "seed_atom_missing" if r["atom_id"] else None
        out[r["piece_id"]] = obj
    return out


# ── Subjects of one tenant (the admin Slate tab) ──────────────────────────────────────────────

# The latest request per Subject (its frozen seed once picked).
_SUBJECT_REQUEST_SEEDS_SQL = """
    SELECT DISTINCT ON (agr.subject_id) agr.subject_id::text AS subject_id, agr.atom_id,
           ta.tour_id::text AS seed_tour_id, ta.place AS seed_place, ta.evidence AS seed_evidence,
           rt.country AS seed_country, apc.card_id::text AS seed_card_id
    FROM acp_shared.angle_gate_request agr
    LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = agr.atom_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = agr.atom_id
    WHERE agr.subject_id = ANY($1::uuid[]) AND agr.tenant_id = $2::uuid
    ORDER BY agr.subject_id, agr.created_at DESC
"""


async def load_subject_seeds(conn, tenant_id: str, subjects: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """{subject_id: seed object} for one tenant's Subjects. Each subject mapping carries
    `subject_id`, `segment_id` (the LIVE id), `canonical_place`, `canonical_action`, `route_id`,
    `hub_name`, `route_tour_id`, `route_first_segment_id`, `route_first_place`, `route_country`.
    4 set-based reads whatever the count (request seeds, the tenant's tours via
    `slate._tenant_tour_ids`, Segment members, place-card aliases). Fail-soft: {} on any read error."""
    subjects = [s for s in subjects if s.get("subject_id")]
    if not subjects:
        return {}
    try:
        frozen = {r["subject_id"]: dict(r) for r in await conn.fetch(
            _SUBJECT_REQUEST_SEEDS_SQL, [str(s["subject_id"]) for s in subjects], str(tenant_id))}
        tour_ids = {str(t) for t in await _tenant_tour_ids(str(tenant_id), conn)}
        segment_ids = sorted({s["segment_id"] for s in subjects if s.get("segment_id")}
                             | {s["route_first_segment_id"] for s in subjects if s.get("route_first_segment_id")})
        members = await load_segment_members(conn, segment_ids, str(tenant_id)) if segment_ids else {}
        alias_cards = await load_alias_cards(
            conn, [s.get("canonical_place") for s in subjects]
            + [m.place for ms in members.values() for m in ms]
            + [r.get("seed_place") for r in frozen.values()])
    except Exception as exc:  # an admin aid: never the reason the Slate tab fails to load
        logger.warning("seed_audit_subjects_failed", error=str(exc)[:300], subjects=len(subjects))
        return {}
    out: dict[str, dict[str, Any]] = {}
    for s in subjects:
        sid = str(s["subject_id"])
        req = frozen.get(sid)
        if s.get("segment_id"):
            seg_members = members.get(s["segment_id"], [])
            kind, place, label = "segment", s.get("canonical_place"), _label(
                s.get("canonical_place"), s.get("canonical_action"))
            countries = [m.country for m in seg_members]
        elif s.get("route_id"):
            seg_members = members.get(s.get("route_first_segment_id") or "", [])
            kind, place, label = "route", None, s.get("hub_name")
            countries = [s.get("route_country")]
        else:
            continue
        if req is not None:
            seed, source, skip = _member_from(req), SOURCE_REQUEST, None
            if seed is None:
                skip = "seed_atom_missing" if req.get("atom_id") else None
        elif kind == "segment":
            seed, skip = choose_representative(place, seg_members, tenant_tour_ids=tour_ids,
                                               alias_cards=alias_cards)
            source = SOURCE_COMPUTED
        else:
            route_tour = str(s.get("route_tour_id") or "")
            seed = choose_route_anchor(s.get("route_first_place"), seg_members, route_tour) if route_tour else None
            source, skip = SOURCE_COMPUTED, (None if seed else "no_member_on_route_tour")
        obj = _seed_object(seed=seed, source=source, skip_reason=skip, kind=kind,
                           segment_id=s.get("segment_id"), label=label, subject_place=place,
                           countries=countries, alias_cards=alias_cards)
        if seed is None and req is not None:
            obj["atom_id"] = req.get("atom_id")
        out[sid] = obj
    return out


__all__ = ["seed_check", "load_piece_seeds", "load_subject_seeds", "MATCH_NAMES",
           "SOURCE_REQUEST", "SOURCE_COMPUTED"]

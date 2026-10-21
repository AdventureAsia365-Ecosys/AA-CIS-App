"""AA-847 — admin fixes for the content-validity findings (POST /admin/places/validity/fix). Dry run by default.

  not_a_pin        area features stored as points (V3 area_feature) → geo_precision 'area' (drawn as an area)
  linear_feature   linear features (V3 linear_feature) → geo_precision 'area', review_reason 'linear_feature',
                   geo_status 'suspect': no catalog view draws them (proposed answer to Nghiệp's display decision,
                   LINEAR_FEATURES_HIDDEN); the name stays in the tour's day text
  duplicate_place  near duplicates (V2 near_duplicate), merged catalog-wide per country: one keeper per cluster
                   (geoapify point, then most live components, then id); the others get duplicate_of = keeper and
                   every CIS row pointing at them is repointed to the keeper: geo.tour_place, geo.tour_day
                   (overnight), geo.place_name (so the store lookup answers the keeper, not a new geocode),
                   acp_shared.place_card, shared.place_photo. Booking and TripPlanner rows are reported, never
                   written (other owners' schemas; cis_app may not read them, then the report says so).

Never touches an admin-fixed row (geo_status or located_by 'manual'). Apply writes one acp_shared.audit_log row
(action places.validity_fix.<check>) whose details hold every prior value and every repointed row, written last in
the transaction; `undo=<audit id>` restores exactly those rows if they still hold the fixed values (else 409), and
writes its own audit row. Apply and undo enqueue a debounced tour_graph_build (stops / legs through a merged
destination are missing until it lands).

Apply re-plans inside its transaction. The dry run returns a `fingerprint` of its planned changes; an apply that
sends it back is refused (409, nothing written) when the plan it would apply now differs from what the admin saw.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from services.places import validity

CHECKS = ("not_a_pin", "linear_feature", "duplicate_place")
ACTION_PREFIX = "places.validity_fix."
UNDO_ACTION = "places.validity_fix.undo"
RESOURCE_TYPE = "places_validity"
LINEAR_FEATURES_HIDDEN = True

CROSS_APP_TABLES = ("booking.hotels", "booking.services", "booking.suppliers", "tripplanner.itinerary_components")
# (table, key columns, destination column) of the CIS rows a merge repoints
REPOINT = (
    ("geo.tour_place", ("component_id",), "destination_id"),
    ("geo.tour_day", ("source_id", "day_index"), "overnight_destination_id"),
    ("geo.place_name", ("name_norm", "country_norm", "locality_norm"), "destination_id"),
    ("acp_shared.place_card", ("card_id",), "destination_id"),
    ("shared.place_photo", ("id",), "destination_id"),
)


class FixError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(status, detail)
        self.status, self.detail = status, detail


def _manual(d: dict) -> bool:
    return d.get("geo_status") == "manual" or d.get("located_by") == "manual"


async def _destinations(conn, ids) -> dict[str, dict]:
    rows = await conn.fetch(
        """SELECT id::text AS id, name, country, lat, lng, located_by, geo_status, geo_precision, review_reason,
                  duplicate_of::text AS duplicate_of
             FROM shared.destinations WHERE id = ANY($1::uuid[])""", sorted(set(ids)))
    return {r["id"]: dict(r) for r in rows}


def _selected(items: list, check: str, source_id: Optional[str]) -> list:
    reason = {"not_a_pin": "area_feature", "linear_feature": "linear_feature", "duplicate_place": "near_duplicate"}
    return [i for i in items if i.reason == reason[check] and (source_id is None or i.source_id == source_id)]


def merge_groups(items: list, comp_count: dict[str, int], dests: dict[str, dict]) -> list[list[str]]:
    """Destination clusters across the selected near_duplicate findings (several tours may name the same pair):
    union by shared destination, keeper by validity.keeper_of. [keeper, other, …] per group."""
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for it in items:
        ids = [d for d in it.destination_ids if d in dests]
        for d in ids[1:]:
            parent[find(d)] = find(ids[0])
        for d in ids:
            find(d)
    groups: dict[str, list[str]] = {}
    for d in parent:
        groups.setdefault(find(d), []).append(d)
    out = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        places = [validity.Place(i, dests[i]["name"], dests[i]["country"], float(dests[i]["lat"]),
                                 float(dests[i]["lng"]), dests[i]["located_by"], dests[i]["geo_precision"],
                                 dests[i]["review_reason"], None, comp_count.get(i, 0)) for i in ids]
        k = validity.keeper_of(places)
        # group-level rule: every member must be the same place as the keeper (no chain through a third)
        others = [p.destination_id for p in places if p is not k and validity.is_same_place(k, p)[0]]
        if others:
            out.append([k.destination_id] + sorted(others))
    return sorted(out)


async def _cross_app(conn, ids: list[str]) -> dict:
    out = {}
    for table in CROSS_APP_TABLES:
        try:
            async with conn.transaction():
                out[table] = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE destination_id = ANY($1::uuid[])", ids)
        except Exception as e:  # noqa: BLE001 — another owner's schema: report, never fail the fix
            out[table] = f"not readable as the CIS runtime ({type(e).__name__}); see the AA-847 probe xref"
    return out


def fingerprint(changes: list) -> str:
    """A stable digest of planned changes (ids, before / after, keeper, repointed rows), for the dry-run / apply
    check."""
    return hashlib.sha256(json.dumps(changes, sort_keys=True, default=str).encode()).hexdigest()[:24]


def check_fingerprint(fix: dict, expected: Optional[str]) -> None:
    """409 when the admin's dry run planned something else than what apply would write now."""
    if expected and expected != fix["fingerprint"]:
        raise FixError(409, {"error": "the data changed since the dry run; run the dry run again",
                             "expected": expected, "now": fix["fingerprint"], "changes_now": len(fix["changes"])})


async def plan(conn, check: str, *, country: Optional[str] = None, source_id: Optional[str] = None) -> dict:
    if check not in CHECKS:
        raise FixError(422, f"check must be one of {CHECKS}")
    tours, comps = await validity.load(conn, country)
    items = _selected(validity.evaluate(tours, comps), check, source_id)
    ids = sorted({d for it in items for d in it.destination_ids})
    dests = await _destinations(conn, ids)
    changes, skipped = [], []
    if check in ("not_a_pin", "linear_feature"):
        for d in ids:
            row = dests.get(d)
            if row is None:
                continue
            if _manual(row):
                skipped.append({"destination_id": d, "name": row["name"], "why": "admin-fixed (manual)"})
                continue
            before = {"geo_precision": row["geo_precision"], "geo_status": row["geo_status"],
                      "review_reason": row["review_reason"]}
            after = dict(before, geo_precision="area")
            if check == "linear_feature":
                after["review_reason"] = "linear_feature"
                if LINEAR_FEATURES_HIDDEN:
                    after["geo_status"] = "suspect"
            if after != before:
                changes.append({"destination_id": d, "name": row["name"], "country": row["country"],
                                "before": before, "after": after})
        return {"check": check, "country": country, "source_id": source_id, "findings": len(items),
                "changes": changes, "skipped": skipped, "fingerprint": fingerprint(changes)}
    # duplicate_place
    comp_count: dict[str, int] = {}
    for c in comps:
        comp_count[str(c["destination_id"])] = comp_count.get(str(c["destination_id"]), 0) + 1
    usable = {i: r for i, r in dests.items() if r["duplicate_of"] is None and r["geo_status"] != "suspect"}
    for group in merge_groups(items, comp_count, usable):
        keeper, others = group[0], group[1:]
        for d in others:
            row = usable[d]
            if _manual(row):
                skipped.append({"destination_id": d, "name": row["name"], "why": "admin-fixed (manual)"})
                continue
            moved = {}
            for table, keys, col in REPOINT:
                rows = await conn.fetch(
                    f"SELECT {', '.join(f'{k}::text AS {k}' for k in keys)} FROM {table} WHERE {col} = $1::uuid",
                    d)
                moved[table] = [[r[k] for k in keys] for r in rows]
            changes.append({"destination_id": d, "name": row["name"], "country": row["country"],
                            "keeper_id": keeper, "keeper_name": usable[keeper]["name"],
                            "before": {"duplicate_of": None}, "after": {"duplicate_of": keeper},
                            "repointed": moved})
    merged = [c["destination_id"] for c in changes]
    return {"check": check, "country": country, "source_id": source_id, "findings": len(items),
            "changes": changes, "skipped": skipped, "fingerprint": fingerprint(changes),
            "cross_app_references": await _cross_app(conn, merged) if merged else {}}


def _where(keys: tuple, start: int) -> str:
    return " AND ".join(f"{k}::text = ${start + i}" for i, k in enumerate(keys))


async def _apply_changes(conn, check: str, changes: list, *, reverse: bool = False) -> None:
    for ch in changes:
        target = ch["before"] if reverse else ch["after"]
        if check == "duplicate_place":
            keeper, dup = ch["keeper_id"], ch["destination_id"]
            src, dst = (keeper, dup) if reverse else (dup, keeper)
            for table, keys, col in REPOINT:
                for key in ch["repointed"].get(table, []):
                    await conn.execute(
                        f"UPDATE {table} SET {col} = $1::uuid WHERE {col} = $2::uuid AND {_where(keys, 3)}",
                        dst, src, *key)
            await conn.execute("UPDATE shared.destinations SET duplicate_of = $2::uuid WHERE id = $1::uuid",
                               dup, target["duplicate_of"])
        else:
            await conn.execute(
                """UPDATE shared.destinations SET geo_precision = $2, geo_status = $3, review_reason = $4
                    WHERE id = $1::uuid""",
                ch["destination_id"], target["geo_precision"], target["geo_status"], target["review_reason"])


async def apply(conn, fix: dict, *, actor: str) -> dict:
    """Applies a plan() result in the caller's transaction; the audit row is written last."""
    from services.acp_shared.audit_log import write_audit_log
    from services.places.enqueue import GRAPH_KIND, enqueue_debounced

    if not fix["changes"]:
        return {**fix, "applied": 0}
    await _apply_changes(conn, fix["check"], fix["changes"])
    job_id, _ = await enqueue_debounced(conn, GRAPH_KIND, reason=f"validity_fix:{fix['check']}", created_by=actor)
    audit_id = await _audit(conn, write_audit_log, actor, ACTION_PREFIX + fix["check"],
                            f"{fix['check']}:{fix.get('country') or '*'}", fix)
    return {**fix, "applied": len(fix["changes"]), "audit_id": audit_id, "graph_job": job_id}


async def _audit(conn, writer, actor: str, action: str, resource_id: str, details: dict) -> str:
    await writer(conn, tenant_id=None, actor=actor, action=action, resource_type=RESOURCE_TYPE,
                 resource_id=resource_id, details=json.loads(json.dumps(details, default=str)))
    return await conn.fetchval(
        "SELECT id::text FROM acp_shared.audit_log WHERE action = $1 AND resource_id = $2 AND actor = $3 "
        "ORDER BY created_at DESC, id DESC LIMIT 1", action, resource_id, actor)


async def undo(conn, audit_id: str, *, actor: str, dry_run: bool = True) -> dict:
    """Restores the rows a fix changed, if they still hold the fixed values (else 409, nothing written)."""
    from services.acp_shared.audit_log import write_audit_log
    from services.places.enqueue import GRAPH_KIND, enqueue_debounced

    row = await conn.fetchrow("SELECT action, details FROM acp_shared.audit_log WHERE id = $1::uuid", audit_id)
    if row is None or not row["action"].startswith(ACTION_PREFIX) or row["action"] == UNDO_ACTION:
        raise FixError(404, "no places validity fix with this audit id")
    done = await conn.fetchval(
        "SELECT count(*) FROM acp_shared.audit_log WHERE action = $1 AND details ->> 'undo_of' = $2",
        UNDO_ACTION, audit_id)
    if done:
        raise FixError(409, "this fix was already undone")
    details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"])
    check, changes = details["check"], details.get("changes") or []
    current = await _destinations(conn, [c["destination_id"] for c in changes])
    drift = []
    for ch in changes:
        cur = current.get(ch["destination_id"]) or {}
        for k, v in ch["after"].items():
            if (cur.get(k) or None) != (v or None):
                drift.append({"destination_id": ch["destination_id"], "field": k, "expected": v, "now": cur.get(k)})
    if drift:
        raise FixError(409, {"error": "rows changed since the fix; undo by hand", "drift": drift})
    out = {"undo_of": audit_id, "check": check, "restored": len(changes), "dry_run": dry_run}
    if dry_run:
        return out
    await _apply_changes(conn, check, changes, reverse=True)
    job_id, _ = await enqueue_debounced(conn, GRAPH_KIND, reason=f"validity_fix_undo:{check}", created_by=actor)
    out["graph_job"] = job_id
    out["audit_id"] = await _audit(conn, write_audit_log, actor, UNDO_ACTION, audit_id,
                                   {"undo_of": audit_id, "check": check, "changes": changes})
    return out

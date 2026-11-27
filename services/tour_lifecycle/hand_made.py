"""AA-1181: a supplier sheet never takes the place of a tour made by hand in the content workspace.

A hand-made tour is a source version with ``raw_tours.entry_origin = 'manual'`` (CIS 478, AA-1148): its facts live in
``manual_facts`` and change only in the workspace (draft, required list, two-person publish). An Excel upload can still
stage a row against one (the match is tenant + name + provider), so the lifecycle service refuses, with 409
``source_hand_made``:

- ``supersede`` replace / update / keep_both on a key with a hand-made version (replace would make the sheet row the
  live version; keep_both would add it as a version that set-active could later make live). Bypass and Create a new
  tour stay allowed;
- ``set_active`` of a version that is not hand-made on a key that has one.

The origin is read through ``to_jsonb`` (as services/content_workspace/store._ORIGIN_SQL): before CIS 478 a key has no
hand-made version instead of an error. The fact-draft merge, merge-preview and publish refuse on their own (409
``fact_draft_hand_made``, api/routers/admin_pipeline/fact_drafts.py) with :func:`key_is_hand_made`: the same per-key
rule, so a key whose live version came from a sheet but which holds a hand-made version (left by a Replace before this
rule) is refused too.
"""
from __future__ import annotations

from typing import Optional

SOURCE_HAND_MADE = "source_hand_made"
HAND_MADE_VERSIONS_SQL = """
    SELECT rt.tour_id::text AS tour_id FROM silver_aa_internal.raw_tours rt
     WHERE (COALESCE(rt.source_group_id, rt.tour_id))::text = $1 AND to_jsonb(rt) ->> 'entry_origin' = 'manual'
"""
_SHEET = ("this tour was made by hand in the content workspace: a supplier sheet never replaces it or joins its "
          "versions. Choose Bypass, or Create a new tour to keep the sheet's tour as a separate tour.")
_SET_ACTIVE = ("this tour was made by hand in the content workspace: an older sheet version never becomes its live "
               "version. Change the tour in the content workspace.")


async def hand_made_versions(conn, source_id: Optional[str]) -> set[str]:
    """The tour ids of the key's hand-made versions (empty for a sheet key, no key or before CIS 478)."""
    if not source_id:
        return set()
    return {r["tour_id"] for r in await conn.fetch(HAND_MADE_VERSIONS_SQL, source_id)}


async def key_is_hand_made(conn, tour_id: Optional[str]) -> bool:
    """True when the tour's key has a hand-made version (any version, not only this row); False for a sheet key, an
    unknown id or before CIS 478. The fact-draft merge, merge-preview, publish and the drawer's ``hand_made`` use it."""
    from .service import key_of     # service imports this module
    return bool(await hand_made_versions(conn, await key_of(conn, tour_id)))


async def refuse_sheet(conn, source_id: Optional[str], *, decision: str = "",
                       live_tour_id: Optional[str] = None) -> None:
    """409 ``source_hand_made`` (LifecycleError) when the key has a hand-made version: for a staged ``decision`` other
    than create_new (supersede), or when ``live_tour_id`` (set_active) is not one of them."""
    if decision == "create_new":
        return
    manual = await hand_made_versions(conn, source_id)
    if not manual or (live_tour_id is not None and live_tour_id in manual):
        return
    from .service import LifecycleError     # service imports this module
    raise LifecycleError(409, SOURCE_HAND_MADE, _SET_ACTIVE if live_tour_id is not None else _SHEET)

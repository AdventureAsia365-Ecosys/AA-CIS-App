"""AA-1144 (S285 decision D8) — an agency's hand edit of its T2 copy keeps the tour facts.

- services/content_generation/t2_fact_lock: wording is free; a new figure, another day structure, a new place in the
  highlights or itinerary, a dropped / added "not included" item group and a non-wording key are locked; an edit is
  laid over the version it edits; the audit view holds hashes, never text.
- PATCH /v1/tours/versions/{id} (action=edit): 422 fact_locked with the locked list and nothing inserted; a saved edit
  stores the merged content with the member from the token as editor (never body.edited_by); one audit row per edit
  and per refusal on the main pool, and a failing audit write never undoes the edit.
Invented tour (Lumora Valley); no text from the DB or prompts.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import tests.unit.test_aa817_fail_closed_routes as fc
from api.routers import v1_tours
from services.acp_shared.audit_log import TenantAuditAction
from services.content_generation import t2_fact_lock as lock

ITINERARY = ("Day 1: Arrive in Kestrel Town, walk 12 km to Tessaly Lodge.\n"
             "Day 2: Climb Brinmoor Ridge (5 hours).\n"
             "Day 3: Descend to Orrin Lake.\n"
             "Day 4: Transfer back to Kestrel Town by 14:00.")
PARENT = {
    "name": "Lumora Valley Trek: 4 Days",
    "subtitle": "Ridges and lakes",
    "summary": "A four-day walk through Lumora Valley. International flights are not included.",
    "highlights": ["Sunrise over Brinmoor Ridge", "Night at Tessaly Lodge"],
    "itineraries": ITINERARY,
    "seo_title": "Lumora Valley Trek",
    "seo_meta": "Walk Lumora Valley in 4 days.",
    "trip_type": "",
    "status": "done",
}
BASE = ["Lumora Valley Trek", "4 days 3 nights", "USD 450 per person", "Accommodation, guide"]
EXCLUSIONS = "International flights\nVisa"


def _check(edited, parent=PARENT, base=BASE, master_itinerary=""):
    new, locked = lock.check_edit(parent, edited, base + [EXCLUSIONS], master_itinerary, EXCLUSIONS)
    return new, [(x.field, x.fact, x.value) for x in locked]


# ── the pure check ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("edited", [
    {"summary": "Walk the quiet Lumora Valley with us. International flights are not included."},
    {"summary": "Lumora Valley over 4 days, from USD 450. International flights are not included."},
    {"itineraries": ("Day 1: Arrive in Kestrel Town and walk to Tessaly Lodge.\nDay 2: Climb Brinmoor Ridge.\n"
                     "Day 3: Down to Orrin Lake.\nDay 4: Back to Kestrel Town.")},
    {"highlights": ["Watch the sunrise over Brinmoor Ridge"]},
    {"name": "Lumora Valley Trek: 4 Days", "status": "done"},          # an unchanged non-wording key is fine
    {"seo_meta": "Wanderbright Journeys: walk Lumora Valley in 4 days."},  # a brand word outside the place fields
], ids=["wording", "known-figures", "itinerary-less-detail", "highlights-reworded", "same-key", "brand-in-seo"])
def test_wording_edits_pass(edited):
    _new, locked = _check(edited)
    assert locked == []


@pytest.mark.parametrize("edited,expected", [
    ({"summary": "A 3 day walk through Lumora Valley. International flights are not included."},
     ("summary", "figure", "3 day")),
    ({"itineraries": ITINERARY.replace("12 km", "15 km")}, ("itineraries", "figure", "15 km")),
    ({"summary": "From USD 390. International flights are not included."}, ("summary", "figure", "390")),
    ({"itineraries": ITINERARY.replace("Day 4", "Day 4: Rest.\nDay 5")}, ("itineraries", "days", "Day 5")),
    ({"itineraries": ITINERARY.split("\nDay 4")[0]}, ("itineraries", "days", "Day 4")),
    ({"highlights": ["Sunrise over Brinmoor Ridge", "Night at Marrowind Castle"]},
     ("highlights", "place", "Marrowind Castle")),
    ({"summary": "A four-day walk through Lumora Valley."}, ("content", "not_included", "flights")),
    ({"summary": "A four-day walk. Flights and tips are not included."}, ("content", "not_included", "tips")),
    ({"duration": "3 days"}, ("duration", "field", "3 days")),
    ({"inclusions": "Everything"}, ("inclusions", "field", "Everything")),
], ids=["days-in-summary", "distance", "price", "extra-day", "dropped-day", "new-place", "dropped-exclusion",
        "added-exclusion", "duration-key", "inclusions-key"])
def test_fact_changes_are_locked(edited, expected):
    _new, locked = _check(edited)
    assert expected in locked, locked


def test_an_exclusion_the_source_states_may_be_added():
    _new, locked = _check({"summary": "A four-day walk. Flights and visa are not included."})
    assert locked == []


@pytest.mark.parametrize("summary,expected", [
    ("A two-day walk through Lumora Valley.", ("summary", "figure", "two-day")),
    ("Two days walking through Lumora Valley.", ("summary", "figure", "Two days")),
    ("Five nights under the stars in Lumora Valley.", ("summary", "figure", "Five nights")),
    ("A walk of twelve hours a day.", ("summary", "figure", "twelve hours")),
], ids=["two-day", "two-days", "five-nights", "twelve-hours"])
def test_a_spelled_out_duration_is_a_figure(summary, expected):
    _new, locked = _check({"summary": summary + " International flights are not included."})
    assert expected in locked, locked


@pytest.mark.parametrize("summary", [
    "Four days in Lumora Valley.", "Three nights in Lumora Valley lodges.", "A four-day trek, three nights out.",
    "One day you will walk Lumora Valley.",
], ids=["four-days", "three-nights", "both", "one-idiom"])
def test_a_spelled_out_duration_the_base_states_passes(summary):
    _new, locked = _check({"summary": summary + " International flights are not included."})
    assert locked == [], locked


@pytest.mark.parametrize("edited,expected", [
    ({"summary": "Price includes international flights and all meals. Flights are not included."},
     ("content", "included", "flights")),
    ({"highlights": ["Visa included", "Sunrise over Brinmoor Ridge"]}, ("content", "included", "visa")),
    ({"summary": "A four-day walk; travel insurance is included. International flights are not included."},
     ("content", "included", "insurance")),
    ({"highlights": ["All meals included", "Sunrise over Brinmoor Ridge"]}, ("content", "included", "meals")),
], ids=["flights-excluded", "visa-excluded", "insurance-never-named", "meals-never-named"])
def test_a_new_inclusion_claim_against_the_base_is_locked(edited, expected):
    _new, locked = _check(edited)
    assert expected in locked, locked


@pytest.mark.parametrize("edited", [
    {"highlights": ["Guide included all the way", "Sunrise over Brinmoor Ridge"]},
    {"summary": "Accommodation is included, but international flights are not included."},
    {"summary": "Everything is covered except the visa. International flights are not included."},
], ids=["guide-in-source", "accommodation-but-flights", "except-clause"])
def test_an_inclusion_claim_the_base_supports_passes(edited):
    _new, locked = _check(edited)
    assert locked == [], locked


def test_meals_included_is_locked_when_the_source_excludes_a_meal():
    new, locked = lock.check_edit(PARENT, {"highlights": ["Meals included", "Night at Tessaly Lodge"]},
                                  BASE + ["Breakfast at the lodge", "Dinner"], "", "Dinner")
    assert ("content", "included", "meals") in [(x.field, x.fact, x.value) for x in locked]


def test_an_inclusion_claim_the_version_already_makes_is_kept():
    parent = {**PARENT, "subtitle": "Ridges and lakes, visa included"}
    _new, locked = _check({"summary": "Walk Lumora Valley. International flights are not included."}, parent=parent)
    assert locked == []


def test_a_place_slipped_into_the_summary_never_clears_the_itinerary():
    first_new, first = _check({"summary": "A four-day walk to Halvaris Bay. International flights are not included."})
    assert first == []                                  # the summary is free wording
    itinerary = ITINERARY.replace("Day 3: Descend to Orrin Lake.", "Day 3: Descend to Halvaris Bay.")
    _new, second = _check({"itineraries": itinerary}, parent=first_new)
    assert ("itineraries", "place", "Halvaris Bay") in second, second


def test_a_place_the_master_names_may_move_into_the_itinerary():
    itinerary = ITINERARY.replace("Day 3: Descend to Orrin Lake.", "Day 3: Descend to Orrin Lake and Selwen Falls.")
    _new, locked = _check({"itineraries": itinerary}, base=BASE + ["Selwen Falls is on the way down."])
    assert locked == []


def test_the_master_itinerary_is_the_day_base_when_the_version_has_none():
    parent = {**PARENT, "itineraries": ""}
    _new, locked = _check({"itineraries": "Day 1: Kestrel Town.\nDay 2: Brinmoor Ridge."}, parent=parent,
                          master_itinerary=ITINERARY)
    assert ("itineraries", "days", "Day 3, Day 4") in locked


def test_an_edit_is_laid_over_the_version():
    new, locked = _check({"summary": "Walk Lumora Valley. International flights are not included."})
    assert locked == []
    assert new["itineraries"] == ITINERARY and new["trip_type"] == "" and new["status"] == "done"
    assert new["summary"].startswith("Walk Lumora Valley")


def test_refusal_shape_and_cap():
    many = [lock.Locked("summary", "figure", str(i)) for i in range(30)]
    detail = lock.refusal(many)
    assert detail["code"] == "fact_locked" and detail["message"] == lock.MESSAGE
    assert len(detail["locked"]) == 20 and detail["locked"][0] == {"field": "summary", "fact": "figure", "value": "0"}


def test_audit_changes_hold_hashes_not_text():
    new = {**PARENT, "summary": "Walk Lumora Valley."}
    changes = lock.audit_changes(PARENT, new)
    assert [c["field"] for c in changes] == ["summary"]
    assert len(changes[0]["old_sha256"]) == 64 and changes[0]["old_sha256"] != changes[0]["new_sha256"]
    assert "Lumora" not in json.dumps(changes)
    assert lock.audit_changes(PARENT, {**PARENT, "extra": 1})[0] == {
        "field": "extra", "old_sha256": None, "new_sha256": lock._sha(1)}


@pytest.mark.parametrize("value,expected", [('{"a": 1}', {"a": 1}), ({"a": 1}, {"a": 1}), ("not json", {}),
                                            (None, {}), ("[1]", {})])
def test_content_dict(value, expected):
    assert lock.content_dict(value) == expected


@pytest.mark.asyncio
async def test_check_against_base_reads_master_and_raw():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"aa_itineraries": ITINERARY, "duration": "4 days", "price_raw": "USD 450"})
    _new, locked = await lock.check_against_base(conn, uuid4(), {"summary": "Short."},
                                                 {"summary": "Lumora in 4 days from USD 450."})
    assert locked == []
    assert conn.fetchrow.call_args.args[0] is lock.BASE_SQL
    conn.fetchrow = AsyncMock(return_value=None)                     # no Master row: the version is the only base
    _new, locked = await lock.check_against_base(conn, uuid4(), {"summary": "Short."}, {"summary": "In 4 days."})
    assert locked == [lock.Locked("summary", "figure", "4 days")]


def test_base_sql_reads_master_and_raw_of_one_tour():
    sql = lock.BASE_SQL
    assert "gold_aa_internal.published_tours pt" in sql and "silver_aa_internal.raw_tours rt" in sql
    assert "WHERE pt.id = $1::uuid" in sql


# ── the route ─────────────────────────────────────────────────────────────────

VERSION_ID = str(uuid4())
PUBLISHED_TOUR_ID = uuid4()
MID = str(uuid4())


def _route_conn(base_row=None):
    conn = fc._conn(fetchrow=[{"id": VERSION_ID, "published_tour_id": PUBLISHED_TOUR_ID, "version_number": 2,
                               "rewritten_content": json.dumps(PARENT)}, base_row or {"aa_itineraries": ITINERARY}])
    conn.inserted = []
    new_id = uuid4()

    async def fetchval(sql, *args):
        if "set_config('app.tenant_id'" in sql:
            return args[0]
        if sql is v1_tours._VERSION_NEXT_NUMBER_SQL:
            return 3
        if sql is v1_tours._VERSION_EDIT_INSERT_SQL:
            conn.inserted.append(args)
            return new_id
        raise AssertionError(f"unexpected fetchval {sql!r}")

    conn.fetchval = AsyncMock(side_effect=fetchval)
    return conn


async def _patch(request, content, edited_by="someone-else"):
    body = v1_tours.VersionActionRequest(action="edit", edited_content=content, edited_by=edited_by)
    return await v1_tours.update_version(VERSION_ID, body, request, tenant={"sub": str(fc.TENANT_ID), "mid": MID})


def _audit_rows(request):
    return [c.args for c in request.app.state.pool.execute.call_args_list if "acp_shared.audit_log" in c.args[0]]


@pytest.mark.asyncio
async def test_route_refuses_a_fact_change_and_audits_it():
    conn = _route_conn()
    request = fc._request(flag=True, tenant_conn=conn)
    with pytest.raises(HTTPException) as exc:
        await _patch(request, {"summary": "A 3 day walk. International flights are not included."})
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "fact_locked"
    assert {"field": "summary", "fact": "figure", "value": "3 day"} in exc.value.detail["locked"]
    assert conn.inserted == []
    (row,) = _audit_rows(request)
    assert row[1:6] == (str(fc.TENANT_ID), f"tenant:{fc.TENANT_ID}", TenantAuditAction.TOUR_VERSION_EDIT_REFUSED,
                        "tenant_tour_version", VERSION_ID)
    details = json.loads(row[6])
    assert details["membership_id"] == MID and details["locked"][0]["fact"] == "figure"


@pytest.mark.asyncio
async def test_route_saves_wording_with_the_token_editor_and_audits_hashes():
    conn = _route_conn()
    request = fc._request(flag=True, tenant_conn=conn)
    out = await _patch(request, {"summary": "Walk Lumora Valley. International flights are not included."})
    assert out["status"] == "edited" and out["version_number"] == 3 and "changes" not in out
    (args,) = conn.inserted
    stored = json.loads(args[4])
    assert stored["itineraries"] == ITINERARY and stored["summary"].startswith("Walk Lumora")
    assert args[5] == f"tenant:{fc.TENANT_ID}:{MID}"                   # never body.edited_by
    (row,) = _audit_rows(request)
    assert row[3] == TenantAuditAction.TOUR_VERSION_EDITED
    details = json.loads(row[6])
    assert details["new_version_id"] == out["new_version_id"]
    assert [c["field"] for c in details["changes"]] == ["summary"] and "Lumora" not in row[6]


@pytest.mark.asyncio
async def test_route_keeps_the_edit_when_the_audit_write_fails():
    conn = _route_conn()
    request = fc._request(flag=True, tenant_conn=conn)
    request.app.state.pool.execute = AsyncMock(side_effect=RuntimeError("audit down"))
    out = await _patch(request, {"summary": "Walk Lumora Valley. International flights are not included."})
    assert out["status"] == "edited" and len(conn.inserted) == 1


@pytest.mark.asyncio
async def test_route_status_change_writes_no_edit_audit():
    conn = _route_conn()
    request = fc._request(flag=True, tenant_conn=conn)
    body = v1_tours.VersionActionRequest(action="approve")
    out = await v1_tours.update_version(VERSION_ID, body, request, tenant={"sub": str(fc.TENANT_ID)})
    assert out == {"status": "approved", "version_id": VERSION_ID}
    assert _audit_rows(request) == []


def test_audit_actions():
    assert TenantAuditAction.TOUR_VERSION_EDITED == "tenant_tour_version.edited"
    assert TenantAuditAction.TOUR_VERSION_EDIT_REFUSED == "tenant_tour_version.edit_refused"
    assert all(len(a) <= 64 for a in (TenantAuditAction.TOUR_VERSION_EDITED,
                                      TenantAuditAction.TOUR_VERSION_EDIT_REFUSED))

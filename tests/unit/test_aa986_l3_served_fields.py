"""AA-986 L3 (root ADR 0015 decision 5): the tenant view of a tour's served state, without a database.

- ServedState: the fields every tenant /v1 row carries, the notice the portal shows (Q1 paused, Q2 nothing for a
  supersede), "served" for a key without a state;
- TourNotServedError: 409 code, plain sentence, X-Error-Code header; the router helper and the app's handler;
- the T7 planner skips a tour that is not served, and the plan hash of served trips is unchanged;
- the staff bell scope never reaches tenant-only rows; the A4 action says it does not touch the channel.
"""
import asyncio
import datetime as dt
import inspect
import json
from uuid import uuid4

import pytest

from api.routers import admin, admin_a4
from api.routers._served import refuse_http, tour_not_served_handler
from services.acp_planning.models import Trip, compute_trips_hash
from shared.tour_liveness import (ServedState, TOUR_DISCONTINUED, TOUR_PAUSED, TourNotServedError, UNKNOWN,
                                  with_served)

AT = dt.datetime(2026, 10, 23, 8, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("state,reason,notice,served", [
    ("served", None, None, True),
    (None, None, None, True),
    ("retired", "source_deleted", "discontinued", False),
    ("retired", "master_trashed", "discontinued", False),
    ("paused", "master_inactive", "paused", False),
    ("paused", "superseded_pending", None, False),          # Q2: a supersede is invisible to the tenant
])
def test_notice_and_served(state, reason, notice, served):
    s = ServedState(state, reason, AT)
    assert s.notice == notice and s.served is served


def test_fields_shape():
    assert ServedState("retired", "source_deleted", AT).fields() == {
        "served_status": "retired", "served_reason": "source_deleted",
        "discontinued_at": AT.isoformat(), "served_notice": "discontinued"}
    # a served key has no discontinued_at even when it has a change date
    assert ServedState("served", None, AT).fields()["discontinued_at"] is None
    assert with_served({"x": 1}, None) == {"x": 1, **UNKNOWN.fields()}


def test_error_codes_and_sentences():
    gone = TourNotServedError(ServedState("retired", "source_deleted"), "publish a new post")
    assert gone.code == TOUR_DISCONTINUED and gone.status_code == 409
    assert gone.detail.startswith("tour_discontinued: ") and "publish a new post" in gone.detail
    assert str(gone) == gone.detail
    paused = TourNotServedError(ServedState("paused", "master_inactive"), "start a new rewrite")
    assert paused.code == TOUR_PAUSED and "paused this tour" in paused.detail
    updating = TourNotServedError(ServedState("paused", "superseded_pending"), "write new content")
    assert updating.code == TOUR_PAUSED and "new version" in updating.detail
    http = refuse_http(gone)
    assert (http.status_code, http.detail, http.headers) == (409, gone.detail, {"X-Error-Code": "tour_discontinued"})


def test_app_handler_answers_409_json():
    exc = TourNotServedError(ServedState("retired", "master_trashed"), "choose a goal for this post")
    resp = asyncio.run(tour_not_served_handler(None, exc))
    assert resp.status_code == 409 and resp.headers["x-error-code"] == "tour_discontinued"
    assert json.loads(resp.body)["detail"] == exc.detail


def _trip(state=None, stage="active"):
    return Trip(id=uuid4(), name="T", destination="Laos", period="Jan-Dec", lifecycle_stage=stage,
                served_state=state)


def test_trip_served_property_and_hash():
    served, gone = _trip("served"), _trip("retired")
    assert served.served and _trip(None).served and not gone.served and not _trip("paused").served
    # the hash of served trips is the pre-AA-986 3-tuple hash (existing plans are not marked stale)
    import hashlib
    legacy = hashlib.sha256(json.dumps(sorted([(str(served.id), "Jan-Dec", "active")])).encode()).hexdigest()
    assert compute_trips_hash([served]) == legacy
    assert compute_trips_hash([gone]) != compute_trips_hash([_trip("served")])


def test_staff_bell_scope_excludes_tenant_only_rows():
    assert "target_roles <@ ARRAY['tenant']" in admin.notification_scope(None)
    assert admin.notification_scope("admin").startswith("NOT (")


def test_a4_force_unpublish_says_what_it_does():
    src = inspect.getsource(admin_a4.force_unpublish)
    assert '"channel_post_removed": False' in src and "still live" in src

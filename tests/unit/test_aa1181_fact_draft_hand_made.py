"""AA-1181 slice (a): an Excel fact draft never publishes over a hand-made tour (raw_tours.entry_origin 'manual', the
content workspace of AA-1148), and a supplier sheet never merges into one. The parts that need no database: the origin
probe, the refusal and where it runs in the publish checks and the merge inputs; a sheet tour still goes through.
Invented tour names and places only; the database side is tests/integration/test_aa1181_fact_draft_hand_made_pg.py."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers.admin_pipeline import fact_drafts as fd
from api.routers.admin_pipeline import uploads as up
from services.content_workspace import store as cw
from shared.tour_liveness import SourceState

TOUR = "00000000-0000-4000-8000-00000000a181"


class _OriginConn:
    """A connection whose entry-origin probe answers ``origin`` (None = before CIS 478 or an unknown tour)."""

    def __init__(self, origin):
        self.origin = origin
        self.sql = []

    async def fetchval(self, sql, *args):
        self.sql.append(sql)
        assert "entry_origin" in sql and args == (TOUR,)
        return self.origin


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.mark.parametrize("origin, expected", [("manual", True), ("sheet", False), (None, False)])
def test_is_hand_made_reads_the_entry_origin(origin, expected):
    assert _run(cw.is_hand_made(_OriginConn(origin), TOUR)) is expected


@pytest.mark.parametrize("action, advice", [("publish", "Discard this sheet draft"), ("merge", "Choose Bypass")])
def test_a_hand_made_tour_is_refused_with_its_own_code(action, advice):
    with pytest.raises(HTTPException) as e:
        _run(fd.refuse_hand_made(_OriginConn("manual"), TOUR, action))
    assert e.value.status_code == 409
    assert e.value.detail["code"] == fd.FACT_DRAFT_HAND_MADE == "fact_draft_hand_made"
    assert e.value.headers == {"X-Error-Code": "fact_draft_hand_made"}
    assert "content workspace" in e.value.detail["message"] and advice in e.value.detail["message"]
    assert "AA-" not in e.value.detail["message"]          # user-visible text never names an issue


@pytest.mark.parametrize("origin", ["sheet", None])
def test_a_sheet_tour_is_not_refused(origin):
    assert _run(fd.refuse_hand_made(_OriginConn(origin), TOUR, "publish")) is None


def _draft(fields=("duration",)):
    return {"id": 7, "fields": list(fields), "author": "admin:zz-editor", "before": json.dumps({"duration": "3 days"}),
            "after": json.dumps({"duration": "4 days"})}


def _req():
    return SimpleNamespace(headers={"x-admin-role": "content_lead", "x-admin-user-id": "zz-lead"}, scope={})


def test_the_publish_checks_refuse_a_hand_made_tour_before_the_gate():
    gate = AsyncMock()
    with patch.object(fd, "enforce_publish_gate", gate), \
            patch.object(fd.cw, "is_hand_made", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as e:
            _run(fd._fd_publish_checks(object(), _req(), TOUR, _draft()))
    assert e.value.detail["code"] == "fact_draft_hand_made"
    gate.assert_not_awaited()                              # the refusal does not depend on who publishes


def test_the_publish_checks_of_a_sheet_tour_still_pass():
    """Regression: a normal Excel tour's sheet draft goes through the same checks as before."""
    gate = AsyncMock()
    live = SourceState(tour_id=TOUR, exists=True, source_status="active")
    with patch.object(fd, "enforce_publish_gate", gate), \
            patch.object(fd.cw, "is_hand_made", AsyncMock(return_value=False)), \
            patch.object(fd, "source_state", AsyncMock(return_value=live)), \
            patch.object(fd, "_fd_current", AsyncMock(return_value={"duration": "3 days"})):
        current = _run(fd._fd_publish_checks(object(), _req(), TOUR, _draft()))
    assert current == {"duration": "3 days"}
    gate.assert_awaited_once()


class _MergeConn:
    """The reads of ``_merge_inputs``: the staging row, the origin probe and (a sheet tour) the tour row, history."""

    def __init__(self, origin):
        self.origin = origin

    async def fetchrow(self, sql, *args):
        assert "upload_staging" in sql, "a hand-made tour's row is never read for a merge"
        return {"id": "s1", "matched_tour_id": TOUR,
                "parsed_payload": json.dumps({"src_name": "Quillmere Ridge Walk 3D", "duration": "4 days"})}

    async def fetchval(self, sql, *args):
        assert "entry_origin" in sql
        return self.origin

    async def fetch(self, sql, *args):
        return []


def test_a_merge_into_a_hand_made_tour_is_refused():
    live = SourceState(tour_id=TOUR, exists=True, source_status="active")
    with patch.object(up, "source_state", AsyncMock(return_value=live)):
        with pytest.raises(HTTPException) as e:
            _run(up._merge_inputs(_MergeConn("manual"), "s1"))
    assert e.value.status_code == 409 and e.value.detail["code"] == "fact_draft_hand_made"
    assert "Choose Bypass" in e.value.detail["message"]


def test_a_merge_into_a_sheet_tour_still_plans_the_fields():
    """Regression: the merge of a normal Excel re-import is planned as before."""
    from services.ingestion import excel_merge as em
    live = SourceState(tour_id=TOUR, exists=True, source_status="active")
    current = {"tour_id": TOUR, **{f: None for f in em.MERGE_FIELDS}, "duration": "3 days"}
    conn = _MergeConn("sheet")
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": "s1", "matched_tour_id": TOUR, "parsed_payload": json.dumps({"duration": "4 days"})}, current])
    with patch.object(up, "source_state", AsyncMock(return_value=live)):
        _staging, cur, owned, plan = _run(up._merge_inputs(conn, "s1"))
    assert cur["tour_id"] == TOUR and owned == set()
    assert [a["field"] for a in plan["auto"]] == ["duration"]


# ── the other staging decisions (replace / update / keep_both) and set-active: services/tour_lifecycle ──────────────

GROUP = "00000000-0000-4000-8000-00000000b181"
SHEET_VERSION = "00000000-0000-4000-8000-00000000c181"


class _LifecycleConn:
    """Enough of a connection for the refusals of supersede / set_active: the key, its deleted state, its hand-made
    versions. Any write is a test failure (the refusal comes before every write)."""

    def __init__(self, manual):
        self.manual = manual

    def transaction(self):
        conn = self

        class _Tr:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tr()

    async def fetchval(self, sql, *args):
        assert "COALESCE(rt.source_group_id, rt.tour_id)" in sql
        return GROUP

    async def fetchrow(self, sql, *args):
        if "has_live" in sql:                                   # deleted_key_info: the key is live
            return {"has_live": True, "deleted_at": None, "trashed_tour_id": None}
        assert "FOR UPDATE OF rt" in sql                        # set_active: the version row
        return {"tour_id": args[0] and str(args[0]), "tenant_id": "t", "src_name": "Quillmere Ridge Walk 3D",
                "source_group_id": GROUP, "source_status": "superseded", "deleted_at": None, "source_id": GROUP}

    async def fetch(self, sql, *args):
        assert "entry_origin" in sql and args == (GROUP,)
        return [{"tour_id": t} for t in self.manual]

    async def execute(self, sql, *args):
        raise AssertionError(f"no write on a refused decision: {sql}")


@pytest.mark.parametrize("decision", ["replace", "update", "keep_both"])
def test_a_sheet_row_never_replaces_or_joins_a_hand_made_tour(decision):
    from services.tour_lifecycle import SOURCE_HAND_MADE, LifecycleError, supersede
    with pytest.raises(LifecycleError) as e:
        _run(supersede(_LifecycleConn({TOUR}), decision=decision, tenant_id="t", batch_id="b",
                       payload={"src_name": "Quillmere Ridge Walk 3D"}, matched_tour_id=TOUR, matched_group_id=GROUP))
    assert e.value.status == 409 and e.value.code == SOURCE_HAND_MADE == "source_hand_made"
    assert e.value.detail.startswith("source_hand_made: this tour was made by hand in the content workspace")
    assert "Bypass" in e.value.message and "Create a new tour" in e.value.message and "AA-" not in e.value.message


def test_the_staging_decide_answers_the_hand_made_refusal_as_409():
    from services.tour_lifecycle import SOURCE_HAND_MADE, LifecycleError
    e = up._lifecycle_http(LifecycleError(409, SOURCE_HAND_MADE, "made by hand"))
    assert e.status_code == 409 and e.headers == {"X-Error-Code": "source_hand_made"}


def test_an_older_sheet_version_never_goes_live_over_a_hand_made_one():
    from services.tour_lifecycle import LifecycleError, set_active
    with pytest.raises(LifecycleError) as e:
        _run(set_active(_LifecycleConn({TOUR}), SHEET_VERSION))
    assert e.value.status == 409 and e.value.code == "source_hand_made"


def test_hand_made_versions_is_empty_without_a_key():
    from services.tour_lifecycle import hand_made_versions
    assert _run(hand_made_versions(_LifecycleConn({TOUR}), None)) == set()
    assert _run(hand_made_versions(_LifecycleConn(set()), GROUP)) == set()


def test_create_new_and_the_hand_made_version_itself_pass():
    """Bypass needs no lifecycle call; Create a new tour and setting the hand-made version itself active pass."""
    from services.tour_lifecycle import hand_made
    conn = _LifecycleConn({TOUR})
    assert _run(hand_made.refuse_sheet(conn, GROUP, decision="create_new")) is None
    assert _run(hand_made.refuse_sheet(conn, GROUP, live_tour_id=TOUR)) is None
    assert _run(hand_made.refuse_sheet(_LifecycleConn(set()), GROUP, decision="replace")) is None   # a sheet key

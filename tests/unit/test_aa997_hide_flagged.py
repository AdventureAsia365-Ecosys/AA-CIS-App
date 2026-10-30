"""AA-997 S260 — the `hide_flagged` pass (hide only the rows a relocate run already stamped llm_point / off_route and
left on the model's point), its audit and its reversal (unhide by stamp / by id), and the relocate paths that show a
policy-hidden row again once the geocoder confirms it. Fake connections only: no DB, no call."""
from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.places import geocoder as gc
from services.places import locate


class _Conn:
    """Records every statement; `fetch` answers the canned rows, `execute` answers per-id UPDATE counts."""

    def __init__(self, rows=None, changed=(), current=None):
        self.rows, self.changed = rows or [], set(changed)
        self.sql, self.fetch_args = [], []
        self.current = current

    async def fetch(self, sql, *a):
        self.fetch_args.append((" ".join(sql.split()), a))
        return self.rows

    async def fetchrow(self, sql, *a):
        return self.current

    async def execute(self, sql, *a):
        flat = " ".join(sql.split())
        self.sql.append((flat, a))
        if flat.startswith("UPDATE shared.destinations") and a and a[0] in self.changed:
            return "UPDATE 0"
        return "UPDATE 1" if flat.startswith("UPDATE") else "INSERT 0 1"

    def transaction(self, **kw):
        @contextlib.asynccontextmanager
        async def tx():
            yield
        return tx()

    def of(self, prefix):
        return [a for s, a in self.sql if s.startswith(prefix)]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        @contextlib.asynccontextmanager
        async def acq():
            yield self.conn
        return acq()


def _row(i, country="Nepal", outcome="llm_point", reason="llm_point", located_by="llm", linked=True, v="aa997.2"):
    return {"id": f"0000000{i}-0000-0000-0000-000000000000", "name": f"Place {i}", "country": country,
            "lat": 27.7 + i / 100, "lng": 85.3, "review_reason": reason, "located_by": located_by, "outcome": outcome,
            "stamp_v": v, "accept_rule": "aa997.3", "route_km": "12.5", "linked": linked}


ROWS = [_row(1), _row(2, outcome="off_route", reason="off_route"), _row(3, "India", reason="unconfirmed_hard_type"),
        _row(4, "India", linked=False, v="aa997.1"), _row(5, "Japan")]


@pytest.fixture
def no_lock(monkeypatch):
    from services.places import store
    monkeypatch.setattr(store, "lock_destinations", AsyncMock())


# ── selection ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_selection_takes_only_stamped_rows_still_on_the_model_point():
    """Rows the chain has not reached (no stamp), a confirmed / admin-fixed / merged / hidden row, a row whose flag a
    reviewer cleared and a linear feature are never taken: the predicates are in the SQL itself."""
    from services.places import relocate
    sql = " ".join(relocate._HIDE_FLAGGED_SQL.split())
    for clause in ("d.duplicate_of IS NULL", "d.geo_status = 'ok'", "d.review_reason IS NOT NULL",
                   "d.review_reason <> 'linear_feature'",
                   "(d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($1::text[])",
                   "d.located_by = ANY($4::text[])"):
        assert clause in sql, clause
    # a stamp version / accept rule narrow it only when given
    assert "cardinality($2::text[]) = 0 OR" in sql and "$3::text IS NULL OR" in sql
    # the per-row write re-checks the row is the one the report read
    row_sql = " ".join(relocate._HIDE_FLAGGED_ROW_SQL.split())
    for clause in ("geo_status = 'ok'", "duplicate_of IS NULL", "review_reason IS NOT DISTINCT FROM $3",
                   "(geocoder_meta -> 'relocate' ->> 'outcome') IS NOT DISTINCT FROM $4",
                   "located_by IS NOT DISTINCT FROM $5", "SET geo_status = 'suspect'"):
        assert clause in row_sql, clause
    assert "jsonb_build_array" in row_sql            # an earlier hide stamp moves into hidden.log


def test_outcomes_default_to_llm_point_and_off_route():
    from services.places import relocate
    assert relocate.hide_flagged_outcomes(None) == ["llm_point", "off_route"]
    assert relocate.hide_flagged_outcomes(["weak_confirm", "confirmed", "non_place"]) == ["weak_confirm"]
    assert relocate.hide_flagged_outcomes(["confirmed"]) == ["llm_point", "off_route"]
    assert relocate.policy_stamps(None) == sorted(relocate.POLICY_STAMPS)
    assert relocate.policy_stamps(["aa997.3-flagged", "other"]) == ["aa997.3-flagged"]
    assert relocate.HIDE_FLAGGED_STAMP in relocate.POLICY_STAMPS and relocate.HIDE_STAMP in relocate.POLICY_STAMPS


# ── report and confirm ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_report_counts_per_country_and_writes_nothing(no_lock):
    from services.places import relocate
    conn = _Conn(ROWS)
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], stamp_versions=["aa997.2"],
                                      stamp_accept="aa997.3", country=None, confirm=False)
    assert conn.sql == [] and out["hidden"] == 0 and out["rows"] == 5 and out["linked"] == 4
    assert out["by_country"] == {"India": 2, "Nepal": 2, "Japan": 1}
    assert out["by_outcome"] == {"llm_point": 4, "off_route": 1}
    assert out["by_reason"]["unconfirmed_hard_type"] == 1 and out["by_stamp_version"] == {"aa997.2": 4, "aa997.1": 1}
    # the sample is spread over countries (round-robin), not the first country's rows
    assert [s["country"] for s in out["samples"][:3]] == ["India", "Japan", "Nepal"]
    assert set(out["samples"][0]) >= {"id", "name", "country", "outcome", "reason", "lat", "lng", "linked"}
    args = conn.fetch_args[0][1]
    assert args == (["llm_point", "off_route"], ["aa997.2"], "aa997.3", list(relocate.LOCATED_BY_ALLOWED), None)


@pytest.mark.asyncio
async def test_confirm_hides_each_unchanged_row_with_a_stamp_and_an_audit_row(no_lock):
    from services.places import relocate
    conn = _Conn(ROWS, changed={ROWS[1]["id"]})        # row 2 changed since the report: not hidden
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], country=None, confirm=True,
                                      max_rows=5, actor="admin:u1", job_id="job-9")
    assert out["hidden"] == 4 and out["changed_since_report"] == 1 and out["audit_rows"] == 1
    hides = conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")
    assert len(hides) == 5
    rid, stamp, reason, outcome, located_by = hides[0]
    stamp = json.loads(stamp)
    assert stamp["by"] == relocate.HIDE_FLAGGED_STAMP and stamp["job"] == "job-9" and stamp["actor"] == "admin:u1"
    assert stamp["reason"] == reason == ROWS[0]["review_reason"] and stamp["outcome"] == outcome == "llm_point"
    assert stamp["stamp_v"] == "aa997.2" and located_by == "llm"
    assert len(conn.of("UPDATE geo.place_name SET status = 'suspect'")) == 4      # only the rows hidden
    audit = [a for s, a in conn.sql if "acp_shared.audit_log" in s]
    assert len(audit) == 1
    details = json.loads(next(x for x in audit[0] if isinstance(x, str) and x.startswith("{")))
    assert details["hidden"] == 4 and ROWS[1]["id"] not in details["destination_ids"]
    assert details["outcomes"] == ["llm_point", "off_route"] and details["job"] == "job-9"


@pytest.mark.asyncio
async def test_confirm_writes_one_audit_row_per_batch(no_lock, monkeypatch):
    from services.places import relocate
    monkeypatch.setattr(relocate, "HIDE_BATCH", 2)
    conn = _Conn(ROWS)
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], country=None, confirm=True)
    assert out["hidden"] == 5 and out["audit_rows"] == 3


@pytest.mark.asyncio
async def test_confirm_refuses_when_the_selection_grew_since_the_report(no_lock):
    from services.places import relocate
    conn = _Conn(ROWS)
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], country=None, confirm=True, max_rows=3)
    assert out["status"] == "refused_selection_grew" and out["hidden"] == 0 and conn.sql == []


# ── unhide ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unhide_reverts_by_stamp_or_by_id_and_is_audited(no_lock):
    from services.places import relocate
    conn = _Conn([{"id": "d1", "same_reason": True}, {"id": "d2", "same_reason": False}])
    out = await relocate.unhide(_Pool(conn), country="Nepal", by=["aa997.3-flagged"], destination_ids=["d1", "d2"],
                                actor="admin:u1", job_id="job-7")
    assert out["unhidden"] == 1 and out["kept_reason_changed"] == 1 and out["by"] == ["aa997.3-flagged"]
    assert out["destination_ids"] == 2 and out["audit_rows"] == 1
    assert conn.fetch_args[0][1] == (["aa997.3-flagged"], "Nepal", ["d1", "d2"])
    rid, at, who = conn.of("UPDATE shared.destinations")[0]
    assert rid == "d1" and at and who == "admin:u1:job-7"
    assert "'{hidden,unhidden_by}'" in relocate._UNHIDE_ROW_SQL
    assert "d.id = ANY($3::uuid[])" in relocate._UNHIDE_ROWS_SQL
    assert "= ANY($1::text[])" in relocate._UNHIDE_ROWS_SQL
    # all policy stamps by default, every row
    conn = _Conn([])
    out = await relocate.unhide(_Pool(conn), country=None)
    assert conn.fetch_args[0][1] == (sorted(relocate.POLICY_STAMPS), None, None) and out["audit_rows"] == 0


# ── a hidden row the geocoder confirms is shown again ────────────────────────────────────────────────────────────────

def _llm_row():
    return {"id": "00000011-0000-0000-0000-000000000000", "name": "Kong Lor Cave", "country": "Laos", "lat": 17.97,
            "lng": 104.80, "located_by": "llm", "geo_status": "suspect", "geo_precision": "area",
            "review_reason": "llm_point", "name_norm": "kong lor cave", "locality_norm": "",
            "geocoder_meta": {"relocate": {"v": "aa997.1", "outcome": "llm_point"}}}


class _Geo:
    calls_external = True

    def __init__(self, hit):
        self.hit = hit

    async def confirm(self, *a, **k):
        return self.hit


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden, shown", [
    ({"by": "aa997.3-flagged", "reason": "llm_point", "at": "t"}, True),
    ({"by": "aa997.2-policy", "reason": "llm_point", "at": "t"}, True),
    ({"by": "aa997.3-flagged", "reason": "llm_point", "at": "t", "unhidden_at": "t2"}, False),
    (None, False),                                   # suspect for another reason (outlier rule): stays hidden
])
async def test_a_confirmed_relocate_shows_a_policy_hidden_row_again(no_lock, monkeypatch, hidden, shown):
    from services.places import relocate, store
    monkeypatch.setattr(store, "_write_point", AsyncMock())
    cur = {"located_by": "llm", "geo_status": "suspect", "merged": False,
           "hidden": json.dumps(hidden) if hidden else None}
    conn = _Conn(current=cur)
    hit = gc.Confirmed(17.98, 104.81, "Laos", "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})
    monkeypatch.setattr(locate, "_confirmed", lambda item, res, country: res)
    r = await relocate.apply_row(conn, _llm_row(), (), _Geo(hit), hide_unconfirmed=False)
    assert r["outcome"] == "confirmed" and bool(r.get("unhidden")) is shown
    reveal = [a for s, a in conn.sql if s.startswith("UPDATE shared.destinations SET geo_status = 'ok'")]
    assert len(reveal) == (1 if shown else 0)
    if shown:
        stamp = json.loads(reveal[0][1])
        assert stamp["by"] == hidden["by"] and stamp["unhidden_by"] == "relocate:confirmed" and stamp["unhidden_at"]
        assert conn.of("UPDATE geo.place_name SET status = 'resolved'")


def test_the_current_row_read_carries_the_hide_stamp():
    from services.places import relocate
    assert "geocoder_meta -> 'hidden' AS hidden" in relocate._CURRENT_SQL
    assert "WHERE id = $1::uuid AND geo_status = 'suspect'" in " ".join(relocate._REVEAL_CONFIRMED_SQL.split())


# ── job payload and dispatch ─────────────────────────────────────────────────────────────────────────────────────────

def test_job_payload_of_hide_flagged_and_unhide():
    from services.jobs import place_relocate_job as job
    p = job._payload({"mode": "hide_flagged", "outcomes": ["off_route", "confirmed"], "stamp_versions": ["aa997.2"],
                      "stamp_accept": "aa997.3", "confirm": True, "max_rows": "812", "country": "India"})
    assert p["mode"] == "hide_flagged" and p["outcomes"] == ["off_route"] and p["stamp_versions"] == ["aa997.2"]
    assert p["stamp_accept"] == "aa997.3" and p["confirm"] is True and p["max_rows"] == 812
    assert job._payload({"mode": "hide_flagged", "confirm": True, "max_rows": "x"})["max_rows"] == 0
    assert job._payload({"mode": "hide_flagged"})["outcomes"] == ["llm_point", "off_route"]
    u = job._payload({"mode": "unhide", "by": ["aa997.3-flagged"], "destination_ids": ["a", "", "b"]})
    assert u["by"] == ["aa997.3-flagged"] and u["destination_ids"] == ["a", "b"]
    assert "destination_ids" not in job._payload({"mode": "unhide"})


@pytest.mark.asyncio
async def test_job_runs_hide_flagged_without_a_geocoder_and_rebuilds_the_graph(monkeypatch):
    from services.jobs import place_relocate_job as job
    from services.places import relocate
    seen, graphs = {}, []

    async def fake_hide_flagged(pool, **kw):
        seen.update(kw)
        return {"mode": "hide_flagged", "hidden": 3}

    async def fake_enqueue_debounced(pool, kind, **kw):
        graphs.append(kind)
        return "graph-1", True
    monkeypatch.setattr(relocate, "hide_flagged", fake_hide_flagged)
    monkeypatch.setattr(job, "enqueue_debounced", fake_enqueue_debounced)
    monkeypatch.setattr(job, "get_geocoder", MagicMock(side_effect=AssertionError("no geocoder for a hide")))
    ctx = MagicMock()
    ctx.job_id, ctx.job.created_by = "job-5", "admin:u1"
    ctx.payload = {"mode": "hide_flagged", "confirm": True, "max_rows": 3}
    out = await job.run(ctx)
    assert out["graph_job"] == "graph-1" and graphs == [job.GRAPH_KIND]
    assert seen["actor"] == "admin:u1" and seen["job_id"] == "job-5" and seen["max_rows"] == 3 and seen["confirm"]


# ── endpoint ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class _AdminConn:
    def __init__(self, last):
        self.last = last

    async def fetchrow(self, sql, *a):
        return self.last if "'hide_flagged'" in sql else None


@pytest.mark.asyncio
async def test_endpoint_confirm_needs_the_report_of_the_same_selection(monkeypatch):
    from fastapi import HTTPException

    from api.routers import admin as admin_module
    from api.routers import admin_places
    from shared.jobs import registry
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    queued = []

    async def fake_enqueue(conn, kind, payload, **kw):
        queued.append(payload)
        return "job-1", True
    monkeypatch.setattr(registry, "enqueue", fake_enqueue)
    request = MagicMock()
    request.app.state.pool = _Pool(_AdminConn(None))
    # the report itself needs nothing
    out = await admin_places.relocate_run(admin_places.RelocateRequest(mode="hide_flagged", country="India"),
                                          request, x_admin_secret="s")
    assert out["job_id"] == "job-1" and queued[-1]["confirm"] is False
    assert queued[-1]["outcomes"] == ["llm_point", "off_route"] and "max_rows" not in queued[-1]
    body = admin_places.RelocateRequest(mode="hide_flagged", confirm=True, country="India")
    with pytest.raises(HTTPException) as e:
        await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert e.value.status_code == 409
    other = {"id": "rep-1", "result": json.dumps({"mode": "hide_flagged", "outcomes": ["llm_point", "off_route"],
                                                  "stamp_versions": [], "country": "Nepal", "rows": 9})}
    request.app.state.pool = _Pool(_AdminConn(other))
    with pytest.raises(HTTPException):                 # a report of another country
        await admin_places.relocate_run(body, request, x_admin_secret="s")
    same = {"id": "rep-2", "result": json.dumps({"mode": "hide_flagged", "outcomes": ["llm_point", "off_route"],
                                                 "stamp_versions": [], "stamp_accept": None, "country": "India",
                                                 "rows": 812})}
    request.app.state.pool = _Pool(_AdminConn(same))
    await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert queued[-1]["max_rows"] == 812 and queued[-1]["dry_run_job"] == "rep-2" and queued[-1]["confirm"] is True


def test_request_validation_of_the_new_fields():
    import uuid

    from api.routers import admin_places
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(mode="hide_flagged", outcomes=["confirmed"])
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(mode="hide_flagged", stamp_versions=["v1"])
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(mode="unhide", by=["manual"])
    with pytest.raises(ValueError):
        admin_places.RelocateRequest(mode="unhide", destination_ids=["not-a-uuid"])
    r = admin_places.RelocateRequest(mode="unhide", by=["aa997.3-flagged"], destination_ids=[str(uuid.uuid4())])
    assert r.by == ["aa997.3-flagged"] and len(r.destination_ids) == 1
    assert admin_places.RelocateRequest(mode="hide_flagged", outcomes=["off_route", "llm_point"]).outcomes == [
        "llm_point", "off_route"]

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

    def __init__(self, rows=None, changed=(), current=None, report=None):
        self.rows, self.changed = rows or [], set(changed)
        self.sql, self.fetch_args = [], []
        self.current, self.report = current, report

    async def fetch(self, sql, *a):
        self.fetch_args.append((" ".join(sql.split()), a))
        return self.rows

    async def fetchrow(self, sql, *a):
        return self.report if "FROM shared.job" in sql else self.current

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


async def _report_of(rows, *, outcomes=("llm_point", "off_route"), status="succeeded", age_s=60.0, **kw):
    """The report job a confirm names: a hide_flagged report run over `rows` (what the job stores as its result)."""
    from services.places import relocate
    res = await relocate.hide_flagged(_Pool(_Conn(rows)), outcomes=list(outcomes), confirm=False, **kw)
    return {"id": "rep-1", "status": status, "result": json.dumps(res), "age_s": age_s}


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
    # a row a person restored is reported, never hidden (selection column + the per-row guard)
    assert "AS restored" in sql and "AND NOT coalesce((geocoder_meta -> 'hidden' ->> 'unhidden_at')" in row_sql


def test_a_restored_row_is_one_a_person_unhid_and_no_relocate_judged_since():
    from services.places import relocate
    frag = " ".join(relocate._RESTORED_TPL.format(t="d.").split())
    assert "(d.geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NOT NULL" in frag
    # a relocate reveal ('relocate:confirmed') is not a person's restore
    # only an explicit restore ('restore:<actor>') is sticky: a bulk unhide ('unhide:<actor>') and a relocate reveal
    # ('relocate:confirmed') are not
    assert "coalesce(d.geocoder_meta -> 'hidden' ->> 'unhidden_by', '') LIKE 'restore:%'" in frag
    # a relocate stamp newer than the restore is a new judgement: the row may be hidden again
    assert ("(d.geocoder_meta -> 'relocate' ->> 'at')::timestamptz <= (d.geocoder_meta -> 'hidden' ->> "
            "'unhidden_at')::timestamptz") in frag


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
    conn = _Conn(ROWS + [{**_row(6, "Laos"), "restored": True}])
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], stamp_versions=["aa997.2"],
                                      stamp_accept="aa997.3", country=None, confirm=False)
    assert conn.sql == [] and out["hidden"] == 0 and out["rows"] == 5 and out["linked"] == 4
    assert out["restored_skipped"] == 1 and "Laos" not in out["by_country"]
    assert out["by_country"] == {"India": 2, "Nepal": 2, "Japan": 1}
    assert out["by_outcome"] == {"llm_point": 4, "off_route": 1}
    assert out["by_reason"]["unconfirmed_hard_type"] == 1 and out["by_stamp_version"] == {"aa997.2": 4, "aa997.1": 1}
    # the sample is spread over countries (round-robin), not the first country's rows
    assert [s["country"] for s in out["samples"][:3]] == ["India", "Japan", "Nepal"]
    assert set(out["samples"][0]) >= {"id", "name", "country", "outcome", "reason", "lat", "lng", "linked"}
    args = conn.fetch_args[0][1]
    assert args == (["llm_point", "off_route"], ["aa997.2"], "aa997.3", ["llm"], None)     # located_by default llm
    assert out["located_by"] == ["llm"]
    # what a confirm may hide: the sorted ids, a digest each (restored rows excluded)
    assert out["ids"] == sorted(r["id"] for r in ROWS) and len(out["row_digests"]) == 5
    assert out["row_digests"][0] == relocate._row_digest(ROWS[0])


@pytest.mark.asyncio
async def test_the_selection_takes_the_requested_located_by(no_lock):
    from services.places import relocate
    conn = _Conn([_row(1, located_by="mapbox")])
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], located_by=["mapbox", "llm", "manual"])
    assert conn.fetch_args[0][1][3] == ["llm", "mapbox"] and out["located_by"] == ["llm", "mapbox"]
    conn = _Conn([])
    await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], located_by=["mapbox"])
    assert conn.fetch_args[0][1][3] == ["mapbox"]


@pytest.mark.asyncio
async def test_confirm_hides_each_unchanged_row_with_a_stamp_and_an_audit_row(no_lock):
    from services.places import relocate
    conn = _Conn(ROWS, changed={ROWS[1]["id"]}, report=await _report_of(ROWS))  # row 2 changed meanwhile: kept
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], country=None, confirm=True,
                                      dry_run_job="rep-1", max_rows=5, actor="admin:u1", job_id="job-9")
    assert out["hidden"] == 4 and out["changed_since_report"] == 1 and out["audit_rows"] == 1
    assert out["dropped_ids"] == [ROWS[1]["id"]] and out["new_since_report"] == 0
    hides = conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")
    assert len(hides) == 5
    rid, stamp, reason, outcome, located_by, stamp_v, accept_rule, stamp_at, country = hides[0]
    stamp = json.loads(stamp)
    assert stamp["by"] == relocate.HIDE_FLAGGED_STAMP and stamp["job"] == "job-9" and stamp["actor"] == "admin:u1"
    assert stamp["reason"] == reason == ROWS[0]["review_reason"] and stamp["outcome"] == outcome == "llm_point"
    assert stamp["stamp_v"] == "aa997.2" and located_by == "llm" and stamp["report"] == "rep-1"
    assert stamp["prev_geo_status"] == "ok"
    # P15: the stamp version / accept rule / time and the country the confirm read are re-checked by the UPDATE
    assert (stamp_v, accept_rule, stamp_at, country) == ("aa997.2", "aa997.3", None, "Nepal")
    names = [a for s, a in conn.sql if s.startswith("WITH n AS ( UPDATE geo.place_name SET status = 'suspect'")]
    assert len(names) == 4      # only the rows hidden; the keys it changed go into the hide stamp
    audit = [a for s, a in conn.sql if "acp_shared.audit_log" in s]
    assert len(audit) == 1
    details = json.loads(next(x for x in audit[0] if isinstance(x, str) and x.startswith("{")))
    assert details["hidden"] == 4 and ROWS[1]["id"] not in details["destination_ids"]
    assert details["outcomes"] == ["llm_point", "off_route"] and details["job"] == "job-9"
    assert details["dry_run_job"] == "rep-1" and details["located_by"] == ["llm"]


@pytest.mark.asyncio
async def test_confirm_writes_one_audit_row_per_batch(no_lock, monkeypatch):
    from services.places import relocate
    monkeypatch.setattr(relocate, "HIDE_BATCH", 2)
    conn = _Conn(ROWS, report=await _report_of(ROWS, outcomes=["llm_point"]))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], country=None, confirm=True,
                                      dry_run_job="rep-1", max_rows=5)
    assert out["hidden"] == 5 and out["audit_rows"] == 3


@pytest.mark.asyncio
async def test_confirm_never_hides_a_restored_row(no_lock):
    from services.places import relocate
    restored = {**_row(6, "Laos"), "restored": True}
    conn = _Conn(ROWS + [restored], report=await _report_of(ROWS + [restored], outcomes=["llm_point"]))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point"], country=None, confirm=True,
                                      dry_run_job="rep-1", max_rows=5)
    assert out["hidden"] == 5 and out["restored_skipped"] == 1 and "status" not in out
    hides = conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")
    assert restored["id"] not in [h[0] for h in hides]


@pytest.mark.asyncio
async def test_confirm_hides_only_the_reported_rows_same_count_different_rows(no_lock):
    """The report saw rows 1, 2, 3; by the confirm row 1 left the selection (fixed) and row 4 entered it (the chain
    stamped it): the same count, other rows. Only 2 and 3 are hidden; 1 is dropped, 4 is never written."""
    from services.places import relocate
    reported = [_row(1), _row(2), _row(3)]
    now = [_row(2), _row(3), _row(4)]
    conn = _Conn(now, report=await _report_of(reported))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], confirm=True,
                                      dry_run_job="rep-1", max_rows=3)
    hides = [h[0] for h in conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")]
    assert hides == [_row(2)["id"], _row(3)["id"]] and out["hidden"] == 2
    assert out["dropped_ids"] == [_row(1)["id"]] and out["new_since_report"] == 1 and "status" not in out
    # a reported row whose reason (a digest field) changed since is dropped too, not hidden
    conn = _Conn([_row(2), {**_row(3), "review_reason": "off_route"}], report=await _report_of(reported))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], confirm=True,
                                      dry_run_job="rep-1", max_rows=3)
    assert [h[0] for h in conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")] == [_row(2)["id"]]
    assert out["dropped_ids"] == [_row(1)["id"], _row(3)["id"]] and out["changed_since_report"] == 2



@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [("stamp_at", "2026-11-01T02:00:00+00:00"), ("stamp_v", "aa997.9")])
async def test_a_row_rejudged_since_the_report_is_changed_since_report(no_lock, field, value):
    """S262 Q2: the digest carries the relocate stamp's `at` and `v`, so a row a relocate run re-judged between the
    report and the confirm (same reason / outcome / located_by, new stamp) is dropped, not hidden on the old report."""
    from services.places import relocate
    at = "2026-10-31T01:00:00+00:00"
    reported = [{**_row(1), "stamp_at": at}, {**_row(2), "stamp_at": at}]
    now = [reported[0], {**reported[1], field: value}]
    assert relocate._row_digest(now[1]) != relocate._row_digest(reported[1])
    conn = _Conn(now, report=await _report_of(reported))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], confirm=True,
                                      dry_run_job="rep-1", max_rows=2)
    assert [h[0] for h in conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")] == [_row(1)["id"]]
    assert out["hidden"] == 1 and out["dropped_ids"] == [_row(2)["id"]] and out["changed_since_report"] == 1


@pytest.mark.asyncio
async def test_a_failing_audit_flush_never_replaces_the_runs_own_exception(monkeypatch):
    """S262 Q3: apply() flushes its pending reveal audit rows when the row loop raises; a flush that fails too (a new
    pool connection) is logged and the run's own exception propagates. Without a run exception the flush error is
    raised (an audit row is never lost silently)."""
    from services.places import relocate
    monkeypatch.setattr(relocate, "candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(relocate, "routes_by_destination", AsyncMock(return_value={}))
    monkeypatch.setattr(relocate, "count_candidates", AsyncMock(return_value=0))
    write = AsyncMock(side_effect=ConnectionError("pool gone"))
    monkeypatch.setattr(relocate, "_audit_batch", write)

    def rows_then(exc):
        async def run(pool, geocoder, rows, routes, towns, out, audit, **kw):
            await audit.add("d-1", {"outcome": "confirmed", "unhidden": True, "hidden_by": "hide_flagged"})
            if exc:
                raise exc
        return run

    monkeypatch.setattr(relocate, "_apply_rows", rows_then(ValueError("row loop broke")))
    with pytest.raises(ValueError, match="row loop broke"):
        await relocate.apply(_Pool(_Conn()), None, located_by=["llm"], country="Nepal", limit=5)
    assert write.await_count == 1          # the flush was tried
    monkeypatch.setattr(relocate, "_apply_rows", rows_then(None))
    with pytest.raises(ConnectionError):
        await relocate.apply(_Pool(_Conn()), None, located_by=["llm"], country="Nepal", limit=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw, report_kw, status", [
    ({"dry_run_job": None, "max_rows": 5}, {}, "refused_no_report"),
    ({"dry_run_job": "rep-1", "max_rows": None}, {}, "refused_no_report"),
    ({"dry_run_job": "rep-1", "max_rows": 5}, {"age_s": 7 * 3600.0}, "refused_report_stale"),
    ({"dry_run_job": "rep-1", "max_rows": 5}, {"status": "running"}, "refused_report_pending"),
    ({"dry_run_job": "rep-1", "max_rows": 5}, {"status": "failed"}, "refused_report_failed"),
    ({"dry_run_job": "rep-1", "max_rows": 4}, {}, "refused_report_row_count_differs"),
    ({"dry_run_job": "rep-1", "max_rows": 5, "located_by": ["mapbox"]}, {}, "refused_report_selection_differs"),
    ({"dry_run_job": "rep-1", "max_rows": 5, "country": "India"}, {}, "refused_report_selection_differs"),
])
async def test_a_confirm_without_a_fresh_report_of_its_selection_is_refused(no_lock, kw, report_kw, status):
    from services.places import relocate
    conn = _Conn(ROWS, report=await _report_of(ROWS, **report_kw))
    out = await relocate.hide_flagged(_Pool(conn), outcomes=["llm_point", "off_route"], confirm=True, **kw)
    assert out["status"] == status and out["hidden"] == 0 and conn.sql == []


@pytest.mark.asyncio
async def test_a_report_job_that_is_missing_or_not_a_report_refuses(no_lock):
    from services.places import relocate
    sel = relocate.hide_flagged_selection(outcomes=None, stamp_versions=[], stamp_accept=None, located_by=None,
                                          country=None)
    assert relocate.check_hide_flagged_report(None, sel) == "no_report"
    confirm_job = {"status": "succeeded", "age_s": 1.0, "result": {"mode": "hide_flagged", "confirm": True}}
    assert relocate.check_hide_flagged_report(confirm_job, sel) == "not_a_report"
    old = {"status": "succeeded", "age_s": 1.0, "result": {"mode": "hide_flagged", "outcomes": sel["outcomes"],
                                                           "located_by": ["llm"], "rows": 3}}
    assert relocate.check_hide_flagged_report(old, sel) == "report_without_ids"     # a report before the id list


@pytest.mark.asyncio
async def test_a_report_too_large_lists_no_ids(no_lock, monkeypatch):
    from services.places import relocate
    monkeypatch.setattr(relocate, "MAX_REPORT_IDS", 3)
    out = await relocate.hide_flagged(_Pool(_Conn(ROWS)), outcomes=["llm_point"])
    assert "ids" not in out and out["ids_omitted"] and out["rows"] == 5


# ── plain hide (reason-based) ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plain_hide_skips_restored_rows_and_writes_an_audit_row_per_batch(no_lock, monkeypatch):
    from services.places import relocate
    monkeypatch.setattr(relocate, "HIDE_BATCH", 2)
    rows = [{"id": f"h{i}", "name": f"P{i}", "country": "Nepal", "review_reason": "llm_point", "located_by": "llm",
             "restored": i == 3} for i in range(1, 5)]
    conn = _Conn(rows)
    out = await relocate.hide(_Pool(conn), reasons=["llm_point"], country=None, confirm=False)
    assert out["rows"] == 3 and out["restored_skipped"] == 1 and conn.sql == []
    out = await relocate.hide(_Pool(conn), reasons=["llm_point"], country=None, confirm=True, actor="admin:u1",
                              job_id="job-3")
    hides = conn.of("UPDATE shared.destinations SET geo_status = 'suspect'")
    assert [h[0] for h in hides] == ["h1", "h2", "h4"] and out["hidden"] == 3 and out["audit_rows"] == 2
    rid, stamp, reason = hides[0]
    assert reason == "llm_point" and json.loads(stamp)["actor"] == "admin:u1" and json.loads(stamp)["job"] == "job-3"
    audit = [a for s, a in conn.sql if "acp_shared.audit_log" in s]
    assert len(audit) == 2 and any(x == relocate.AUDIT_HIDE_POLICY for x in audit[0])
    sql = " ".join(relocate._HIDE_ROW_SQL.split())
    assert "review_reason IS NOT DISTINCT FROM $3" in sql and "LIKE 'restore:%'" in sql
    assert "AS restored" in relocate._HIDE_CANDIDATES_SQL


# ── unhide ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unhide_reverts_by_stamp_or_by_id_and_is_audited(no_lock):
    from services.places import relocate
    conn = _Conn([{"id": "d1", "same_reason": True}, {"id": "d2", "same_reason": False}])
    out = await relocate.unhide(_Pool(conn), country="Nepal", by=["aa997.3-flagged"], destination_ids=["d1", "d2"],
                                actor="admin:u1", job_id="job-7")
    # an explicit id list is the admin's restore: reverted whatever the reason now
    assert out["unhidden"] == 2 and out["kept_reason_changed"] == 0 and out["restored_reason_changed"] == 1
    assert out["by"] == ["aa997.3-flagged"] and out["destination_ids"] == 2 and out["audit_rows"] == 1
    assert conn.fetch_args[0][1] == (["aa997.3-flagged"], "Nepal", ["d1", "d2"])
    rid, at, who, explicit, job = conn.of("UPDATE shared.destinations")[0]
    # an explicit id restore is a person's restore: sticky ('restore:'), the job kept next to it
    assert rid == "d1" and at and who == "restore:admin:u1" and explicit is True and job == "job-7"
    assert "'unhidden_by', $3::text" in relocate._UNHIDE_ROW_SQL
    assert "($4::bool OR review_reason IS NOT DISTINCT FROM" in " ".join(relocate._UNHIDE_ROW_SQL.split())
    # by stamp: a row another path flagged since keeps its state
    conn = _Conn([{"id": "d1", "same_reason": True}, {"id": "d2", "same_reason": False}])
    out = await relocate.unhide(_Pool(conn), country=None, by=["aa997.3-flagged"], actor="admin:u1")
    assert out["unhidden"] == 1 and out["kept_reason_changed"] == 1 and out["restored_reason_changed"] == 0
    assert [a[3] for a in conn.of("UPDATE shared.destinations")] == [False]
    assert conn.of("UPDATE shared.destinations")[0][2] == "unhide:admin:u1"      # a bulk unhide is not sticky
    assert "d.duplicate_of IS NULL" in relocate._UNHIDE_ROWS_SQL
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
    reveal = [a for s, a in conn.sql
              if s.startswith("UPDATE shared.destinations SET geo_status = coalesce($2::jsonb ->> 'prev_geo_status'")]
    assert len(reveal) == (1 if shown else 0)
    if shown:
        stamp = json.loads(reveal[0][1])
        assert stamp["by"] == hidden["by"] and stamp["unhidden_by"] == "relocate:confirmed" and stamp["unhidden_at"]
        assert conn.of("UPDATE geo.place_name pn SET status = 'resolved'")
        assert r["hidden_by"] == hidden["by"]


@pytest.mark.asyncio
@pytest.mark.parametrize("on_route, non_place, reason", [
    (False, None, "off_route"),                      # llm_point -> off_route
    (None, "generic", "non_place:generic"),          # llm_point -> non_place:*
])
async def test_a_recheck_that_flags_a_hidden_row_again_moves_its_hide_stamp_reason(no_lock, monkeypatch, on_route,
                                                                                   non_place, reason):
    """hide, then an aa997.1 recheck finds no hit and flags the row with another reason (it stays suspect): the hide
    stamp takes the new reason, so unhide (by stamp or id) can still revert it."""
    from services.places import relocate
    cur = {"located_by": "llm", "geo_status": "suspect", "merged": False,
           "hidden": json.dumps({"by": "aa997.3-flagged", "reason": "llm_point", "at": "t"})}
    conn = _Conn(current=cur)
    monkeypatch.setattr(relocate, "classify_row", lambda row, routes, **k: {
        "non_place": non_place, "query": row["name"], "on_route": on_route, "route_km": 80.0 if on_route is False
        else None, "hk_mo": None, "requests": 1})
    r = await relocate.apply_row(conn, _llm_row(), (), _Geo(None), hide_unconfirmed=False)
    assert r["outcome"] == ("off_route" if non_place is None else "non_place")
    flag = conn.of("UPDATE shared.destinations SET review_reason")
    assert flag and flag[0][1] == reason
    sync = [a for s, a in conn.sql if "'reason_was'" in s]
    assert len(sync) == 1 and sync[0][1] == reason and sync[0][2].startswith("relocate:")
    assert sync[0][3] == list(relocate.POLICY_STAMPS)


@pytest.mark.asyncio
async def test_a_recheck_leaves_the_hide_stamp_of_a_row_no_policy_hid(no_lock, monkeypatch):
    from services.places import relocate
    cur = {"located_by": "llm", "geo_status": "suspect", "merged": False, "hidden": None}   # outlier rule
    conn = _Conn(current=cur)
    monkeypatch.setattr(relocate, "classify_row", lambda row, routes, **k: {
        "non_place": None, "query": row["name"], "on_route": False, "route_km": 80.0, "hk_mo": None, "requests": 1})
    await relocate.apply_row(conn, _llm_row(), (), _Geo(None), hide_unconfirmed=False)
    assert not [s for s, _ in conn.sql if "'reason_was'" in s]


def test_the_hidden_reason_sync_touches_only_a_live_policy_hide():
    from services.places import relocate
    sql = " ".join(relocate._HIDDEN_REASON_SQL.split())
    for clause in ("geo_status = 'suspect'", "(geocoder_meta -> 'hidden' ->> 'unhidden_at') IS NULL",
                   "(geocoder_meta -> 'hidden' ->> 'by') = ANY($4::text[])",
                   "(geocoder_meta -> 'hidden' ->> 'reason') IS DISTINCT FROM $2"):
        assert clause in sql, clause


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
    assert job._payload({"mode": "hide_flagged", "dry_run_job": "rep-1"})["dry_run_job"] == "rep-1"
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
    ctx.payload = {"mode": "hide_flagged", "confirm": True, "max_rows": 3, "dry_run_job": "rep-1",
                   "located_by": ["mapbox"]}
    out = await job.run(ctx)
    assert out["graph_job"] == "graph-1" and graphs == [job.GRAPH_KIND]
    assert seen["actor"] == "admin:u1" and seen["job_id"] == "job-5" and seen["max_rows"] == 3 and seen["confirm"]
    assert seen["dry_run_job"] == "rep-1" and seen["located_by"] == ["mapbox"]
    # a confirm that hid nothing now (a retry after a crash between batches) still queues the graph rebuild
    seen.clear()
    monkeypatch.setattr(relocate, "hide_flagged", AsyncMock(side_effect=lambda pool, **kw: {"mode": "hide_flagged",
                                                                                           "hidden": 0}))
    assert (await job.run(ctx))["graph_job"] == "graph-1" and len(graphs) == 2
    # a report writes nothing: no rebuild
    ctx.payload = {"mode": "hide_flagged"}
    assert "graph_job" not in await job.run(ctx) and len(graphs) == 2


# ── endpoint ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class _AdminConn:
    def __init__(self, last, apply=None):
        self.last, self.apply = last, apply

    async def fetchrow(self, sql, *a):
        if "'apply'" in sql:
            return self.apply
        return self.last if "'hide_flagged'" in sql else None


def _report_job(*, status="succeeded", age_s=60.0, **res):
    base = {"mode": "hide_flagged", "outcomes": ["llm_point", "off_route"], "stamp_versions": [],
            "stamp_accept": None, "located_by": ["llm"], "country": "India", "rows": 3, "ids": ["a", "b", "c"],
            "row_digests": ["1", "2", "3"]}
    return {"id": "rep-2", "status": status, "age_s": age_s, "result": json.dumps({**base, **res})}


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

    async def refused(last, apply=None, b=body):
        request.app.state.pool = _Pool(_AdminConn(last, apply))
        with pytest.raises(HTTPException) as e:
            await admin_places.relocate_run(b, request, x_admin_secret="s")
        assert e.value.status_code == 409
        return e.value.detail
    assert (await refused(None))["reason"] == "no_report"
    assert (await refused(_report_job(country="Nepal")))["reason"] == "report_selection_differs"
    assert (await refused(_report_job(status="queued")))["reason"] == "report_pending"
    assert (await refused(_report_job(age_s=7 * 3600.0)))["reason"] == "report_stale"
    assert (await refused(_report_job(ids=None)))["reason"] == "report_without_ids"
    # located_by is part of the selection: a report of llm rows does not back a confirm of mapbox rows
    mapbox = admin_places.RelocateRequest(mode="hide_flagged", confirm=True, country="India", located_by=["mapbox"])
    assert (await refused(_report_job(), b=mapbox))["reason"] == "report_selection_differs"
    # an open apply (any rule version) keeps stamping rows: no confirm meanwhile
    assert (await refused(_report_job(), apply={"id": "apply-1"}))["apply_job"] == "apply-1"
    n = len(queued)
    request.app.state.pool = _Pool(_AdminConn(_report_job()))
    await admin_places.relocate_run(body, request, x_admin_secret="s")
    assert len(queued) == n + 1
    assert queued[-1]["max_rows"] == 3 and queued[-1]["dry_run_job"] == "rep-2" and queued[-1]["confirm"] is True
    assert queued[-1]["located_by"] == ["llm"]


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

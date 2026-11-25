"""AA-1142 (S292) — the P2 features of the Ecosystem status page without a database or AWS: the availability objective
with its error budget and burn rate alerts (a synthetic error burst fires the fast burn), the external synthetic checks
(feed parsing, states, certificate levels, the strip, attention items, the sampler's copy), the incident record (rules,
changes, the timeline, the lesson draft, the routes and who may act), the per-agency drill-down and the Dev drill route.
Invented names, hosts and agencies only; every parameter is a fixed value."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import ops_status
from api.services.ops_status import external, incidents, slo, tenants, verdict
from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

NOW = datetime(2026, 11, 25, 10, 2, tzinfo=timezone.utc)
RNG = TimeRange("6h", NOW - timedelta(hours=6), NOW, 300)
_SECRET = "aa1142-test-secret"
STAFF = "00000000-0000-4000-8000-00000000a142"
AGENCY_A = "00000000-0000-4000-8000-0000000000aa"
AGENCY_B = "00000000-0000-4000-8000-0000000000bb"
RULES = {"target_pct": 99.5, "window_days": 30, "fast": {"window_min": 60, "factor": 14.4},
         "slow": {"window_hours": 6, "factor": 6.0}, "min_requests": 50}


# ── SLO, error budget, burn rate ────────────────────────────────────────────────────────────────────────────────

def test_burn_rate_and_judgement():
    assert slo.burn(5, 1000, 0.005) == 1.0            # spending the budget exactly
    assert slo.burn(0, 0, 0.005) is None
    assert slo.judge(None, 0, 14.4, 50) == "none"
    assert slo.judge(30.0, 20, 14.4, 50) == "few"       # under the minimum of events: shown, not judged
    assert slo.judge(30.0, 400, 14.4, 50) == "alert" and slo.judge(2.0, 400, 14.4, 50) == "ok"


def test_app_row_budget_left_and_states():
    row = slo.app_row("cis", "CIS", "requests", {"window": (100_000, 200), "slow": (6000, 12), "fast": (1000, 2)},
                      RULES)
    assert row["availability_pct"] == 99.8 and row["budget_left_pct"] == 60.0 and row["below_target"] is False
    assert row["fast"] == {"burn": 0.4, "events": 1000, "bad": 2, "factor": 14.4, "state": "ok"}
    over = slo.app_row("booking", "Booking", "requests", {"window": (1000, 10), "slow": (0, 0), "fast": (0, 0)},
                       RULES)
    assert over["budget_left_pct"] == -100.0 and over["below_target"] is True and over["fast"]["state"] == "none"


def _points(app_ts: list[tuple[datetime, float, float]]) -> dict:
    return {"requests": [(t.isoformat(), r) for t, r, _ in app_ts],
            "errors": [(t.isoformat(), e) for t, _, e in app_ts]}


def test_a_synthetic_error_burst_fires_the_fast_burn_alert_and_the_verdict_lists_it():
    """The Dev drill in miniature: quiet traffic all window, then a burst of 503s in the last 5 minutes."""
    starts = slo.windows(NOW, RULES)
    quiet = [(NOW - timedelta(minutes=5 * i), 20.0, 0.0) for i in range(2, 72)]
    burst = [(NOW - timedelta(minutes=5), 260.0, 200.0)]
    short = {"status": "ok", "apps": {"cis": _points(quiet + burst)}}
    long_ = {"status": "ok", "apps": {"cis": _points([(NOW - timedelta(days=3), 50_000.0, 10.0),
                                                      (NOW - timedelta(hours=1), 1500.0, 200.0)])}}
    rows = {r["app"]: r for r in slo.metric_rows(short, long_, "dev", starts, RULES)}
    cis = rows["cis"]
    assert cis["fast"]["state"] == "alert" and cis["fast"]["burn"] > 14.4
    assert cis["slow"]["state"] == "alert"            # 200 bad of about 1,660 over 6 h: above 6x as well
    assert rows["booking"]["fast"]["state"] == "none"  # no points: nothing judged
    data = {"rules": RULES, "rows": list(rows.values())}
    got = {i["id"]: i for i in slo.verdict_items(data)}
    assert got["slo-fast-cis"]["severity"] == "sev2" and got["slo-slow-cis"]["severity"] == "sev3"
    assert got["slo-fast-cis"]["msg"]["key"] == "slo_burn" and got["slo-fast-cis"]["msg"]["limit"] == 14.4
    out = verdict.decide({"slo": {"status": "ok", "data": data}}, th.snapshot(), [])
    assert "slo-fast-cis" in {i["id"] for i in out["attention"]} and out["status"] == "attention"


def test_cloudwatch_unavailable_rows_are_not_measured_never_green():
    starts = slo.windows(NOW, RULES)
    rows = slo.metric_rows({"status": "unavailable"}, {"status": "ok", "apps": {}}, "dev", starts, RULES)
    assert {r["app"] for r in rows} == {"cis", "tripplanner", "booking"}
    assert all(r["measured"] is False and r["reason"] == "cloudwatch_unavailable" for r in rows)
    assert slo.items(rows) == []


def test_queries_cover_requests_and_errors_of_every_measured_source():
    qs = slo.queries("dev", 300)
    ids = {q["Id"] for q in qs}
    assert {"cis_0_requests", "cis_0_errors", "booking_0_errors", "tripplanner_1_requests"} <= ids
    assert not any(i.startswith("crm_") for i in ids) and all(q["MetricStat"]["Period"] == 300 for q in qs)


def test_points_sum_sources_per_timestamp():
    t = NOW.isoformat()
    res = [{"Id": "tripplanner_0_requests", "Timestamps": [t], "Values": [3]},
           {"Id": "tripplanner_1_requests", "Timestamps": [t], "Values": [4]},
           {"Id": "tripplanner_0_errors", "Timestamps": [t], "Values": [1]}]
    p = slo.points(res, "dev")["tripplanner"]
    assert p["requests"] == [(t, 7.0)] and p["errors"] == [(t, 1.0)]


@pytest.mark.asyncio
async def test_health_sample_row_for_the_crm():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"good_window": 8000, "bad_window": 20, "good_slow": 70, "bad_slow": 2,
                                            "good_fast": 11, "bad_fast": 1})
    row = await slo._health_row(pool, "crm", "Partner CRM", slo.windows(NOW, RULES), RULES)
    assert row["basis"] == "health samples" and row["events"] == 8020
    assert row["fast"]["state"] == "few"              # 12 samples < the 50 minimum: not judged
    assert row["slow"]["state"] == "ok"               # 72 samples, 2 bad: burn 5.56x, under 6x
    pool.fetchrow = AsyncMock(return_value={"good_window": 0, "bad_window": 0, "good_slow": 0, "bad_slow": 0,
                                            "good_fast": 0, "bad_fast": 0})
    assert (await slo._health_row(pool, "crm", "Partner CRM", slo.windows(NOW, RULES), RULES))["measured"] is False


# ── external checks ─────────────────────────────────────────────────────────────────────────────────────────────

LIMITS = {"warn": 21.0, "crit": 7.0}


def _doc(**over) -> dict:
    r = {"target": "zz-api", "app": "cis", "host": "api.zz.example", "url": "https://api.zz.example/health",
         "mode": "health", "dns_ok": True, "tls_ok": True, "cert_not_after": "2027-01-20T00:00:00Z",
         "http_status": 200, "http_ok": True, "latency_ms": 210, "error": None}
    r.update(over)
    return {"format": 1, "checked_at": "2026-11-25T10:00:00Z", "results": [r]}


def test_parse_checks_every_field_and_drops_bad_targets():
    at, rows = external.parse(_doc())
    assert at == datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc) and rows[0]["cert_not_after"].year == 2027
    _, rows = external.parse(_doc(target="Bad Target!", host="x"))
    assert rows == []
    _, rows = external.parse(_doc(app="nowhere", http_status=999, latency_ms=-3, error="e" * 400))
    assert rows[0]["app"] is None and rows[0]["http_status"] is None and rows[0]["latency_ms"] is None
    assert len(rows[0]["error"]) == 300
    for bad in ({"format": 2}, {"format": 1}, {"format": 1, "checked_at": "2026-11-25T10:00:00Z", "results": "x"},
                {"format": 1, "checked_at": "2026-11-25T10:00:00", "results": []}):
        with pytest.raises(external.FeedError):
            external.parse(bad)


@pytest.mark.parametrize("over,state", [
    ({}, "ok"),
    ({"cert_not_after": "2026-12-10T00:00:00Z"}, "warn"),          # 14.6 days left
    ({"cert_not_after": "2026-11-29T00:00:00Z"}, "degraded"),      # 3.6 days left
    ({"http_status": 502, "http_ok": False}, "degraded"),
    ({"dns_ok": False, "http_status": None}, "down"),
    ({"tls_ok": False}, "down"),
    ({"http_status": None, "error": "timed out"}, "down"),
    ({"mode": "tls", "http_status": None}, "ok"),                   # TLS-only target: no health path
    ({"mode": "tls", "http_status": None, "tls_ok": None, "error": "TimeoutError"}, "down"),   # no handshake
    ({"mode": "tls", "http_status": None, "tls_ok": None, "error": "ConnectionRefusedError"}, "down"),
    ({"tls_ok": None, "http_status": None, "error": "TimeoutError"}, "down"),   # same case in health mode
    ({"dns_ok": None, "tls_ok": None, "mode": "tls", "http_status": None}, "unknown"),   # nothing was checked
])
def test_state_of(over, state):
    _, rows = external.parse(_doc(**over))
    assert external.state_of(rows[0], datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc), LIMITS) == state


def test_cert_level():
    assert external.cert_level(None, LIMITS) == "none"
    assert external.cert_level(30.0, LIMITS) == "ok" and external.cert_level(21.0, LIMITS) == "warn"
    assert external.cert_level(7.0, LIMITS) == "crit"


@pytest.mark.asyncio
async def test_ingest_copies_a_run_once_with_its_state():
    sink: list = []

    class _Conn:
        async def executemany(self, sql, rows):
            sink.append((sql, list(rows)))

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = lambda: _Acq()
    out: dict = {}
    doc = _doc(cert_not_after="2026-11-29T00:00:00Z")
    await external.ingest(pool, out, loader=lambda: json.dumps(doc).encode())
    assert out["external"]["results"] == 1 and "ON CONFLICT (target, checked_at) DO NOTHING" in sink[0][0]
    assert sink[0][1][0][1] == "zz-api" and sink[0][1][0][4] == "degraded"
    out = {}
    await external.ingest(pool, out, loader=lambda: None)
    assert out["external"] == "not_published"
    await external.ingest(pool, out, loader=lambda: b"{not json")
    assert out["external"].startswith("unreadable")


def _ext_row(target: str, state: str, days: float | None, at: datetime) -> dict:
    return {"checked_at": at, "target": target, "app": "cis", "host": f"{target}.zz.example", "state": state,
            "dns_ok": state != "down", "tls_ok": True, "http_status": 200 if state != "down" else None,
            "latency_ms": 100, "error": None,
            "cert_not_after": None if days is None else at + timedelta(days=days)}


def test_target_rows_items_and_stale_feed():
    at = NOW - timedelta(minutes=10)
    latest = [_ext_row("zz-api", "ok", 40, at), _ext_row("zz-web", "down", 40, at), _ext_row("zz-auth", "ok", 3, at)]
    in_range = latest + [_ext_row("zz-api", "down", 40, at - timedelta(hours=2))]
    rows = external.target_rows(in_range, latest, RNG, NOW, LIMITS, 99.5)
    by = {r["target"]: r for r in rows}
    assert by["zz-api"]["cert_level"] == "ok" and "down" in by["zz-api"]["cells"]
    assert by["zz-auth"]["cert_level"] == "crit"
    got = {i[0]: i for i in external.items({"rows": rows, "stale": False})}
    assert set(got) == {"external-zz-web", "external-cert-zz-auth"}
    items = {i["id"]: i for i in external.verdict_items({"rows": rows, "stale": False})}
    assert items["external-zz-web"]["severity"] == "sev2" and "DNS" in items["external-zz-web"]["title"]
    stale = external.verdict_items({"rows": rows, "stale": True, "newest": at.isoformat()})
    assert [i["id"] for i in stale] == ["external-feed"] and stale[0]["severity"] == "sev3"
    assert external.stale_after(3) == timedelta(minutes=45)


# ── incidents ───────────────────────────────────────────────────────────────────────────────────────────────────

def _inc(**over) -> dict:
    r = {"incident_id": 7, "title": "Invented outage of zz", "severity": "sev2", "status": "investigating",
         "app": "cis", "item_id": "errors-cis", "owner": f"admin:{STAFF}", "summary": None,
         "declared_by": f"admin:{STAFF}", "declared_at": NOW - timedelta(minutes=30),
         "updated_at": NOW - timedelta(minutes=30), "resolved_at": None}
    r.update(over)
    return r


def test_validate_declare():
    f = incidents.validate_declare("  Invented zz outage ", "sev1", "booking", "  ")
    assert f == {"title": "Invented zz outage", "severity": "sev1", "app": "booking", "summary": None}
    for args in (("zz", "sev1", None, None), ("Invented zz", "sev4", None, None), ("Invented zz", "sev2", "x", None),
                 ("Invented zz", "sev2", None, "s" * 2001)):
        with pytest.raises(incidents.IncidentError):
            incidents.validate_declare(*args)


def test_changes_resolve_reopen_take_over():
    row = _inc()
    new, diff = incidents.changes(row, status="resolved", severity="sev3", take_over=True, actor="admin:zz-other",
                                  now=NOW)
    assert new["resolved_at"] == NOW and new["owner"] == "admin:zz-other"
    assert [d[0] for d in diff] == ["status", "severity", "owner"]
    new, diff = incidents.changes(_inc(status="resolved", resolved_at=NOW), status="monitoring", severity=None,
                                  take_over=False, actor="admin:zz", now=NOW)
    assert new["resolved_at"] is None and diff == [("status", "resolved", "monitoring")]
    assert incidents.changes(row, status=None, severity=None, take_over=False, actor=row["owner"], now=NOW)[1] == []
    with pytest.raises(incidents.IncidentError):
        incidents.changes(row, status="paused", severity=None, take_over=False, actor="admin:zz", now=NOW)


def test_timeline_merges_updates_events_and_acks_oldest_first():
    inc = _inc()
    updates = [{"at": NOW - timedelta(minutes=30), "actor": inc["owner"], "kind": "declared", "from_value": None,
                "to_value": "sev2", "note": None}]
    events = [{"event_id": 3, "occurred_at": NOW - timedelta(minutes=50), "ended_at": None, "kind": "deploy",
               "severity": "info", "app": "cis", "subject": "zz-api", "title": "zz deploy"}]
    acks = [{"ack_id": 1, "action": "ack", "until": NOW + timedelta(hours=1), "reason": "zz looking into it now",
             "actor": inc["owner"], "created_at": NOW - timedelta(minutes=40), "cleared_at": None}]
    rows = incidents.timeline(inc, updates, events, acks)
    assert [r["kind"] for r in rows] == ["event", "ack", "update"] and rows[0]["id"] == "ev-3"
    start, end = incidents.window(inc, NOW, 60)
    assert start == NOW - timedelta(minutes=90) and end == NOW
    assert incidents.event_apps("cis") == ["cis", "shared"] and incidents.event_apps("shared") is None


def test_lesson_draft_follows_the_lesson_log_format():
    d = incidents.lesson_draft(incidents.view(_inc()))
    assert d.startswith("<principle> — Invented outage of zz (SEV2, cis) → <cause> (incident 7, 2026-11-25)")
    assert d.endswith("Catch with: <guard>.")


# ── per-agency drill-down ───────────────────────────────────────────────────────────────────────────────────────

def test_check_tenant_and_hour_window():
    assert tenants.check_tenant(None) is None and tenants.check_tenant(AGENCY_A.upper()) == AGENCY_A
    with pytest.raises(tenants.TenantError):
        tenants.check_tenant("zz-not-an-id")
    h0, h1 = tenants.hour_window(TimeRange("custom", NOW - timedelta(minutes=90), NOW, 300))
    assert (h0, h1) == (datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc), datetime(2026, 11, 25, 11, 0,
                                                                                         tzinfo=timezone.utc))


def test_rankings_top_n_and_platform_share():
    names = {AGENCY_A: {"name": "Zz Travel", "slug": "zz-travel", "is_active": True},
             AGENCY_B: {"name": "Yy Tours", "slug": "yy-tours", "is_active": True}}
    ai = [{"tenant_id": AGENCY_A, "cost_usd": 1.5, "requests": 30, "failed": 3},
          {"tenant_id": AGENCY_B, "cost_usd": 4.0, "requests": 80, "failed": 0},
          {"tenant_id": None, "cost_usd": 9.0, "requests": 500, "failed": 7}]
    jobs = [{"tenant_id": AGENCY_A, "failed": 2, "last": NOW}]
    out = tenants.rankings(ai, jobs, names, 1)
    assert [r["name"] for r in out["ai_spend"]] == ["Yy Tours"]          # top 1 by spend
    assert out["ai_errors"][0]["tenant_id"] == AGENCY_A and out["ai_errors"][0]["failed_pct"] == 10.0
    assert out["failed_jobs"][0]["failed"] == 2 and out["platform"] == {"cost_usd": 9.0, "requests": 500, "failed": 7}
    assert out["agencies_seen"] == 2
    assert [o["name"] for o in tenants.options(names)] == ["Yy Tours", "Zz Travel"]


@pytest.mark.asyncio
async def test_collect_one_agency_breakdown():
    pool = MagicMock()

    async def fetch(sql, *args):
        if sql == tenants.NAMES_SQL:
            return [{"tenant_id": AGENCY_A, "name": "Zz Travel", "slug": "zz-travel", "is_active": True}]
        if sql == tenants.ONE_AI_SQL:
            return [{"app": "cis", "stage": "zz_stage", "cost_usd": 0.25, "requests": 10, "failed": 1}]
        if sql == tenants.ONE_ERRORS_SQL:
            return [{"error_class": "throttled", "requests": 1}]
        return [{"kind": "zz_kind", "failed": 2, "last": NOW}]

    pool.fetch = fetch
    out = await tenants.collect(pool, RNG, AGENCY_A, NOW)
    ag = out["agency"]
    assert ag["known"] is True and ag["cost_usd"] == 0.25 and ag["jobs_failed"] == 2 and ag["ai_failed"] == 1
    assert out["not_measured"] == ["server_errors_per_agency", "crm_jobs_per_agency"] and out["top_n"] == 10


# ── routes ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)          # not the business owner
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)

    async def fake_build(pool, redis, **kw):
        return {"verdict": {"attention": [{"id": "errors-cis", "app": "cis", "severity": "sev2", "title": "zz"}]}}

    monkeypatch.setattr(ops_status, "build_status", fake_build)
    seen: dict = {}

    async def fake_declare(pool, **kw):
        seen.update(kw)
        return incidents.view(_inc(item_id=kw["item_id"], app=kw["app"]))

    monkeypatch.setattr(incidents, "declare", fake_declare)
    yield TestClient(app), pool, seen


def _h(role: str | None = "admin") -> dict:
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": STAFF}
    if role:
        h["x-admin-role"] = role
    return h


BODY = {"title": "Invented outage of zz", "severity": "sev2", "item_id": "errors-cis"}


@pytest.mark.parametrize("role,status", [("admin", 200), ("content", 403), ("viewer", 403), ("finance", 403)])
def test_declare_route_policy(client, role, status):
    c, _pool, seen = client
    resp = c.post("/admin/overview/incidents", headers=_h(role), json=BODY)
    assert resp.status_code == status, resp.text
    if status == 200:
        assert seen["actor"] == f"admin:{STAFF}" and seen["app"] == "cis" and seen["item_id"] == "errors-cis"


def test_declare_refuses_the_business_owner_an_unknown_item_and_a_second_declaration(client, monkeypatch):
    c, pool, _seen = client
    resp = c.post("/admin/overview/incidents", headers=_h(), json={**BODY, "item_id": "nowhere-zz"})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "no_such_item"

    async def taken(pool, **kw):
        raise incidents.IncidentError("already_open", "zz", status=409, incident_id=5)

    monkeypatch.setattr(incidents, "declare", taken)
    resp = c.post("/admin/overview/incidents", headers=_h(), json=BODY)
    assert resp.status_code == 409 and resp.json()["detail"]["incident_id"] == 5
    pool.fetchval = AsyncMock(return_value=True)
    resp = c.post("/admin/overview/incidents", headers=_h(), json=BODY)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "business_owner_reads_only"


def test_update_route_and_not_found(client, monkeypatch):
    c, _pool, _seen = client
    got: dict = {}

    async def fake_update(pool, incident_id, **kw):
        got.update(kw, incident_id=incident_id)
        return incidents.view(_inc(status="resolved", resolved_at=NOW))

    monkeypatch.setattr(incidents, "update", fake_update)
    resp = c.post("/admin/overview/incidents/7", headers=_h(), json={"status": "resolved", "note": "zz fixed"})
    assert resp.status_code == 200 and got["incident_id"] == 7 and got["status"] == "resolved"
    assert c.post("/admin/overview/incidents/7", headers=_h("viewer"), json={"note": "zz"}).status_code == 403
    monkeypatch.setattr(incidents, "detail", AsyncMock(return_value=None))
    assert c.get("/admin/overview/incidents/8", headers=_h()).status_code == 404


def test_tenants_route_refuses_a_bad_filter(client):
    c, _pool, _seen = client
    resp = c.get("/admin/overview/status/tenants?tenant=zz-not-an-id", headers=_h())
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "bad_tenant"
    assert c.get("/admin/overview/status/tenants", headers=_h("content")).status_code == 403


def test_drill_answers_503_on_dev_only(client, monkeypatch):
    from api.services.ops_overview import cloudwatch

    c, _pool, _seen = client
    monkeypatch.setattr(cloudwatch, "environment", lambda: "dev")
    resp = c.get("/admin/overview/status/slo/drill", headers=_h())
    assert resp.status_code == 503 and resp.json()["code"] == "slo_drill"
    assert c.get("/admin/overview/status/slo/drill", headers=_h("content")).status_code == 403
    monkeypatch.setattr(cloudwatch, "environment", lambda: "prod")
    assert c.get("/admin/overview/status/slo/drill", headers=_h()).status_code == 404


def test_policy_entries():
    from api.core.staff_authz import MACHINE_ROUTES, OPS_READ, OPS_WRITE, POLICY

    for p in ("", "/{incident_id}"):
        assert POLICY[("POST", "/admin/overview/incidents" + p)] == OPS_WRITE
    for p in ("incidents", "incidents/{incident_id}", "status/tenants", "status/slo/drill"):
        assert POLICY[("GET", "/admin/overview/" + p)] == OPS_READ
        assert ("GET", "/admin/overview/" + p) not in MACHINE_ROUTES


def test_status_answer_carries_the_slo_and_external_sources():
    assert {"slo", "external"} <= set(ops_status.SOURCES)

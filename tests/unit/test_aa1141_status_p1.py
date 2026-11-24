"""AA-1141 (S291) — the P1 features of the Ecosystem status page without a database or AWS: what changed (rows,
annotations, links), ack / snooze / mute (rules, rows in force, the verdict's open count, the routes and who may act),
the freshness footer, the capacity forecast, the cost anomaly banner and saved views. Invented names only; every
parameter is a fixed value."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import ops_status
from api.services.ops_status import acks, anomaly, changes, forecast, pipelines, verdict, views
from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

NOW = datetime(2026, 11, 25, 3, 20, tzinfo=timezone.utc)
RNG = TimeRange("24h", NOW - timedelta(hours=24), NOW, 3600)
RULES = {"reason_min_chars": 20, "ack_hours": 24, "mute_max_days": 7, "snooze_hours": [1, 4],
         "snooze_tomorrow_hour": 8}
REASON = "zz invented reason for the test"
_SECRET = "aa1141-test-secret"
STAFF = "00000000-0000-4000-8000-00000000a141"


# ── what changed ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version,app", [("481", "cis"), ("bk-075", "booking"), ("crm-012", "crm"),
                                         ("tp-031", "tripplanner")])
def test_migration_app(version, app):
    assert changes.migration_app(version) == app


def test_commit_href_only_for_a_sha_of_a_known_repo():
    assert changes.commit_href("cis", "dev-1a2b3c4d5e") == (
        "https://github.com/AdventureAsia365-Ecosys/AA-CIS-App/commit/1a2b3c4d5e")
    assert changes.commit_href("booking", "0f0f0f0").endswith("/AA-Booking/commit/0f0f0f0")
    assert changes.commit_href("cis", "latest") is None and changes.commit_href("nowhere", "1a2b3c4") is None


def test_change_rows_cover_every_type_newest_first_with_links():
    t = NOW - timedelta(hours=1)
    src = {
        "deploy": [{"event_id": 7, "occurred_at": t, "ended_at": t + timedelta(minutes=4), "kind": "deploy",
                    "app": "cis", "subject": "zz-api", "title": "x", "details": '{"image_tag": "dev-abcdef1"}'}],
        "setting": [{"id": "s1", "key": "cis.ops.zz_key", "app": "cis", "label": "Zz limit", "version": 3,
                     "effective_from": t - timedelta(hours=2), "decided_by": None, "proposed_by": "zz-person",
                     "tenant_scoped": False}],
        "migration": [{"version": "bk-090", "description": "zz invented table", "applied_at": t - timedelta(hours=3)}],
        "wave": [{"id": "w1", "name": "Zz wave", "status": "open", "created_by": "zz", "created_at": t - timedelta(
            hours=4), "paused_at": t - timedelta(hours=30), "paused_by": "zz", "closed_at": None}],
        "route": [{"version": 12, "created_at": t - timedelta(hours=5), "created_by": "zz", "action": "preset",
                   "preset": "zz_preset", "reverted_from": None}],
    }
    rows = changes.change_rows(src, RNG)
    assert [r["type"] for r in rows] == ["deploy", "setting", "migration", "wave", "route"]
    d, s, m, w, r = rows
    assert d["href"].endswith("/AA-CIS-App/commit/abcdef1") and d["ref"] == "dev-abcdef1" and d["t2"]
    assert s["href"] == "/admin/platform/settings?key=cis.ops.zz_key" and s["msg"]["version"] == 3
    assert m["app"] == "booking" and m["href"] == "/admin/data-model"
    assert w["id"] == "wave-w1-opened"     # the pause lies outside the range
    assert r["app"] == "shared" and "zz_preset" in r["title"] and r["href"].endswith("tab=routing")


def test_annotations_follow_the_changes_section():
    chg = {"status": "ok", "data": {"rows": [{"id": "a", "t": NOW.isoformat(), "type": "setting", "app": "cis",
                                              "title": "Setting zz"}]}}
    assert ops_status.annotations(chg) == [{"t": NOW.isoformat(), "type": "setting", "app": "cis",
                                            "text": "Setting zz", "id": "a"}]
    assert ops_status.annotations({"status": "unavailable"}) == []


# ── ack / snooze / mute ──────────────────────────────────────────────────────────────────────────────────────────

def test_ack_lasts_the_ack_hours_whatever_until_says():
    assert acks.validate("ack", None, REASON, NOW, RULES) == NOW + timedelta(hours=24)
    assert acks.validate("ack", NOW + timedelta(days=30), REASON, NOW, RULES) == NOW + timedelta(hours=24)


@pytest.mark.parametrize("action,until_h,reason,code", [
    ("ack", 0, "too short", "reason_too_short"),
    ("pause", 1, REASON, "bad_action"),
    ("snooze", None, REASON, "until_missing"),
    ("snooze", -1, REASON, "until_past"),
    ("snooze", 3, REASON, "snooze_not_offered"),          # an until that is no next-morning time (03:20 + 3 h)
    ("snooze", 49, REASON, "snooze_not_offered"),         # further than any "next day's hour"
    ("mute", 24 * 8, REASON, "until_too_far"),
])
def test_refused_acks(action, until_h, reason, code):
    until = None if until_h is None else NOW + timedelta(hours=until_h)
    with pytest.raises(acks.AckError) as e:
        acks.validate(action, until, reason, NOW, RULES)
    assert e.value.code == code


def test_snooze_and_mute_keep_their_end_time():
    tomorrow_8_local = datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)   # 08:00 next day at UTC+7
    assert acks.validate("snooze", tomorrow_8_local, REASON, NOW, RULES) == tomorrow_8_local
    assert acks.validate("mute", NOW + timedelta(days=7), REASON, NOW, RULES) == NOW + timedelta(days=7)


@pytest.mark.parametrize("hours,ok", [(1, True), (4, True), (2, False), (12, False)])
def test_a_snooze_length_must_be_offered_and_ends_on_the_server_clock(hours, ok):
    if ok:
        assert acks.validate("snooze", None, REASON, NOW, RULES, snooze_hours=hours) == NOW + timedelta(hours=hours)
        return
    with pytest.raises(acks.AckError) as e:
        acks.validate("snooze", None, REASON, NOW, RULES, snooze_hours=hours)
    assert e.value.code == "snooze_not_offered" and "1 h, 4 h" in e.value.message
    assert acks.validate("snooze", None, REASON, NOW, {**RULES, "snooze_hours": [1, 4, 12]},
                         snooze_hours=12) == NOW + timedelta(hours=12)


@pytest.mark.parametrize("until", [
    datetime(2026, 11, 26, 1, 7, tzinfo=timezone.utc),       # not on a quarter hour: no clock's 08:00
    datetime(2026, 11, 26, 1, 0, 30, tzinfo=timezone.utc),   # seconds
    datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc),       # 48 h 40 min ahead
])
def test_until_tomorrow_must_look_like_the_next_morning(until):
    with pytest.raises(acks.AckError) as e:
        acks.validate("snooze", until, REASON, NOW, RULES)
    assert e.value.code == "snooze_not_offered"


def test_until_tomorrow_on_a_half_hour_zone_is_accepted():
    india_8 = datetime(2026, 11, 26, 2, 30, tzinfo=timezone.utc)   # 08:00 next day at UTC+5:30
    assert acks.validate("snooze", india_8, REASON, NOW, RULES) == india_8


def test_in_force_keeps_the_newest_open_row_per_item():
    rows = [
        {"item_id": "a", "action": "ack", "until": NOW + timedelta(hours=1), "created_at": NOW - timedelta(hours=2)},
        {"item_id": "a", "action": "mute", "until": NOW + timedelta(days=1), "created_at": NOW - timedelta(hours=1)},
        {"item_id": "b", "action": "snooze", "until": NOW - timedelta(minutes=1), "created_at": NOW - timedelta(
            hours=1)},
        {"item_id": "c", "action": "ack", "until": NOW + timedelta(hours=1), "created_at": NOW, "cleared_at": NOW},
    ]
    got = acks.in_force(rows, NOW)
    assert list(got) == ["a"] and got["a"]["action"] == "mute"


def _item(iid: str, sev: str = "sev2") -> dict:
    return verdict._item(iid, sev, "cis", f"zz {iid}")


def test_an_ack_does_not_cover_an_item_that_got_worse(monkeypatch):
    """A SEV2 "not ready" acked for 24 h, then the health check fails (SEV1, same item id): open again, the page says
    it got worse since the ack, and the verdict is not "ok"."""
    monkeypatch.setattr(verdict, "_collect", lambda s, t, e: ([_item("health-cis", "sev1")], []))
    ack = {"health-cis": {"item_id": "health-cis", "severity": "sev2", "action": "ack",
                          "until": NOW + timedelta(hours=20), "created_at": NOW - timedelta(hours=4),
                          "reason": REASON, "actor": f"admin:{STAFF}"}}
    v = verdict.decide({}, {}, [], ack, {STAFF: "Zz Person"})
    assert v["open"] == 1 and v["set_aside"] == 0 and v["status"] == "attention"
    item = v["attention"][0]
    assert item["status"] == "open" and "ack" not in item
    assert item["escalated"]["from"] == "sev2" and item["escalated"]["ack"]["by_name"] == "Zz Person"


@pytest.mark.parametrize("now_sev,acked_sev,covered", [
    ("sev2", "sev2", True), ("sev3", "sev2", True), ("sev1", "sev3", False), ("sev2", "sev3", False),
    ("sev1", None, True),
])
def test_an_ack_covers_its_own_severity_and_milder(now_sev, acked_sev, covered):
    row = {"item_id": "x", "severity": acked_sev, "action": "mute", "until": NOW + timedelta(days=1),
           "created_at": NOW, "reason": REASON, "actor": "admin"}
    out = acks.apply([_item("x", now_sev)], {"x": row})[0]
    assert (out["status"] == "muted") is covered and ("escalated" in out) is not covered


def test_verdict_sets_acked_items_aside_but_keeps_them_listed(monkeypatch):
    monkeypatch.setattr(verdict, "_collect", lambda s, t, e: ([_item("errors-cis"), _item("p95-cis", "sev3")], []))
    ack = {"errors-cis": {"item_id": "errors-cis", "action": "snooze", "until": NOW + timedelta(hours=1),
                          "created_at": NOW, "reason": REASON, "actor": f"admin:{STAFF}"}}
    v = verdict.decide({}, {}, [], ack, {STAFF: "Zz Person"})
    assert v["open"] == 1 and v["set_aside"] == 1 and v["status"] == "attention"
    snoozed = next(i for i in v["attention"] if i["id"] == "errors-cis")
    assert snoozed["status"] == "snoozed" and snoozed["ack"]["by_name"] == "Zz Person"
    assert snoozed["ack"]["until"] == (NOW + timedelta(hours=1)).isoformat()
    assert v["attention"][0]["id"] == "p95-cis"   # open items first, then the ones set aside
    all_acked = verdict.decide({}, {}, [], {**ack, "p95-cis": {**ack["errors-cis"], "item_id": "p95-cis",
                                                                "action": "ack"}})
    assert all_acked["open"] == 0 and all_acked["status"] == "ok" and len(all_acked["attention"]) == 2


def test_forecast_and_anomaly_items():
    fc = verdict.forecast_items([{"id": "rds_free", "level": "crit", "days_left": 5.0},
                                 {"id": "ai:zz", "label": "Zz conn", "level": "warn", "days_left": 20.0}])
    assert [i["id"] for i in fc] == ["forecast-rds_free"] and fc[0]["severity"] == "sev3"
    an = verdict.anomaly_items([
        {"app": "cis", "route": "anthropic", "hour": "2026-11-25T02:00:00+00:00", "usd": 1.2, "median_usd_h": 0.2,
         "threshold_usd": 0.6, "times_median": 6.0},
        {"app": "cis", "route": "anthropic", "hour": "2026-11-25T03:00:00+00:00", "usd": 0.9, "median_usd_h": 0.2,
         "threshold_usd": 0.6, "times_median": 4.5}])
    assert len(an) == 1 and an[0]["id"] == "anomaly-cis-anthropic" and an[0]["since"].startswith("2026-11-25T03")


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────────────────

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
        return {"verdict": {"attention": [_item("errors-cis")]}, "viewer": {"can_ack": kw.get("can_ack")}}

    monkeypatch.setattr(ops_status, "build_status", fake_build)
    seen: dict = {}

    async def fake_set(pool, **kw):
        seen.update(kw)
        return {"ack_id": 1, "item_id": kw["item"]["id"], "action": kw["action"], "state": "acked",
                "until": NOW.isoformat(), "at": NOW.isoformat()}

    monkeypatch.setattr(acks, "set_ack", fake_set)
    yield TestClient(app), pool, seen


def _h(role: str | None = "admin") -> dict:
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": STAFF}
    if role:
        h["x-admin-role"] = role
    return h


@pytest.mark.parametrize("role,status", [("admin", 200), ("content", 403), ("viewer", 403), ("finance", 403)])
def test_ack_route_policy(client, role, status):
    c, _pool, seen = client
    resp = c.post("/admin/overview/status/acks", headers=_h(role),
                  json={"item_id": "errors-cis", "action": "ack", "reason": REASON})
    assert resp.status_code == status, resp.text
    if status == 200:
        assert seen["actor"] == f"admin:{STAFF}" and seen["item"]["id"] == "errors-cis"


def test_ack_route_refuses_the_business_owner_and_an_unknown_item(client):
    c, pool, _seen = client
    resp = c.post("/admin/overview/status/acks", headers=_h(),
                  json={"item_id": "nowhere-zz", "action": "ack", "reason": REASON})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "no_such_item"
    pool.fetchval = AsyncMock(return_value=True)
    resp = c.post("/admin/overview/status/acks", headers=_h(),
                  json={"item_id": "errors-cis", "action": "ack", "reason": REASON})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "business_owner_reads_only"
    pool.fetchval = AsyncMock(side_effect=OSError("zz down"))
    assert c.post("/admin/overview/status/acks", headers=_h(),
                  json={"item_id": "errors-cis", "action": "ack", "reason": REASON}).status_code == 503


def test_ack_route_answers_422_on_a_short_reason(client, monkeypatch):
    c, _pool, _seen = client

    async def refuse(pool, **kw):
        raise acks.AckError("reason_too_short", "Write a reason of at least 20 characters.")

    monkeypatch.setattr(acks, "set_ack", refuse)
    resp = c.post("/admin/overview/status/acks", headers=_h(),
                  json={"item_id": "errors-cis", "action": "ack", "reason": "short"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "reason_too_short"


def test_status_tells_the_page_who_may_ack(client):
    c, pool, _seen = client
    assert c.get("/admin/overview/status", headers=_h()).json()["viewer"]["can_ack"] is True
    pool.fetchval = AsyncMock(return_value=True)
    assert c.get("/admin/overview/status", headers=_h()).json()["viewer"]["can_ack"] is False


def test_saved_views_need_a_staff_id(client):
    c, _pool, _seen = client
    resp = c.get("/admin/overview/views", headers={"X-Admin-Secret": _SECRET, "x-admin-role": "admin"})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "no_staff_id"


def test_policy_entries():
    from api.core.staff_authz import MACHINE_ROUTES, OPS_READ, OPS_WRITE, POLICY

    assert POLICY[("POST", "/admin/overview/status/acks")] == OPS_WRITE
    assert POLICY[("POST", "/admin/overview/status/acks/{item_id}/clear")] == OPS_WRITE
    for m, p in (("GET", "/admin/overview/views"), ("POST", "/admin/overview/views"),
                 ("DELETE", "/admin/overview/views/{view_id}")):
        assert POLICY[(m, p)] == OPS_READ and (m, p) not in MACHINE_ROUTES


# ── freshness footer ─────────────────────────────────────────────────────────────────────────────────────────────

def test_pipelines_mark_stale_unknown_and_not_measured():
    delivery = {"status": "ok", "data": {"repos_as_of": (NOW - timedelta(hours=1)).isoformat(), "repos_note": None,
                                         "guards": [{"seen_at": (NOW - timedelta(hours=80)).isoformat()}]}}
    rows = {r["key"]: r for r in pipelines.rows(NOW - timedelta(minutes=20), None, delivery, NOW, 3.0)}
    assert rows["sampler"]["state"] == "stale"            # 20 min > 3 x 5 min
    assert rows["digest"]["state"] == "unknown"           # never ran
    assert rows["guards"]["state"] == "stale"             # 80 h > 3 x 24 h
    assert rows["delivery"]["state"] == "ok"
    assert rows["schema_dump"]["state"] == "not_measured" and rows["schema_dump"]["reason"]
    fresh = {r["key"]: r for r in pipelines.rows(NOW - timedelta(minutes=4), NOW - timedelta(hours=5), delivery,
                                                 NOW, 3.0)}
    assert fresh["sampler"]["state"] == "ok" and fresh["digest"]["state"] == "ok"


# ── capacity forecast ────────────────────────────────────────────────────────────────────────────────────────────

LIM = {"warn": 30.0, "crit": 7.0}


@pytest.mark.parametrize("days,level", [(None, "ok"), (40.0, "ok"), (30.0, "warn"), (7.0, "crit"), (0.0, "crit")])
def test_forecast_levels(days, level):
    assert forecast.level_of(days, LIM) == level


def test_trend_is_a_least_squares_line():
    pts = [(NOW - timedelta(days=d), 20.0 - (6 - d) * 0.5) for d in range(7)]   # loses 0.5 GiB a day
    tr = forecast.trend(pts)
    assert tr["slope_per_day"] == pytest.approx(-0.5) and tr["latest"] == 17.0 and tr["points"] == 7
    assert forecast.trend(pts[:1]) is None


def test_storage_rows_follow_the_autoscaling_ceiling_when_known():
    tr = {"slope_per_day": -0.5, "latest": 14.0}
    row = forecast.storage_rows(tr, {"allocated_gib": 20, "max_allocated_gib": 100}, LIM)[0]
    # warn = 20 % of 20 GiB = 4 GiB: (14 - 4) / 0.5 = 20 days; ceiling: used 6 GiB, (100 - 6) / 0.5 = 188 days
    assert row["days_to_warn"] == 20.0 and row["days_to_ceiling"] == 188.0
    assert row["basis"] == "autoscaling_ceiling" and row["level"] == "ok"
    fixed = forecast.storage_rows(tr, {"allocated_gib": 20, "max_allocated_gib": None}, LIM)[0]
    assert fixed["days_left"] == 20.0 and fixed["level"] == "warn"
    growing = forecast.storage_rows({"slope_per_day": 0.2, "latest": 14.0}, {"allocated_gib": 20}, LIM)[0]
    assert growing["days_left"] is None and growing["level"] == "ok"
    assert forecast.storage_rows(None, None, LIM)[0]["status"] == "not_measured"


def test_ai_rows_reach_the_cap_this_month_or_not():
    now = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc)   # 10 days into a 30-day month
    rows = forecast.ai_rows([
        {"connection_key": "zz-a", "label": "Zz A", "monthly_budget_usd": 100, "month_spent_usd": 50},  # 5 USD/day
        {"connection_key": "zz-b", "label": "Zz B", "monthly_budget_usd": 100, "month_spent_usd": 10},  # 1 USD/day
        {"connection_key": "zz-c", "label": "Zz C", "monthly_budget_usd": 10, "month_spent_usd": 12},
        {"connection_key": "zz-d", "label": "Zz D", "monthly_budget_usd": None, "month_spent_usd": 3},
    ], now, LIM)
    by = {r["connection_key"]: r for r in rows}
    assert set(by) == {"zz-a", "zz-b", "zz-c"}
    assert by["zz-a"]["days_left"] == 10.0 and by["zz-a"]["level"] == "warn" and by["zz-a"]["forecast_usd"] == 150.0
    assert by["zz-b"]["days_left"] is None and by["zz-b"]["level"] == "ok"
    assert by["zz-c"]["days_left"] == 0.0 and by["zz-c"]["level"] == "crit"


def test_actions_cache_is_not_measured():
    row = forecast.actions_cache_row()
    assert row["status"] == "not_measured" and "GitHub" in row["reason"]


# ── cost anomaly ─────────────────────────────────────────────────────────────────────────────────────────────────

POLICY = {"factor": 3.0, "baseline_days": 7, "floor_usd_h": 0.1}


def _hour(h_ago: int) -> datetime:
    return anomaly.hour_floor(NOW) - timedelta(hours=h_ago)


def test_anomaly_flags_an_hour_above_n_times_the_median_per_app_provider_route():
    rows = [{"hour": _hour(h), "app": "cis", "provider": "anthropic", "account": None, "usd": 0.2}
            for h in range(2, 7 * 24 + 2)]
    rows += [{"hour": _hour(1), "app": "cis", "provider": "anthropic", "account": None, "usd": 0.7},
             {"hour": _hour(0), "app": "cis", "provider": "anthropic", "account": None, "usd": 0.5},
             # another route of the same app: usually idle, judged by the floor
             {"hour": _hour(0), "app": "cis", "provider": "bedrock", "account": "zz-acct", "usd": 0.05},
             {"hour": _hour(0), "app": "tripplanner", "provider": "bedrock", "account": "zz-acct", "usd": 0.3}]
    out = anomaly.anomalies(rows, NOW, POLICY)
    got = {(r["app"], r["route"], r["partial"]): r for r in out["rows"]}
    assert set(got) == {("cis", "anthropic", False), ("tripplanner", "bedrock:zz-acct", True)}
    a = got[("cis", "anthropic", False)]
    assert (a["median_usd_h"], a["threshold_usd"], a["times_median"]) == (0.2, 0.6, 3.5)
    assert got[("tripplanner", "bedrock:zz-acct", True)]["times_median"] is None   # median 0: above the floor
    assert out["groups"] == 3 and out["baseline_days"] == 7


def test_anomaly_window():
    base, checked, end = anomaly.window(NOW, 7)
    assert end - checked == timedelta(hours=2) and checked - base == timedelta(days=7)


# ── saved views ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_saved_view_query_keeps_only_page_keys():
    assert views.clean_query({"range": "6h", "apps": "cis,crm", "sort": "att:since:desc", "from": "",
                              "to": None}) == {"range": "6h", "apps": "cis,crm", "sort": "att:since:desc"}
    for bad in ({"evil": "1"}, {"range": "<script>"}, {"hide": "x" * 700}, ["6h"]):
        with pytest.raises(views.ViewError):
            views.clean_query(bad)
    assert views.clean_name("  Wave   watch ") == "Wave watch"
    with pytest.raises(views.ViewError):
        views.clean_name("   ")


def test_threshold_snapshot_carries_the_p1_rules():
    snap = th.snapshot()
    assert snap["acks"]["reason_min_chars"] == 20 and snap["acks"]["ack_hours"] == 24
    assert snap["acks"]["snooze_hours"] == [1, 4] and snap["acks"]["snooze_hours_key"] == th.SNOOZE_CHOICES_HOURS
    assert snap["cost_anomaly"]["factor"] == 3.0 and snap["cost_anomaly"]["baseline_days"] == 7
    assert snap["forecast"]["warn"] == 30.0 and snap["forecast"]["crit"] == 7.0


# ── daily digest ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_digest_part_is_the_page_verdict_with_links_to_the_right_view():
    from api.services.ops_status import digest

    items = [_item("errors-cis", "sev2"), {**_item("guard-zz", "sev3"), "app": "shared", "status": "snoozed"}]
    status = {"generated_at": NOW.isoformat(), "verdict": {"status": "attention", "open": 1, "set_aside": 1,
                                                          "unknown": ["jobs"], "attention": items}}
    part = digest.compact(status, "https://admin.zz.example")
    assert part["link"] == "https://admin.zz.example/admin/overview?range=24h&sev=sev2"
    assert part["items"][0]["link"] == "https://admin.zz.example/admin/overview?range=24h&apps=cis&sev=sev2"
    assert part["items"][1]["link"] == "https://admin.zz.example/admin/overview?range=24h&sev=sev3"
    md = "\n".join(digest.markdown(part))
    assert "1 open on the status page" in md and "1 set aside" in md and "(snoozed)" in md
    assert "Not counted (no answer): jobs" in md
    assert "Not available: OSError" in "\n".join(digest.markdown({"error": "OSError"}))
    assert "Not built" in "\n".join(digest.markdown(None))


@pytest.mark.asyncio
async def test_digest_build_never_raises(monkeypatch):
    from api.services.ops_status import digest

    async def boom(*a, **kw):
        raise OSError("zz down")

    monkeypatch.setattr(ops_status, "build_status", boom)
    assert await digest.build(MagicMock(), None) == {"error": "OSError"}


def test_ops12_takes_the_status_part_as_an_input():
    from services.reports import digest as ops12
    from services.reports import generate

    assert "OPS-12" in generate.TAKES_INPUTS and ops12.STATUS_INPUT == "status_page"


# ── review fix: the AI budget read of the forecast is cached and scans the ledger once ────────────────────────────

class _Redis:
    """The few async Redis calls the section cache uses, in memory."""

    def __init__(self) -> None:
        self.d: dict = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    async def delete(self, k):
        self.d.pop(k, None)


ZZ_BUDGET = {"connection_key": "zz-conn", "label": "Zz connection", "monthly_budget_usd": 10.0,
             "month_spent_usd": 2.0, "enabled": True}


@pytest.mark.asyncio
async def test_forecast_budget_read_is_cached_for_the_refresh_time(monkeypatch):
    from api.services import ai_gateway

    calls = []

    async def read(pool):
        calls.append(pool)
        return [ZZ_BUDGET]

    monkeypatch.setattr(ai_gateway, "read_connection_budgets", read)
    redis = _Redis()
    first, second = await forecast._budgets("zz-pool", redis), await forecast._budgets("zz-pool", redis)
    assert first == second == {"status": "ok", "rows": [ZZ_BUDGET]} and len(calls) == 1
    assert any(":forecast_budgets:" in k for k in redis.d)
    assert await forecast._budgets("zz-pool", None) == {"status": "ok", "rows": [ZZ_BUDGET]} and len(calls) == 2


@pytest.mark.asyncio
async def test_forecast_shows_the_ai_row_unavailable_when_the_budget_read_fails(monkeypatch):
    from api.services import ai_gateway

    async def boom(pool):
        raise OSError("zz down")

    monkeypatch.setattr(ai_gateway, "read_connection_budgets", boom)
    monkeypatch.setattr(forecast, "_storage", AsyncMock(return_value={"status": "unavailable", "reason": "zz"}))
    monkeypatch.setattr(forecast.store, "latest", AsyncMock(return_value=[]))
    out = await forecast.collect("zz-pool", None, NOW)
    ai = next(r for r in out["data"]["rows"] if r["id"] == "ai")
    assert ai["status"] == "unavailable" and out["refresh_s"] == forecast.REFRESH_S


@pytest.mark.asyncio
async def test_connection_budgets_scan_this_months_ledger_once(monkeypatch):
    from api.services import ai_gateway
    from shared import cost_guard
    from shared.llm_client import connections as conn_registry

    conn = MagicMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    reg = MagicMock(connection_key="zz-conn", kind="openai", label="Zz", enabled=True, monthly_budget_usd=10.0)
    other = MagicMock(connection_key="zz-free", kind="openai", label="Zz free", enabled=True,
                      monthly_budget_usd=None)
    monkeypatch.setattr(conn_registry, "fetch_connections", AsyncMock(return_value={"zz-conn": reg,
                                                                                    "zz-free": other}))
    monkeypatch.setattr(cost_guard, "read_cost_policies", AsyncMock(return_value={}))
    scans = []

    async def month_spend(c, keys):
        scans.append(list(keys))
        return {"zz-conn": (2.5, 7), "zz-free": (1.0, 3)}

    monkeypatch.setattr(cost_guard, "connection_month_spend", month_spend)
    rows = await ai_gateway.read_connection_budgets(pool)
    assert scans == [["zz-conn", "zz-free"]]
    capped = next(r for r in rows if r["connection_key"] == "zz-conn")
    assert capped["monthly_budget_usd"] == 10.0 and capped["month_spent_usd"] == 2.5
    assert next(r for r in rows if r["connection_key"] == "zz-free")["monthly_budget_usd"] is None

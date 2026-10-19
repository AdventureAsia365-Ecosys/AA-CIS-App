"""S243 UI verify (AA-858 / AA-884) — one health definition, live worker count, live directory readiness, cost net.

No DB, no network: fakes only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import app_health
from api.services import platform_apps as pa

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _traffic(state="ok", reason="Working normally", app="tripplanner", **extra):
    return {"app": app, "state": state, "state_reason": reason, "has_data": state != "unknown", "requests": 74,
            "errors": 0, "error_rate_pct": 0.0, "throttles": 0, "p95_ms": 1600.0, "kind": "lambda",
            "resources": ["aa-tripplanner-dev-browse"], "alarms": {"total": 4, "no_data": 0, "firing": []}, **extra}


def _section(*apps):
    return {"status": "ok", "as_of": NOW.isoformat(), "window_hours": 24, "apps": list(apps)}


# ── overall_status: the one rule ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("probe,traffic,expected", [
    (None, _traffic("ok"), "ok"),                                       # TripPlanner: traffic alone
    (None, _traffic("warn", "2% of requests failed"), "degraded"),
    (None, _traffic("alarm", "1 alarm firing"), "degraded"),
    (None, _traffic("unknown", "No metrics available"), "unknown"),
    (None, None, "unknown"),
    ({"status": "ok", "reason": "fine"}, _traffic("alarm", "1 alarm firing"), "degraded"),
    ({"status": "down", "reason": "/booking/health answered 502"}, _traffic("ok"), "down"),
    ({"status": "unknown", "reason": "timeout"}, _traffic("ok"), "ok"),   # no data is not a problem by itself
    ({"status": "degraded", "reason": "no live worker"}, None, "degraded"),
])
def test_overall_status_is_the_worst_known_signal(probe, traffic, expected):
    out = app_health.overall_status(probe, traffic)
    assert out["status"] == expected and out["reason"]


def test_overall_status_reason_and_basis():
    out = app_health.overall_status({"status": "degraded", "reason": "no live worker"}, _traffic("ok"))
    assert out["reason"] == "no live worker"
    assert out["basis"] == [app_health.PROBE_BASIS, app_health.TRAFFIC_BASIS]
    both_ok = app_health.overall_status({"status": "ok", "reason": "x"}, _traffic("ok"))
    assert both_ok["reason"] == "Health check passes and traffic looks normal"


def test_apply_health_tripplanner_takes_the_overview_signal():
    card = {"id": "tripplanner", "status": "unknown", "probe": None}
    pa.apply_health(card, _section(_traffic("ok")))
    assert card["status"] == "ok" and card["overall"]["basis"] == [app_health.TRAFFIC_BASIS]
    assert card["traffic"]["requests"] == 74 and card["traffic"]["alarms_total"] == 4
    assert card["traffic"]["alarms_firing"] == 0 and card["traffic"]["window_hours"] == 24
    # traffic unavailable (no Redis / AWS error): unknown, with no traffic block
    card = {"id": "tripplanner", "status": "unknown", "probe": None}
    pa.apply_health(card, {"status": "unavailable", "reason": "cache_offline", "message": "m"})
    assert card["status"] == "unknown" and card["traffic"] is None


@pytest.mark.asyncio
async def test_build_passes_the_given_traffic_section(monkeypatch):
    monkeypatch.setattr(pa, "_cis_card", AsyncMock(return_value={"id": "cis", "probe": {"status": "ok",
                                                                                          "reason": "fine"}}))
    monkeypatch.setattr(pa, "_booking_card", AsyncMock(return_value={"id": "booking", "probe": {
        "status": "down", "reason": "/booking/health answered 502"}}))
    monkeypatch.setattr(pa, "_tripplanner_card", AsyncMock(return_value={"id": "tripplanner", "probe": None}))
    fetch = AsyncMock(side_effect=AssertionError("must reuse the given section"))
    monkeypatch.setattr(app_health, "traffic_section", fetch)
    section = _section(_traffic("ok", app="cis"), _traffic("ok", app="booking"),
                       _traffic("warn", "x", app="tripplanner"))
    out = await pa.collect_apps(MagicMock(), None, traffic=section)
    status = {a["id"]: a["status"] for a in out["apps"]}
    assert status == {"cis": "ok", "booking": "down", "tripplanner": "degraded"}
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_overview_tiles_carry_the_apps_page_status(monkeypatch):
    from api.services import ops_overview

    health = _section(_traffic("ok", app="cis"), _traffic("ok", app="tripplanner"))
    apps = {"apps": [{"id": "cis", "overall": {"status": "degraded", "reason": "no live worker", "basis": []}}]}
    collect = AsyncMock(return_value=apps)
    monkeypatch.setattr(pa, "collect_apps", collect)
    await ops_overview._attach_overall(MagicMock(), "redis", health)
    tiles = {a["app"]: a["overall"] for a in health["apps"]}
    assert tiles["cis"]["status"] == "degraded"                   # the Apps page's answer, not traffic alone
    assert tiles["tripplanner"]["status"] == "ok"                 # missing card: traffic alone, same rule
    assert collect.await_args.kwargs["traffic"] is health
    # a failing Apps build still gives every tile a status
    monkeypatch.setattr(pa, "collect_apps", AsyncMock(side_effect=RuntimeError("db")))
    health = _section(_traffic("alarm", "1 alarm firing", app="cis"))
    await ops_overview._attach_overall(MagicMock(), None, health)
    assert health["apps"][0]["overall"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_booking_own_health_block_survives_the_verdict():
    import httpx

    def handler(request):
        return httpx.Response(200, json={"status": "ok", "git_sha": "b" * 40, "db": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        card = await pa._booking_card(c)
    pa.apply_health(card, _section(_traffic("ok", app="booking")))
    assert card["health"] == {"http_status": 200, "status": "ok"}       # /booking/health, not the verdict
    assert card["overall"]["status"] == "ok" and card["probe"]["status"] == "ok"


def test_tripplanner_note_has_no_internal_codes():
    import inspect

    src = inspect.getsource(pa._tripplanner_card)
    assert "GĐ" not in src and "S241" not in src


# ── finding 2: workers alive now ───────────────────────────────────────────────────────────────────────────────────

def test_worker_counts_alive_only_and_jobs_not_kinds():
    from api.routers.admin_overview import worker_counts

    health = {"workers": [{"alive": True}] + [{"alive": False}] * 19,
              "queued": [{"kind": "a", "n": 3}, {"kind": "b", "n": 2}],
              "running": [{"worker_id": "w", "kind": "a", "n": 4}]}
    assert worker_counts(health) == (1, 5, 4)
    assert worker_counts({"workers": [], "queued": [], "running": []}) == (0, 0, 0)


# ── finding 3: live directory readiness ────────────────────────────────────────────────────────────────────────────

def test_readiness_from_rows_counts_active_rows_only():
    from services import staff_reconcile as sr

    rows = [
        {"username": "nghiep", "is_active": True, "email": "n@adventure.asia", "cognito_sub": "s1"},
        {"username": "thu", "is_active": True, "email": "t@adventure.asia", "cognito_sub": None},
        {"username": "trang", "is_active": False, "email": None, "cognito_sub": None},   # disabled after the check
    ]
    r = sr.readiness_from_rows(rows)
    assert r["active_rows"] == 2 and r["missing_email"] == [] and r["missing_cognito_sub"] == ["thu"]
    assert r["ready"] is False


@pytest.mark.asyncio
async def test_health_adds_live_readiness(monkeypatch):
    from services import staff_reconcile as sr
    from services.jobs import staff_reconcile_job as job

    t = datetime(2026, 10, 19, 7, 38, tzinfo=timezone.utc)
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"bucket": "succeeded", "id": "s", "status": "succeeded", "created_at": t, "started_at": t,
         "finished_at": t, "result": '{"drift_total": 1, "readiness": {"active_rows": 6}}', "error": None}])
    monkeypatch.setattr(sr, "load_directory", AsyncMock(return_value=(
        [{"username": "a", "is_active": True, "email": "a@adventure.asia", "cognito_sub": "x"},
         {"username": "trang", "is_active": False, "email": None, "cognito_sub": None}], [])))
    out = await job.health(pool)
    assert out["latest"]["result"]["readiness"]["active_rows"] == 6          # the stored report, unchanged
    assert out["readiness_now"]["active_rows"] == 1 and out["readiness_now"]["ready"] is True
    assert out["readiness_now"]["as_of"]
    monkeypatch.setattr(sr, "load_directory", AsyncMock(side_effect=OSError("db")))
    assert (await job.health(pool))["readiness_now"] is None

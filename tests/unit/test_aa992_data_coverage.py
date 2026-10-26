"""AA-992 — GET /admin/data-coverage (api/services/data_coverage): registry completeness, the status maths per
polarity, one failing source turning only its rows n/a, the error mapping, the tiers' cache (TTL, last good, refresh
throttle), the CRM tier (Cognito only, own key), the no-ids guard on the response, and the route policy (admin only).
The SQL itself runs on PostgreSQL in tests/integration/test_aa992_data_coverage_sql.py."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

import api.services.data_coverage.build as b
from api.services.data_coverage import registry as reg
from api.services.data_coverage import sources as src

ROOT = Path(__file__).resolve().parents[2]
_SECRET = "aa992-test-admin-secret"


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    async def delete(self, key):
        self.store.pop(key, None)


def _ok(**values) -> dict:
    return {"status": "ok", "reason": None, "timing_ms": 3, "tier": "fast", "values": values}


def _fail(status="error", reason="RuntimeError") -> dict:
    return {"status": status, "reason": reason, "timing_ms": 3, "tier": "fast", "values": {}}


def _row(**kw) -> reg.Row:
    base = dict(id="x.y", group="raw", label="X", value=reg.f("s", "v"), kind="must", sources=("raw",))
    base.update(kw)
    return reg.Row(**base)


# ── registry ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_every_row_is_complete_and_unique():
    groups = {g for g, _ in reg.GROUPS}
    ids = [r.id for r in reg.ROWS]
    assert len(ids) == len(set(ids))
    for r in reg.ROWS:
        assert r.group in groups, r.id
        assert r.label and r.kind in ("must", "backlog", "info") and r.sources, r.id
        assert r.polarity in ("coverage", "defect", "threshold", "equal"), r.id
        assert set(r.sources) <= set(reg.SOURCE_BY_KEY), r.id
        if r.polarity == "threshold":
            assert r.limit is not None, r.id
        if r.polarity in ("coverage", "equal") and r.kind != "info":
            assert r.denominator is not None, f"{r.id}: a coverage target needs a denominator"
        if r.kind == "backlog":
            assert r.issue, f"{r.id}: a backlog row names the work (issue)"
    assert all(g in {r.group for r in reg.ROWS} for g in groups), "every group has rows"


def test_every_source_has_a_function_and_every_source_is_read():
    assert set(src.FUNCTIONS) == set(reg.SOURCE_BY_KEY)
    read = {k for r in reg.ROWS for k in r.sources}
    assert read == set(reg.SOURCE_BY_KEY)


def test_rows_read_only_their_declared_sources():
    """Evaluating a row with only its declared sources present never asks for another one."""
    for r in reg.ROWS:
        asked: set[str] = set()

        def g(s, name, asked=asked):
            asked.add(s)
            return 1
        r.value(g)
        if r.denominator:
            r.denominator(g)
        if r.expected:
            r.expected(g)
        assert asked <= set(r.sources), f"{r.id} reads {asked - set(r.sources)} without declaring it"


def test_every_drill_link_is_an_admin_nav_route():
    nav = (ROOT / "frontend" / "app" / "_kit" / "adminNav.tsx").read_text(encoding="utf-8")
    hrefs = set(re.findall(r'href: "([^"]+)"', nav))
    for r in reg.ROWS:
        if r.drill is not None:
            assert r.drill.split("?")[0] in hrefs, f"{r.id}: {r.drill} is not an admin nav route"


# ── status maths ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _eval(row, **sources):
    full = {"raw": _ok(), **sources}
    return b.evaluate(row, full, {"fast": "2026-10-26T00:00:00+00:00"})


def test_coverage_ok_gap_and_expected_gap():
    row = _row(value=reg.f("raw", "v"), denominator=reg.f("raw", "d"))
    out = _eval(row, raw=_ok(v=8, d=10))
    assert (out["missing"], out["pct"], out["status"]) == (2, 80.0, "gap")
    row = _row(value=reg.f("raw", "v"), denominator=reg.f("raw", "d"), expected=reg.f("raw", "e"))
    assert _eval(row, raw=_ok(v=8, d=10, e=2))["status"] == "ok"
    assert _eval(row, raw=_ok(v=7, d=10, e=2))["status"] == "gap"
    assert _eval(row, raw=_ok(v=10, d=10, e=0))["status"] == "ok"


def test_defect_threshold_equal_and_info():
    defect = _row(value=reg.f("raw", "v"), polarity="defect", denominator=reg.f("raw", "d"))
    assert (_eval(defect, raw=_ok(v=3, d=50))["missing"], _eval(defect, raw=_ok(v=0, d=50))["status"]) == (3, "ok")
    thr = _row(value=reg.f("raw", "v"), polarity="threshold", limit=30, kind="backlog", issue="x")
    assert (_eval(thr, raw=_ok(v=28))["status"], _eval(thr, raw=_ok(v=31))["missing"]) == ("ok", 1)
    eq = _row(value=reg.f("raw", "v"), polarity="equal", denominator=reg.f("raw", "d"))
    assert _eval(eq, raw=_ok(v=12, d=10))["missing"] == 2 and _eval(eq, raw=_ok(v=10, d=10))["status"] == "ok"
    info = _row(value=reg.f("raw", "v"), kind="info", denominator=reg.f("raw", "d"))
    out = _eval(info, raw=_ok(v=1, d=4))
    assert (out["status"], out["pct"]) == ("info", 25.0)


def test_failed_source_is_na_with_its_reason_and_expected_failure_is_named():
    row = _row(value=reg.f("raw", "v"), denominator=reg.f("raw", "d"))
    out = _eval(row, raw=_fail("unavailable", "contract_view_missing"))
    assert (out["status"], out["value"], out["reason"]) == ("na", None, "unavailable: contract_view_missing")
    mixed = _row(value=reg.f("raw", "v"), denominator=reg.f("raw", "d"), expected=reg.f("slowsrc", "e"),
                 sources=("raw", "seg_classify"))
    out = _eval(mixed, raw=_ok(v=8, d=10), slowsrc=_fail("timeout", "timeout"))
    assert out["status"] == "na" and out["reason"].startswith("expected gap unavailable")
    assert out["value"] == 8 and out["missing"] == 2   # the numbers still show
    nodata = _row(value=reg.f("raw", "missing_field"))
    assert _eval(nodata, raw=_ok())["reason"] == "no_data_yet"


def test_real_rows_on_known_numbers():
    by_id = {r.id: r for r in reg.ROWS}
    sources = {"atoms": _ok(live=892, with_atoms=892, empty_marker_only=0, atoms=25878, place_atoms=25878,
                            atoms_older_than_master=0),
               "seg": _ok(with_place_atoms=892, with_segment=883, member_rows=27712, rows_on_dead_atoms=10547),
               "seg_classify": _ok(all_excluded_tours=9, unexplained=228, atoms_without_member=9008,
                                   excluded_by_rule=8780),
               "s1": _ok(eligible=917, with_master=892, never_written=0, in_review_no_master=25,
                         written_no_master_other=0)}
    ev = {k: b.evaluate(by_id[k], sources, {}) for k in ("atoms.tours", "seg.tours", "seg.dead_member_rows",
                                                         "seg.unexplained_atoms", "master.active")}
    assert ev["atoms.tours"]["status"] == "ok"
    assert (ev["seg.tours"]["missing"], ev["seg.tours"]["expected_gap"], ev["seg.tours"]["status"]) == (9, 9, "ok")
    assert ev["seg.dead_member_rows"]["status"] == "gap" and ev["seg.unexplained_atoms"]["status"] == "gap"
    assert ev["master.active"]["status"] == "ok"   # 25 missing, all in review
    sources["s1"] = _ok(eligible=917, with_master=891, never_written=1, in_review_no_master=25,
                        written_no_master_other=0)
    assert b.evaluate(by_id["master.active"], sources, {})["status"] == "gap"   # critic item 1


# ── sources ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("exc,status,reason", [
    (asyncpg.exceptions.UndefinedTableError("x"), "unavailable", "contract_view_missing"),
    (asyncpg.exceptions.InsufficientPrivilegeError("x"), "unavailable", "contract_view_missing"),
    (asyncio.TimeoutError(), "timeout", "timeout"),
    (asyncpg.exceptions.QueryCanceledError("x"), "timeout", "timeout"),
    (src.Unavailable("catalog_unavailable: busy"), "unavailable", "catalog_unavailable: busy"),
    (RuntimeError("boom secret text"), "error", "RuntimeError"),
])
async def test_run_source_maps_errors(monkeypatch, exc, status, reason):
    async def fail(ctx):
        raise exc
    monkeypatch.setitem(src.FUNCTIONS, "raw", fail)
    out = await src.run_source("raw", src.Ctx(None), "fast")
    assert (out["status"], out["reason"], out["values"]) == (status, reason, {})


@pytest.mark.asyncio
async def test_in_drops_pivots_known_reasons_and_hides_insert_failure_text(monkeypatch):
    async def rows(ctx, sql, *args):
        return [{"reason": "empty_itinerary", "n": 3}, {"reason": "not_a_tour", "n": 1},
                {"reason": "duplicate key value violates unique constraint", "n": 2},
                {"reason": "value too long for type", "n": 1}]
    monkeypatch.setattr(src, "_cis_rows", rows)
    out = await src.in_drops(src.Ctx(None))
    assert out["empty_itinerary"] == 3 and out["not_a_tour"] == 1 and out["by_design"] == 4
    assert out["insert_failed"] == 3
    assert all(not k.startswith(("duplicate key", "value too")) for k in out)


@pytest.mark.asyncio
async def test_seg_classify_counts_all_excluded_tours(monkeypatch):
    def atom(tid, place, action, has_seg, aid):
        return {"atom_id": aid, "tour_id": tid, "itinerary_day": 1, "place": place, "action": action,
                "activity_type": None, "tour_has_segment": has_seg}
    rows = [atom("t1", "Airport", "transfer", False, "a1"), atom("t1", "", "rest", False, "a2"),   # all excluded
            atom("t2", "Temple", "visit", False, "a3"), atom("t2", "Hotel", "transfer", False, "a4"),  # one unexplained
            atom("t3", "Market", "visit", True, "a5")]                                                # tour has segment

    async def fetch(ctx, sql, *args):
        return rows
    monkeypatch.setattr(src, "_cis_rows", fetch)
    from services.acp_contract import segment_matching
    monkeypatch.setattr(segment_matching, "forms_segment", lambda a: a.action != "transfer")
    out = await src.seg_classify(src.Ctx(None))
    assert out["atoms_without_member"] == 5 and out["unexplained"] == 2
    assert out["excluded_by_rule"] == 3          # 2 transfer + 1 unnamed
    assert out["all_excluded_tours"] == 1        # t1 only


@pytest.mark.asyncio
async def test_crm_source_needs_a_cognito_token_and_compares_as_of_the_last_pass(monkeypatch):
    with pytest.raises(src.Unavailable, match="cognito_session_required"):
        await src.crm(src.Ctx(None, authorization=None))
    from api.services import crm_console, platform_apps
    monkeypatch.setattr(platform_apps, "_cognito_bearer", lambda a: "tok" if a else None)
    passed = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    call = AsyncMock(return_value=(200, {
        "as_of": passed, "agencies": {"active": 3, "locked": 1, "offboarding": 0, "offboarded": 0, "synthetic": 2},
        "members": {"active": 5, "invited": 1, "disabled": 0},
        "catalog_cache": {"served": 890, "paused": 0, "retired": 1, "last_full_pass_at": passed,
                          "last_ok_at": passed, "failures": 0, "last_error_class": None}}))
    monkeypatch.setattr(crm_console, "crm_ops_call", call)
    monkeypatch.setattr(src, "_cis_row", AsyncMock(return_value={"live": 892, "published_after": 2}))
    out = await src.crm(src.Ctx(None, authorization="Bearer x"))
    assert call.await_args.args[:2] == ("GET", "/data-coverage")
    assert out["cache_age_min"] in (29, 30, 31) and out["published_after_pass"] == 2
    row = {r.id: r for r in reg.ROWS}["crm.cache_served"]
    assert b.evaluate(row, {"crm": _ok(**out)}, {})["status"] == "ok"   # 892 vs 890, 2 published since the pass
    call.return_value = (403, {"detail": "missing_group"})
    with pytest.raises(src.Unavailable, match="crm_missing_group"):
        await src.crm(src.Ctx(None, authorization="Bearer x"))


def test_crm_relay_allows_the_route():
    from api.services import crm_console
    assert crm_console.is_allowed("GET", "/data-coverage") and not crm_console.is_allowed("POST", "/data-coverage")
    assert crm_console._timeout("GET", "/data-coverage") == crm_console.LIST_TIMEOUT_S


# ── build: tiers, cache, failure isolation, no ids ───────────────────────────────────────────────────────────────────
def _fake_functions(monkeypatch, failing=(), calls=None):
    calls = calls if calls is not None else {}

    def make(key):
        async def fn(ctx):
            calls[key] = calls.get(key, 0) + 1
            if key in failing:
                raise RuntimeError("down")
            return {"live": 892, "with_atoms": 892, "empty_marker_only": 0, "atoms": 10, "place_atoms": 5,
                    "atoms_older_than_master": 0, "anything": 1}
        return fn
    for key in list(src.FUNCTIONS):
        monkeypatch.setitem(src.FUNCTIONS, key, make(key))
    return calls


def _walk_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            _walk_keys(v, found)
    elif isinstance(value, list):
        for v in value:
            _walk_keys(v, found)
    return found


@pytest.mark.asyncio
async def test_one_failing_source_makes_only_its_rows_na(monkeypatch):
    _fake_functions(monkeypatch, failing=("raw",))
    out = await b.build(MagicMock(), None, None, authorization=None)
    rows = {r["id"]: r for g in out["groups"] for r in g["rows"]}
    raw_rows = {r.id for r in reg.ROWS if "raw" in r.sources}
    assert all(rows[i]["status"] == "na" for i in raw_rows)
    assert all(rows[r.id]["reason"] == "error: RuntimeError" for r in reg.ROWS if r.sources == ("raw",))
    assert rows["atoms.tours"]["status"] == "ok" and out["live_tours"] == 892
    crm_rows = [r for r in rows.values() if r["id"].startswith("crm.") and r["id"] != "crm.aa_content"]
    assert all(r["reason"] == "unavailable: cognito_session_required" for r in crm_rows)
    assert out["sources"]["raw"]["status"] == "error" and out["summary"]["na"] >= len(raw_rows)
    from api.schemas.data_coverage import DataCoverageResponse
    DataCoverageResponse.model_validate(out)


@pytest.mark.asyncio
async def test_response_carries_no_id_name_or_sample_key(monkeypatch):
    _fake_functions(monkeypatch)
    out = await b.build(MagicMock(), None, None)
    keys = _walk_keys(out) - set(reg.SOURCE_BY_KEY)   # source keys are query names, not data
    bad = {k for k in keys if k.endswith("_id") or k.startswith("sample") or k == "name" or k.endswith("_ids")}
    assert bad == set()
    assert "id" in keys   # the row id (e.g. "atoms.tours") is a registry key, not a data id
    assert all(re.fullmatch(r"[a-z0-9_.]+", r["id"]) for g in out["groups"] for r in g["rows"])


@pytest.mark.asyncio
async def test_tiers_are_cached_with_their_ttl_and_refresh_is_throttled(monkeypatch):
    calls = _fake_functions(monkeypatch)
    redis = FakeRedis()
    await b.build(MagicMock(), None, redis)
    assert calls["raw"] == 1 and calls["seg_classify"] == 1
    fast_key = "admin:data-coverage:fast:v1"
    assert redis.ttl[fast_key] == 60 and redis.ttl["admin:data-coverage:slow:v1"] == 600
    assert "admin:data-coverage:fast:v1:last" in redis.store
    out = await b.build(MagicMock(), None, redis)
    assert calls["raw"] == 1 and out["cached"] is True
    await b.build(MagicMock(), None, redis, refresh=True)           # younger than 15 s: served from the cache
    assert calls["raw"] == 1
    data = json.loads(redis.store[fast_key])
    data["computed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    redis.store[fast_key] = json.dumps(data)
    await b.build(MagicMock(), None, redis, refresh=True)
    assert calls["raw"] == 2 and calls["seg_classify"] == 1       # refresh never recomputes the slow tier


@pytest.mark.asyncio
async def test_partial_tier_is_cached_one_minute_and_not_kept_as_last_good(monkeypatch):
    _fake_functions(monkeypatch, failing=("seg_classify",))
    redis = FakeRedis()
    await b.build(MagicMock(), None, redis)
    assert redis.ttl["admin:data-coverage:slow:v1"] == 60
    assert "admin:data-coverage:slow:v1:last" not in redis.store


@pytest.mark.asyncio
async def test_a_second_caller_during_a_computation_gets_the_last_good_copy(monkeypatch):
    calls = _fake_functions(monkeypatch)
    redis = FakeRedis()
    await b.build(MagicMock(), None, redis)
    del redis.store["admin:data-coverage:fast:v1"]                 # expired, a computation holds the lock
    redis.store["admin:data-coverage:fast:v1:lock"] = "1"
    out = await b.build(MagicMock(), None, redis)
    assert out["stale"] is True and calls["raw"] == 1


@pytest.mark.asyncio
async def test_crm_tier_is_its_own_key_and_never_filled_without_a_token(monkeypatch):
    _fake_functions(monkeypatch)
    redis = FakeRedis()
    await b.build(MagicMock(), None, redis, authorization=None)
    assert not any(k.startswith("admin:data-coverage:crm") for k in redis.store)
    from api.services import platform_apps
    monkeypatch.setattr(platform_apps, "_cognito_bearer", lambda a: "tok" if a else None)
    await b.build(MagicMock(), None, redis, authorization="Bearer t")
    assert redis.ttl["admin:data-coverage:crm:v1"] == 600


@pytest.mark.asyncio
async def test_a_tier_over_its_deadline_is_time_budget(monkeypatch):
    _fake_functions(monkeypatch)

    async def slow(ctx):
        await asyncio.sleep(1)
        return {}
    monkeypatch.setitem(src.FUNCTIONS, "seg_classify", slow)
    monkeypatch.setattr(b, "TIER_DEADLINE_S", 0.05)
    out = await b.build(MagicMock(), None, None)
    assert out["sources"]["seg_classify"]["reason"] == "time_budget"
    assert out["sources"]["raw"]["status"] == "ok"


# ── route policy ─────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_data_coverage

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", MagicMock(), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    build = AsyncMock(return_value={"as_of": "2026-10-26T00:00:00+00:00", "generated_ms": 1, "cached": False,
                                    "stale": False, "live_tours": 892,
                                    "summary": {"rows": 0, "gaps": 0, "backlog": 0, "na": 0},
                                    "groups": [], "sources": {}})
    monkeypatch.setattr(admin_data_coverage.data_coverage, "build_data_coverage", build)
    client = TestClient(app)
    client.build = build
    return client


@pytest.mark.parametrize("role,status", [("admin", 200), ("reviewer", 403), ("content", 403), ("viewer", 403),
                                         (None, 403)])
def test_route_policy(client, role, status):
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa992"}
    if role:
        headers["x-admin-role"] = role
    resp = client.get("/admin/data-coverage", headers=headers)
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json()["live_tours"] == 892


def test_refresh_flag_reaches_the_build(client):
    client.get("/admin/data-coverage?refresh=1", headers={"X-Admin-Secret": _SECRET, "x-admin-role": "admin",
                                                          "x-admin-user-id": "aa992"})
    assert client.build.await_args.kwargs["refresh"] is True


def test_route_needs_the_secret(client):
    assert client.get("/admin/data-coverage", headers={"x-admin-role": "admin"}).status_code in (401, 403)


def test_policy_entry_is_ops_read_and_not_a_machine_route():
    from api.core.staff_authz import MACHINE_ROUTES, OPS_READ, POLICY

    assert POLICY[("GET", "/admin/data-coverage")] == OPS_READ
    assert ("GET", "/admin/data-coverage") not in MACHINE_ROUTES

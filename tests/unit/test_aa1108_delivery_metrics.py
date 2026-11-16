"""AA-1108 — GET /admin/platform/delivery (api/routers/admin_platform_delivery.py, api/services/delivery_metrics.py) and
the DEV-01 / DEV-03 delivery sections (services/reports/delivery.py) on the published GitHub delivery summary.

Admin only (staff_authz POLICY ops.read); the summary read from the docs reader prefix; shape checks (apps without an
id dropped, unknown top-level keys left out); the account-ID mask; 404 / 503 mapping; the cache; the colours from the
cis.delivery.* settings (CIS 433, compiled defaults: no database here); the report sections and KPI lines. The
summary is invented: this repo is public and ships no delivery data.
"""
from __future__ import annotations

import asyncio
import io
import json
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import delivery_metrics as dmx
from api.services import docs_reader
from services.reports import delivery, registry
from services.reports import delivery_settings as ds

SECRET = "the-real-secret"
PATH = "/admin/platform/delivery"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/delivery/summary.json"
NOW = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)


def _week(start: str, **over) -> dict:
    w = {"week": "2026-W44", "start": start, "end": start, "partial": False, "complete": True, "deploys": 4,
         "deploy_attempts": 5, "lead_time_p50_h": 6.0, "lead_time_n": 7, "cfr": 0.2, "failed": 1,
         "recovery_p50_h": 2.0, "recovery_n": 1, "rework_rate": 0.2, "rework": 1, "main_failure_rate": 0.1,
         "main_n": 10, "ci_p95_s": 600, "minutes": 50}
    w.update(over)
    return w


def _app(app_id: str = "lantern", **over) -> dict:
    a = {
        "id": app_id, "name": app_id.title(), "repo": f"{app_id}-repo", "environment": "dev",
        "window": {"start": "2026-10-05T00:00:00Z", "end": "2026-11-02T00:00:00Z", "days": 28},
        "complete_since": "2026-08-01",
        "dora": {
            "deployment_frequency": {"value": 2.5, "n": 10, "attempts": 12, "unit": "per_week"},
            "lead_time": {"p50_h": 30.0, "p90_h": 60.0, "n": 9},
            "change_failure_rate": {"value": 0.1, "n": 12, "failed": 1, "by_reason": {"verify_red": 1}},
            "recovery_time": {"p50_h": 200.0, "p90_h": 200.0, "n": 1, "open": 0},
            "rework_rate": {"value": 0.25, "n": 12, "rework": 3},
        },
        "lead_time_segments": [], "ci": {
            "workflows": [], "slowest": {"name": "CI", "p95_s": 1200, "n": 30},
            "main": {"state": "red", "red_since": "2026-11-01T20:00:00Z", "red_hours": 5.0,
                     "url": "https://github.com/o/r/actions/runs/1", "failure_rate": 0.1, "n": 30},
            "reruns": 0, "minutes": {"total": 10, "coverage": 1.0, "n": 30}},
        "deploys": [], "failed_deploys": [],
        "trend": {"weeks": [_week("2026-10-26")],
                  "months": [{"month": "2026-10", "start": "2026-10-01", "end": "2026-10-31", "partial": False,
                              "complete": True, "deploys": 12, "deploy_attempts": 14, "lead_time_p50_h": 8.0,
                              "lead_time_n": 20, "cfr": 0.1429, "failed": 2, "recovery_p50_h": 3.0, "recovery_n": 2,
                              "rework_rate": 0.0714, "rework": 1, "main_failure_rate": 0.05, "main_n": 40,
                              "failed_deploys": [{"sha": "abc123def456", "started_at": "2026-10-10T03:00:00Z",
                                                  "failed": ["verify_red"], "recovery_h": 1.5}]}]},
    }
    a.update(over)
    return a


def _summary(**over) -> dict:
    s = {"format": 1, "generated_at": "2026-11-02T00:23:00Z", "environment": "dev",
         "source": {"repo": "AA-Ecosys", "sha": "b" * 40, "run_id": "77", "extra": "dropped"},
         "definitions": {"version": "2026-11-16", "rework_hours": 48}, "collection": {"requests": 120},
         "apps": [_app(), {"name": "no id: dropped"}, _app("harbour")], "secret_section": {"x": 1}}
    s.update(over)
    return s


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.calls: list[str] = []

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        value = self.objects[Key]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    dmx.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    dmx.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _use(fake):
    return patch.object(docs_reader, "_s3_client", lambda: fake)


# ── the route ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_policy_is_admin_only():
    assert staff_authz.POLICY[("GET", PATH)] == staff_authz.OPS_READ
    for role in ("viewer", "content", "customer_success", "finance"):
        assert not staff_authz.has_perm(role, staff_authz.OPS_READ), role


def test_non_admin_roles_403_without_storage_reads(client):
    fake = FakeS3({KEY: _summary()})
    with _use(fake):
        assert client.get(PATH, headers={"x-admin-role": "admin"}).status_code == 403
        for role in ("viewer", "content", "tenant"):
            assert client.get(PATH, headers={"X-Admin-Secret": SECRET, "x-admin-role": role}).status_code == 403
    assert fake.calls == []


def test_admin_reads_the_summary_with_colours_and_thresholds(client):
    with _use(FakeS3({KEY: _summary()})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert [a["id"] for a in body["apps"]] == ["lantern", "harbour"]
    assert body["source"] == {"repo": "AA-Ecosys", "sha": "b" * 40, "run_id": "77"}
    assert "secret_section" not in body
    assert body["cache_ttl_s"] == docs_reader.CACHE_TTL_S
    # the starting values (CIS 433 compiled defaults)
    th = body["thresholds"]
    assert th["change_failure_rate"]["green"] == 15 and th["change_failure_rate"]["amber"] == 30
    assert th["deployment_frequency"]["higher_is_better"] is True
    assert th["main_red_hours"] == 2 and th["min_sample"] == 5
    assert body["apps"][0]["status"] == {
        "deployment_frequency": "green",      # 2.5 a week >= 1
        "lead_time": "amber",                 # 30 h: above 24, under 168
        "change_failure_rate": "green",       # 10 % <= 15
        "recovery_time": "few",               # n 1 < 5
        "rework_rate": "red",                 # 25 % > 20
        "ci_p95": "amber",                    # 20 min: above 15, under 30
        "main": "red",                        # red 5 h > 2
    }


def test_account_id_is_masked(client):
    s = _summary()
    s["apps"][0]["name"] = "Lantern 123456789012"
    with _use(FakeS3({KEY: s})):
        body = client.get(PATH, headers=ADMIN).json()
    assert body["apps"][0]["name"] == f"Lantern {docs_reader.ACCOUNT_ID_MARKER}"
    assert "123456789012" not in json.dumps(body)


# A SHA with a standalone 12-digit run between hex letters, as about 1.6 % of real SHAs have one.
SHA_WITH_DIGIT_RUN = "ab" + "123456789012" + "cd" * 13


def test_sha_and_github_links_pass_unmasked(client):
    s = _summary()
    s["source"]["sha"] = SHA_WITH_DIGIT_RUN
    app = s["apps"][0]
    app["deploys"] = [{"sha": SHA_WITH_DIGIT_RUN, "url": "https://github.com/o/r/actions/runs/123456789012"}]
    app["failed_deploys"] = [{"sha": SHA_WITH_DIGIT_RUN, "failed": ["verify_red"]}]
    app["ci"]["main"]["url"] = "https://github.com/o/r/actions/runs/123456789012/job/123456789012"
    app["ci"]["last_red_url"] = "https://github.com/o/r/actions/runs/123456789012"
    app["trend"]["months"][0]["failed_deploys"][0]["sha"] = SHA_WITH_DIGIT_RUN
    with _use(FakeS3({KEY: s})), patch.object(dmx.logger, "error") as err:
        body = client.get(PATH, headers=ADMIN).json()
    out = body["apps"][0]
    assert body["source"]["sha"] == SHA_WITH_DIGIT_RUN
    assert out["deploys"][0] == app["deploys"][0]
    assert out["failed_deploys"][0]["sha"] == SHA_WITH_DIGIT_RUN
    assert out["ci"]["main"]["url"] == app["ci"]["main"]["url"]
    assert out["ci"]["last_red_url"] == app["ci"]["last_red_url"]
    assert out["trend"]["months"][0]["failed_deploys"][0]["sha"] == SHA_WITH_DIGIT_RUN
    assert docs_reader.ACCOUNT_ID_MARKER not in json.dumps(body)
    err.assert_not_called()


def test_digit_run_inside_a_text_token_is_not_masked(client):
    s = _summary()
    s["apps"][0]["name"] = "Lantern build-x123456789012y"
    with _use(FakeS3({KEY: s})):
        body = client.get(PATH, headers=ADMIN).json()
    assert body["apps"][0]["name"] == "Lantern build-x123456789012y"


@pytest.mark.parametrize("key,value", [
    ("sha", "not a sha 123456789012"),
    ("url", "https://example.invalid/123456789012"),
    ("last_url", "see 123456789012"),
])
def test_sha_and_url_keys_mask_values_that_are_not_identifiers(client, key, value):
    s = _summary()
    s["apps"][0]["ci"]["main"][key] = value
    with _use(FakeS3({KEY: s})), patch.object(dmx.logger, "error") as err:
        body = client.get(PATH, headers=ADMIN).json()
    assert "123456789012" not in json.dumps(body)
    assert docs_reader.ACCOUNT_ID_MARKER in body["apps"][0]["ci"]["main"][key]
    err.assert_called_once()


def test_missing_summary_is_404(client):
    with _use(FakeS3({})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 404
    assert "Delivery metrics" in res.json()["detail"]


@pytest.mark.parametrize("stored", [b"not json", {"format": 2, "apps": []}, {"format": 1, "apps": "x"},
                                    RuntimeError("boom")])
def test_bad_summary_or_storage_error_is_503(client, stored):
    with _use(FakeS3({KEY: stored})):
        assert client.get(PATH, headers=ADMIN).status_code == 503


def test_summary_is_cached_and_refresh_respects_the_floor(client):
    fake = FakeS3({KEY: _summary()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert client.get(f"{PATH}?refresh=1", headers=ADMIN).status_code == 200
    assert fake.calls == [KEY]


def test_prefix_follows_the_docs_reader(monkeypatch, client):
    monkeypatch.setenv("DOCS_READER_S3_PREFIX", "other-prefix")
    fake = FakeS3({"other-prefix/delivery/summary.json": _summary()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
    assert fake.calls == ["other-prefix/delivery/summary.json"]


# ── colours ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_band_directions_sample_floor_and_no_value():
    up = {"green": 1, "amber": 0.25, "higher_is_better": True}
    down = {"green": 24, "amber": 168, "higher_is_better": False}
    assert [dmx.band(v, 10, up, 5) for v in (2, 1, 0.5, 0.25, 0.1)] == ["green", "green", "amber", "amber", "red"]
    assert [dmx.band(v, 10, down, 5) for v in (5, 24, 100, 168, 200)] == ["green", "green", "amber", "amber", "red"]
    assert dmx.band(5, 4, down, 5) == "few"
    assert dmx.band(None, 40, down, 5) == "none"


def test_main_state():
    app = _app()
    assert dmx.main_state(app, 2) == "red"
    assert dmx.main_state(app, 6) == "amber"
    app["ci"]["main"]["state"] = "green"
    assert dmx.main_state(app, 2) == "green"
    app["ci"]["main"]["state"] = "unknown"
    assert dmx.main_state(app, 2) == "none"


def test_thresholds_follow_the_settings():
    async def fake_aget(key, tenant_id=None):
        return {ds.CFR_GREEN_PCT: 5, ds.CFR_AMBER_PCT: 9}.get(key, 1)

    with patch.object(dmx.settings, "aget", fake_aget):
        th = asyncio.run(dmx.thresholds())
    assert th["change_failure_rate"]["green"] == 5 and th["change_failure_rate"]["keys"] == [ds.CFR_GREEN_PCT,
                                                                                             ds.CFR_AMBER_PCT]
    assert dmx.grade(_app(), th)["change_failure_rate"] == "red"         # 10 % > 9


def test_every_key_is_read_by_a_band_or_named():
    banded = {k for g, a, _h in ds.BANDS.values() for k in (g, a)}
    assert banded | {ds.MAIN_RED_HOURS, ds.MIN_SAMPLE} == set(ds.ALL)


# ── DEV-01 / DEV-03 ────────────────────────────────────────────────────────────────────────────────────────────

MON, SUN = date(2026, 10, 26), date(2026, 11, 1)
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)


def _th() -> dict:
    return asyncio.run(dmx.thresholds())


def _kpis(out) -> dict:
    return {k["id"]: k for k in out.output["kpis"]}


def test_period_rows_match_the_iso_week_and_the_month():
    s = _summary()
    s["apps"] = [a for a in s["apps"] if "id" in a]
    rows = delivery.period_rows(s, "weeks", MON)
    assert [r["app"] for r in rows] == ["lantern", "harbour"]
    assert delivery.period_rows(s, "weeks", date(2026, 10, 19)) is None
    assert delivery.period_rows(s, "months", OCT1)[0]["row"]["deploys"] == 12
    assert delivery.period_rows({"error": "x"}, "weeks", MON) is None


def test_dev03_shows_delivery_per_app():
    s = _summary()
    s["apps"] = [a for a in s["apps"] if "id" in a]
    facts = {"starts": [], "versions": [], "delivery": s}
    out = delivery.render_dev03(registry.load(), registry.load().report("DEV-03"), MON, SUN, facts, NOW)
    assert "## Delivery per app (GitHub, Dev numbers)" in out.markdown
    assert "| Lantern | 4 (5) | 6 h (7) | 20 % (1) |" in out.markdown
    assert "8 green deploy(s) over 2 apps" in out.summary
    assert [d["app"] for d in out.output["delivery"]] == ["lantern", "harbour"]
    json.dumps(out.output, default=str)


def test_dev03_without_the_summary_says_why():
    facts = {"starts": [], "versions": [], "delivery": {"error": "storage down"}}
    out = delivery.render_dev03(registry.load(), registry.load().report("DEV-03"), MON, SUN, facts, NOW)
    assert "Not readable: storage down" in out.markdown
    assert "GitHub delivery summary not readable" in out.summary


def test_dev01_kpis_from_the_month_rows():
    s = _summary()
    s["apps"] = [a for a in s["apps"] if "id" in a]
    facts = {"starts": [], "weekly": [], "versions": [], "delivery": s, "thresholds": _th()}
    reg = registry.load()
    out = delivery.render_dev01(reg, reg.report("DEV-01"), OCT1, OCT31, facts, NOW)
    assert [k["id"] for k in out.output["kpis"]] == list(reg.report("DEV-01").kpis)
    k = _kpis(out)
    assert k["K-DORA-DF"]["value"] == pytest.approx(round(24 / (31 / 7), 2)) and k["K-DORA-DF"]["status"] == "info"
    assert k["K-DORA-LT"]["value"] == 8.0 and k["K-DORA-LT"]["status"] == "ok"           # 8 h <= 24
    assert k["K-DORA-CFR"]["value"] == pytest.approx(4 / 28, abs=1e-4)
    assert k["K-DORA-CFR"]["status"] == "ok"                                              # 14 % <= 15
    assert k["K-DORA-MTTR"]["value"] == 3.0 and k["K-DORA-MTTR"]["status"] == "info"      # n 2 < 5: too few
    assert k["K-DORA-RW"]["value"] == pytest.approx(2 / 28, abs=1e-4) and k["K-DORA-RW"]["status"] == "ok"
    assert k["K-CI-MAINFAIL"]["value"] == pytest.approx(4 / 80, abs=1e-4)
    assert "## Failed deploys and recovery" in out.markdown and "abc123def456" in out.markdown
    assert "Lead time for changes" not in out.output["not_available"]


def test_dev01_falls_back_to_the_worker_rows_without_the_summary():
    facts = {"starts": [], "weekly": [], "versions": [], "delivery": {"error": "storage down"}, "thresholds": _th()}
    reg = registry.load()
    out = delivery.render_dev01(reg, reg.report("DEV-01"), OCT1, OCT31, facts, NOW)
    k = _kpis(out)
    assert [x["id"] for x in out.output["kpis"]] == list(reg.report("DEV-01").kpis)
    assert {k[i]["status"] for i in ("K-DORA-LT", "K-DORA-MTTR", "K-DORA-RW", "K-CI-MAINFAIL")} == {"not wired"}
    assert "storage down" in k["K-DORA-LT"]["note"]
    assert "Lead time for changes" in out.output["not_available"]

"""AA-1170 — Platform › Compliance API (api/routers/admin_platform_compliance.py, api/services/compliance.py) without a
database: the policy (compliance.read / compliance.write, administrator only; a non-administrator gets 403 before
anything is read), the published evidence file (format, masks, dropped rows, the cache) and the route answers with
the service stubbed. The evidence file is invented; the DB paths are covered by
tests/integration/test_aa1170_compliance_service.py."""
from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import compliance
from api.services import docs_reader

SECRET = "the-real-secret"
BASE = "/admin/platform/compliance"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/compliance/evidence.json"
NOW = datetime(2026, 11, 21, 3, 30, tzinfo=timezone.utc)


def bundle(**over) -> dict:
    b = {"format": 1, "source": "root_daily", "collector": "root compliance-evidence", "collector_version": "a1b2c3d",
         "run_ref": "compliance-evidence #4", "generated_at": "2026-11-21T03:00:00Z",
         "checks": [{"check_id": "repo_branch_protection", "status": "pass", "summary": "6/6 protected",
                     "detail": {"repos": 6}, "link": "https://github.com/ExampleOrg"},
                    {"check_id": "last_restore_drill", "status": "pass", "summary": "ok",
                     "detail": {"last_date": "2026-11-02"}, "link": "https://example.com/not-github"}]}
    b.update(over)
    return b


class FakeS3:
    def __init__(self, objects: dict):
        self.objects, self.calls = objects, []

    def get_object(self, Bucket, Key):   # noqa: N803 - boto3 names
        self.calls.append(Key)
        if Key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        body = json.dumps(self.objects[Key]).encode() if isinstance(self.objects[Key], dict) else self.objects[Key]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    compliance.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    compliance.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


# ── policy ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_policy_is_compliance_capabilities_admin_only():
    P = staff_authz.POLICY
    assert P[("GET", BASE)] == P[("GET", BASE + "/controls")] == P[("GET", BASE + "/controls/{control_id}")] \
        == staff_authz.COMPLIANCE_READ == "compliance.read"
    assert P[("POST", BASE + "/refresh")] == staff_authz.COMPLIANCE_WRITE == "compliance.write"
    holders = {r for r, perms in staff_authz.ROLE_PERMS.items() if {"compliance.read", "compliance.write"} & perms}
    assert holders == {"admin"}


def test_non_admin_gets_403_before_any_read(client):
    fake = FakeS3({KEY: bundle()})
    with patch.object(docs_reader, "_s3_client", lambda: fake), \
            patch.object(compliance, "overview", AsyncMock()) as ov:
        for role in ("viewer", "content", "content_lead", "customer_success", "finance", "ai_ops", "tenant"):
            h = {"X-Admin-Secret": SECRET, "x-admin-role": role}
            assert client.get(BASE, headers=h).status_code == 403, role
            assert client.get(BASE + "/controls/EX-IAM-01", headers=h).status_code == 403, role
            assert client.post(BASE + "/refresh", headers=h).status_code == 403, role
        assert client.get(BASE, headers={"x-admin-role": "admin"}).status_code == 403   # no secret
    assert fake.calls == [] and ov.await_count == 0


# ── the published file ──────────────────────────────────────────────────────────────────────────────────────────────


def test_parse_bundle_keeps_valid_rows_and_cleans_text():
    b = bundle()
    b["checks"].append({"check_id": "Bad Id", "status": "pass"})
    b["checks"].append({"check_id": "repo_branch_protection", "status": "pass"})      # repeated
    b["checks"].append({"check_id": "x_unknown", "status": "great"})
    b["checks"].append({"check_id": "x_leak", "status": "fail", "summary": "account " + "4" * 12,
                        "detail": {"role": "arn:aws:iam::" + "4" * 12 + ":role/x", "n": 2}})
    rows, dropped = compliance.parse_bundle(json.dumps(b).encode(), NOW)
    assert [r["check_id"] for r in rows] == ["repo_branch_protection", "last_restore_drill", "x_leak"]
    assert len(dropped) == 3
    leak = rows[2]
    text = json.dumps(leak, default=str)
    assert "4" * 12 not in text and "arn:aws" not in text and leak["detail"]["n"] == 2
    assert rows[1]["link"] is None and rows[0]["link"] == "https://github.com/ExampleOrg"
    assert rows[0]["collected_at"] == datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
    assert rows[0]["collector_version"] == "a1b2c3d"


@pytest.mark.parametrize("over", [{"format": 2}, {"source": "Bad Source"}, {"generated_at": "yesterday"},
                                  {"generated_at": "2026-11-22T03:00:00Z"}, {"checks": "none"}])
def test_parse_bundle_refuses_a_bad_file(over):
    with pytest.raises(ValueError):
        compliance.parse_bundle(json.dumps(bundle(**over)).encode(), NOW)


def test_oversized_detail_is_dropped():
    b = bundle(checks=[{"check_id": "big_one", "status": "pass", "detail": {"x": "y" * 9000}}])
    rows, dropped = compliance.parse_bundle(json.dumps(b).encode(), NOW)
    assert rows == [] and "8192" in dropped[0]


def test_sync_ingests_once_per_cache_window_and_reports_states():
    fake = FakeS3({KEY: bundle()})
    ingest = AsyncMock(return_value=2)
    with patch.object(docs_reader, "_s3_client", lambda: fake), patch.object(compliance, "ingest", ingest), \
            patch.object(compliance, "_validity_hours", AsyncMock(return_value=72)):
        first = asyncio.run(compliance.sync(object(), now=NOW))
        again = asyncio.run(compliance.sync(object(), now=NOW))
    assert first["state"] == "ok" and first["new"] == 2 and first["rows"] == 2 and again is first
    assert fake.calls == [KEY] and ingest.await_args.args[2] == 72
    compliance.reset_cache()
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3({})):
        assert asyncio.run(compliance.sync(object(), now=NOW))["state"] == "missing"
    compliance.reset_cache()
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3({KEY: b"not json"})):
        assert asyncio.run(compliance.sync(object(), now=NOW))["state"] == "invalid"


def test_sync_reports_unavailable_storage():
    class Broken:
        def get_object(self, Bucket, Key):   # noqa: N803
            raise RuntimeError("denied")
    with patch.object(docs_reader, "_s3_client", lambda: Broken()):
        assert asyncio.run(compliance.sync(object(), now=NOW))["state"] == "unavailable"


def test_score_settings_read_the_compliance_keys():
    values = {"cis.compliance.validity_hours.root_daily": 72, "cis.compliance.collector_heartbeat_hours": 36,
              "cis.compliance.partial_weight": 0.5, "cis.compliance.drill_max_age_days": 90,
              "cis.compliance.access_review_max_age_days": 100, "cis.compliance.docs_overdue_max": 0,
              "cis.compliance.reference_scores": {"iso27001:2022": 45.8, "unknown": 1, "soc2:2017": "x"}}

    async def aget(key, tenant_id=None):
        return values[key]
    with patch.object(compliance.settings, "aget", aget):
        s = asyncio.run(compliance.score_settings())
        refs = asyncio.run(compliance.reference_scores())
    assert s.validity_hours == {"root_daily": 72} and s.heartbeat_hours == 36 and s.partial_weight == 0.5
    assert s.fact_limits["last_access_review"] == ("last_date", 100.0, "age_days")
    assert s.fact_limits["docs_review_overdue"] == ("overdue", 0.0, "max")
    assert refs == {"iso27001:2022": 45.8}


# ── routes (service stubbed) ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pool():
    with patch.object(main.app.state, "pool", object(), create=True):
        yield


def test_admin_routes_answer_with_the_service(client, pool):
    state = {"state": "ok"}
    with patch.object(compliance, "sync", AsyncMock(return_value=state)) as sync, \
            patch.object(compliance, "overview", AsyncMock(return_value={"frameworks": [], "ingest": state})), \
            patch.object(compliance, "controls", AsyncMock(return_value={"controls": []})), \
            patch.object(compliance, "control_detail", AsyncMock(side_effect=compliance.ComplianceMissing("No"))):
        res = client.get(BASE, headers=ADMIN)
        assert res.status_code == 200 and res.json()["ingest"] == state
        assert res.headers["cache-control"] == "private, no-store"
        assert client.get(BASE + "/controls", headers=ADMIN).json() == {"controls": []}
        assert client.get(BASE + "/controls/EX-IAM-01", headers=ADMIN).status_code == 404
        assert client.get(BASE + "/controls/not-an-id", headers=ADMIN).status_code == 422
        r = client.post(BASE + "/refresh", headers=ADMIN)
        assert r.status_code == 200 and r.json() == {"ingest": state}
        assert sync.await_args_list[-1].kwargs == {"refresh": True}

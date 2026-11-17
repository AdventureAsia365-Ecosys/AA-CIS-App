"""AA-1125 — the Merge queue and Git map tabs of Platform › Delivery: GET /admin/platform/delivery?section=queue|git
(api/routers/admin_platform_delivery.py, api/services/delivery_metrics.py) on the published GitHub delivery summary.

The default section stays the AA-1108 view without the two new sections; section=queue serves the apps with a merge
queue, coloured with the cis.delivery.queue_* settings (CIS 445, compiled defaults: no database here) and the shared
min_sample; section=git serves the git activity of every app, no colour; an unknown section is 422; the account-ID
mask still covers the new sections and lets their github.com links through. The summary is invented: this repo is
public and ships no delivery data.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin
from api.services import delivery_metrics as dmx
from api.services import docs_reader
from services.reports import delivery_settings as ds
from shared import settings

SECRET = "the-real-secret"
PATH = "/admin/platform/delivery"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/delivery/summary.json"
REPO_URL = "https://github.com/example-org/lantern-repo"


def _queue(**over) -> dict:
    q = {
        "window": {"start": "2026-10-05T00:00:00Z", "end": "2026-11-02T00:00:00Z"}, "complete_since": "2026-08-01",
        "entries": 22, "prs": 20, "merged": 19, "ejected": 3, "removed": 0, "in_queue": 1,
        "wait": {"p50_h": 0.25, "p95_h": 2.5, "n": 19},
        "to_merge": {"p50_h": 0.3, "p95_h": 3.1, "n": 19},
        "ejection_rate": {"value": 0.1364, "n": 22, "ejected": 3},
        "flaky": {"value": 0.0455, "n": 22, "flaky": 1, "rerun_green": 0, "reentry_green": 1},
        "reasons": [{"reason": "checks_failed", "n": 3, "workflows": {"CI": 3}, "last_pr": 41,
                     "last_url": f"{REPO_URL}/actions/runs/123456789012", "last_at": "2026-11-01T09:00:00Z"}],
        "ci": {"runs": 25, "failures": 3, "failure_rate": 0.12, "p50_s": 600, "p95_s": 900, "reruns": 1,
               "minutes": 310, "workflows": [{"name": "CI", "runs": 25, "failures": 3, "minutes": 310, "reruns": 1}]},
        "throughput": [{"date": "2026-11-01", "merged": 3, "ejected": 1, "removed": 0}],
        "recent": [{"pr": 41, "title": "feat: lantern colours", "url": f"{REPO_URL}/pull/41",
                    "enqueued_at": "2026-11-01T08:40:00Z", "left_at": "2026-11-01T09:00:00Z", "outcome": "ejected",
                    "reason": "checks_failed", "failed_workflows": ["CI"], "wait_h": 0.333, "attempt": 1,
                    "attempts": 2, "runs": 1, "rebuilds": 0, "reruns": 0, "minutes": 12, "flaky": "reentry_green",
                    "new_commit": False, "run_url": f"{REPO_URL}/actions/runs/123456789012"}],
        "flaky_entries": [],
    }
    q.update(over)
    return q


def _git() -> dict:
    sha = "ab" + "1" * 38
    return {
        "branch": "main", "days": 90, "complete_since": "2026-08-04", "commits": 2, "files_coverage": 1.0,
        "daily": [{"date": "2026-11-01", "n": 2}],
        "windows": {"d30": {"commits": 2, "by_kind": {"agent": 1, "human": 1}, "dirs": [
            {"path": "app", "churn": 50, "commits": 2, "files": 1, "by_kind": {"agent": 1, "human": 1},
             "children": [{"path": "app/lanterns", "churn": 50, "commits": 2}]}], "files": [], "merges": 2,
            "merges_by_prefix": {"feat": 2}}},
        "graph": [{"sha": sha, "parents": ["cd" + "2" * 38], "title": "feat: lantern colours (#41) 123456789012",
                   "pr": 41, "url": f"{REPO_URL}/commit/{sha}", "pr_url": f"{REPO_URL}/pull/41", "kind": "agent",
                   "at": "2026-11-01T09:00:00Z", "additions": 40, "deletions": 10, "files": 1}],
    }


def _app(app_id: str, queue: bool = True) -> dict:
    return {
        "id": app_id, "name": app_id.title(), "repo": f"{app_id}-repo", "environment": "dev",
        "window": {"start": "2026-10-05T00:00:00Z", "end": "2026-11-02T00:00:00Z", "days": 28},
        "complete_since": "2026-08-01",
        "dora": {}, "ci": {"main": {"state": "green"}}, "merge_queue": {"wait_p50_h": 0.2, "n": 3},
        "queue": _queue() if queue else None, "git": _git(),
    }


def _summary() -> dict:
    return {"format": 1, "generated_at": "2026-11-02T00:23:00Z", "environment": "dev",
            "source": {"repo": "AA-Ecosys", "sha": "b" * 40, "run_id": "77"},
            "definitions": {"git_days": 90}, "collection": {"requests": 120},
            "apps": [_app("lantern"), _app("harbour", queue=False)]}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = json.dumps(self.objects[Key]).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    dmx.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"), \
            patch.object(docs_reader, "_s3_client", lambda: FakeS3({KEY: _summary()})):
        yield
    dmx.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def test_default_section_leaves_the_new_sections_out(client):
    body = client.get(PATH, headers=ADMIN).json()
    assert body["section"] == "dora"
    for app in body["apps"]:
        assert "queue" not in app and "git" not in app
        assert "status" in app and "merge_queue" in app      # the AA-1108 view is unchanged
    assert "change_failure_rate" in body["thresholds"]


def test_queue_section_serves_the_apps_with_a_merge_queue_and_their_colours(client):
    res = client.get(PATH, params={"section": "queue"}, headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert body["section"] == "queue"
    assert [a["id"] for a in body["apps"]] == ["lantern"]          # harbour has no merge queue
    app = body["apps"][0]
    assert set(app) == {"id", "name", "repo", "environment", "window", "complete_since", "queue", "queue_status"}
    assert app["queue_status"] == {
        "queue_wait": "amber",      # p95 2.5 h: above 1, under 4
        "ejection_rate": "amber",   # 13.64 %: above 10, under 25
        "flaky_rate": "green",      # 4.55 % <= 5
    }
    th = body["thresholds"]
    assert th["queue_wait"] == {"green": 1, "amber": 4, "higher_is_better": False,
                                "keys": [ds.QUEUE_WAIT_GREEN_HOURS, ds.QUEUE_WAIT_AMBER_HOURS]}
    assert th["ejection_rate"]["green"] == 10 and th["flaky_rate"]["amber"] == 15 and th["min_sample"] == 5
    # github.com links pass the account-ID mask unchanged (run IDs reach 12 digits)
    assert app["queue"]["recent"][0]["run_url"].endswith("/actions/runs/123456789012")
    assert app["queue"]["reasons"][0]["last_url"].endswith("/actions/runs/123456789012")


def test_git_section_serves_every_app_and_masks_text(client):
    body = client.get(PATH, params={"section": "git"}, headers=ADMIN).json()
    assert body["section"] == "git" and body["thresholds"] == {}
    assert [a["id"] for a in body["apps"]] == ["lantern", "harbour"]
    g = body["apps"][0]["git"]
    assert g["windows"]["d30"]["dirs"][0]["children"][0]["path"] == "app/lanterns"
    node = g["graph"][0]
    assert node["url"].startswith(f"{REPO_URL}/commit/") and node["pr_url"] == f"{REPO_URL}/pull/41"
    assert node["sha"] == "ab" + "1" * 38 and node["parents"] == ["cd" + "2" * 38]
    assert "123456789012" not in node["title"] and docs_reader.ACCOUNT_ID_MARKER in node["title"]
    assert "queue" not in body["apps"][0] and "dora" not in body["apps"][0]


def test_unknown_section_is_422(client):
    assert client.get(PATH, params={"section": "secrets"}, headers=ADMIN).status_code == 422


def test_non_admin_roles_are_refused_for_every_section(client):
    for section in ("queue", "git"):
        res = client.get(PATH, params={"section": section}, headers={"X-Admin-Secret": SECRET, "x-admin-role": "viewer"})
        assert res.status_code == 403


def test_grade_queue_bands_sample_floor_and_missing_values():
    th = {"queue_wait": {"green": 1, "amber": 4, "higher_is_better": False},
          "ejection_rate": {"green": 10, "amber": 25, "higher_is_better": False},
          "flaky_rate": {"green": 5, "amber": 15, "higher_is_better": False}, "min_sample": 5}
    q = _queue(wait={"p95_h": 6.0, "n": 9}, ejection_rate={"value": 0.3, "n": 3}, flaky={"value": None, "n": 0})
    assert dmx.grade_queue(q, th) == {"queue_wait": "red", "ejection_rate": "few", "flaky_rate": "none"}
    assert dmx.grade_queue({}, th) == {"queue_wait": "none", "ejection_rate": "none", "flaky_rate": "none"}
    assert dmx.queue_values(_queue())["ejection_rate"] == (13.64, 22)


def test_view_refuses_an_unknown_section():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(dmx.view(section="other"))


def test_every_queue_key_is_read_by_a_band():
    banded = {k for g, a, _h in ds.QUEUE_BANDS.values() for k in (g, a)}
    assert banded == set(ds.QUEUE_ALL)
    for key in ds.QUEUE_ALL:
        d = settings.definition(key)
        assert d.area == "delivery" and d.enforced, key

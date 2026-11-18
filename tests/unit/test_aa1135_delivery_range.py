"""AA-1135 — the time range of Platform › Delivery: presets from the summary, the custom range recomputed from the
collector's day files (api/services/delivery_range.py, delivery_range_worker.py, the byte copy delivery_engine.py).

The parity test pins custom == preset on the fixture the root collector publishes for its invented demo repo
(tests/unit/fixtures/delivery_range/, written by the root tools/delivery/sync_engine.py with the same engine). Endpoint
tests: admin only, the overlay of each section, validation (dates, the cap setting, outside the store), the engine
mismatch pause, the cache per range, the account-ID mask on the computed answer, the child process plumbing. No S3,
no database (compiled setting defaults).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin
from api.services import delivery_engine as engine
from api.services import delivery_metrics as dmx
from api.services import delivery_range as dr
from api.services import delivery_range_worker as worker
from api.services import docs_reader
from services.reports import delivery_settings as ds

FIX = Path(__file__).resolve().parent / "fixtures" / "delivery_range"
LOCK = Path(__file__).resolve().parents[2] / "api" / "services" / "delivery_engine.lock.json"
SECRET = "the-real-secret"
PATH = "/admin/platform/delivery"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/delivery/summary.json"
SUMMARY = json.loads((FIX / "summary.json").read_text(encoding="utf-8"))


def _read_fixture(key: str):
    """The worker's S3 reader, answered from the fixture (keys under docs-reader/delivery/)."""
    p = FIX / key.split("delivery/", 1)[1]
    return p.read_bytes() if p.exists() else None


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
        body = json.dumps(self.objects[Key]).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


class Worker:
    """In-process stand-in for the child: the real computation on the fixture, counting calls."""

    def __init__(self, transform=None):
        self.calls: list[dict] = []
        self.transform = transform

    async def __call__(self, payload):
        self.calls.append(payload)
        out = worker.run(json.loads(json.dumps(payload)), _read_fixture)
        return self.transform(out) if self.transform else out


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    dmx.reset_cache()
    dr.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    dmx.reset_cache()
    dr.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _get(client, summary=None, params=None, run=None):
    run = run or Worker()
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3({KEY: summary or SUMMARY})), \
            patch.object(dr, "_run_worker", run):
        return client.get(PATH, headers=ADMIN, params=params or {}), run


# ── the byte copy and the parity ─────────────────────────────────────────────────────────────────────────────────


def test_engine_copy_matches_its_lock():
    """The copy is never edited here: change the root tools/delivery/delivery_engine.py, then sync_engine.py."""
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    data = Path(engine.__file__).read_bytes()
    assert hashlib.sha256(data).hexdigest() == lock["sha256"] == engine.engine_sha256()
    assert lock["definitions_version"] == engine.DEFINITIONS_VERSION
    assert lock["source"] == "AA-Ecosys/tools/delivery/delivery_engine.py"


def test_fixture_was_published_by_this_engine():
    assert SUMMARY["definitions"]["engine_sha256"] == engine.engine_sha256()


@pytest.mark.parametrize("preset", ["d7", "d28", "d90"])
def test_custom_range_equals_the_preset_of_the_same_window(preset):
    """The parity test: the worker recomputes the collector's preset exactly from the published day files."""
    want = SUMMARY["apps"][0]["ranges"][preset]
    payload = dr._payload(SUMMARY, engine.ts(want["window"]["requested_start"]), engine.ts(want["window"]["end"]),
                          "bucket", "docs-reader/delivery")
    got = worker.run(payload, _read_fixture)
    assert got["engine_sha256"] == engine.engine_sha256()
    assert [a["id"] for a in got["apps"]] == ["demo"]
    assert json.dumps(got["apps"][0]["range"], sort_keys=True) == json.dumps(want, sort_keys=True)


def test_worker_main_reads_stdin_and_reports_errors(capsys):
    out = io.StringIO()
    with patch.object(worker, "_s3_reader", lambda bucket: _read_fixture):
        want = SUMMARY["apps"][0]["ranges"]["d7"]["window"]
        payload = dr._payload(SUMMARY, engine.ts(want["requested_start"]), engine.ts(want["end"]), "bucket",
                              "docs-reader/delivery")
        assert worker.main(io.StringIO(json.dumps(payload)), out) == 0
    assert json.loads(out.getvalue())["apps"][0]["id"] == "demo"
    assert worker.main(io.StringIO("{not json"), io.StringIO()) == 1
    assert "JSONDecodeError" in capsys.readouterr().err


def test_child_process_plumbing(monkeypatch):
    echo = "import sys, json; d = json.load(sys.stdin); print(json.dumps({'echo': d['start']}))"
    monkeypatch.setattr(dr, "_worker_argv", lambda: [sys.executable, "-c", echo])
    assert asyncio.run(dr._run_worker({"start": "x"})) == {"echo": "x"}
    monkeypatch.setattr(dr, "_worker_argv", lambda: [sys.executable, "-c", "import sys; sys.exit(1)"])
    with pytest.raises(dr.RangeFailed):
        asyncio.run(dr._run_worker({}))
    monkeypatch.setattr(dr, "_worker_argv", lambda: [sys.executable, "-c", "print('not json')"])
    with pytest.raises(dr.RangeFailed):
        asyncio.run(dr._run_worker({}))


def _missing_one_event(key: str):
    """A day file that lost one event (a collector upload that failed after its cache kept the event)."""
    raw = _read_fixture(key)
    if raw is None or not key.endswith("2026/03/18.ndjson"):
        return raw
    return ("".join(line + "\n" for line in raw.decode("utf-8").splitlines()[1:])).encode("utf-8")


def test_day_files_out_of_step_with_the_summary_are_refused(monkeypatch):
    """The data parity: day files that do not rebuild the summary's store (definitions.store_fingerprint) never give
    numbers; the child exits OUT_OF_STEP and the parent refuses with 409 out_of_step."""
    assert SUMMARY["definitions"]["store_fingerprint"]
    want = SUMMARY["apps"][0]["ranges"]["d7"]["window"]
    payload = dr._payload(SUMMARY, engine.ts(want["requested_start"]), engine.ts(want["end"]), "bucket",
                          "docs-reader/delivery")
    with pytest.raises(engine.StoreMismatch):
        worker.run(payload, _missing_one_event)
    err = io.StringIO()
    with patch.object(worker, "_s3_reader", lambda bucket: _missing_one_event), patch.object(sys, "stderr", err):
        assert worker.main(io.StringIO(json.dumps(payload)), io.StringIO()) == worker.OUT_OF_STEP == dr.OUT_OF_STEP
    assert err.getvalue().startswith("StoreMismatch")
    monkeypatch.setattr(dr, "_worker_argv", lambda: [sys.executable, "-c", f"import sys; sys.exit({dr.OUT_OF_STEP})"])
    with pytest.raises(dr.RangeRefused) as e:
        asyncio.run(dr._run_worker({}))
    assert (e.value.status, e.value.code) == (409, "out_of_step")


def test_out_of_step_answers_409_and_presets_still_work(client, monkeypatch):
    real = dr._run_worker
    monkeypatch.setattr(dr, "_worker_argv", lambda: [sys.executable, "-c", f"import sys; sys.exit({dr.OUT_OF_STEP})"])
    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, run=real)
    assert res.status_code == 409 and "out of step" in res.json()["detail"]
    assert dr.cached_ranges() == 0
    res, _ = _get(client, params={"range": "7d"}, run=real)
    assert res.status_code == 200


# ── presets ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_admin_only(client):
    fake = FakeS3({KEY: SUMMARY})
    run = Worker()
    with patch.object(docs_reader, "_s3_client", lambda: fake), patch.object(dr, "_run_worker", run):
        res = client.get(PATH, headers={"X-Admin-Secret": SECRET, "x-admin-role": "viewer"},
                         params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"})
    assert res.status_code == 403
    assert fake.calls == [] and run.calls == []


def test_preset_replaces_the_windowed_keys(client):
    res, _ = _get(client, params={"range": "7d"})
    assert res.status_code == 200
    body = res.json()
    app = body["apps"][0]
    d7 = SUMMARY["apps"][0]["ranges"]["d7"]
    assert app["window"] == d7["window"]
    for key in ("dora", "ci", "deploys", "changes", "lead_time_segments"):
        assert app[key] == d7[key], key
    assert "ranges" not in app and "config" not in body and "queue" not in app
    assert body["range"]["key"] == "7d"
    assert body["range"]["presets"] == ["7d", "28d", "90d"]
    assert body["range"]["store_from"] == "2025-12-12" and body["range"]["last_day"] == "2026-03-20"
    assert body["range"]["custom"] is True
    assert body["range"]["max_days"] == 90
    assert set(app["status"]) >= {"deployment_frequency", "main"}


def test_a_preset_does_not_need_the_custom_range_setting(client):
    """Code deployed before the settings registry knows the key: presets still answer (max_days null), custom 500s
    only for the custom range, never the whole page."""
    real = dmx.settings.aget

    async def aget(key, tenant_id=None):
        if key == ds.CUSTOM_RANGE_MAX_DAYS:
            raise KeyError(f"unknown setting {key!r} (not in the compiled defaults)")
        return await real(key, tenant_id) if tenant_id else await real(key)

    with patch.object(dmx.settings, "aget", aget):
        for params in ({}, {"range": "7d"}, {"range": "90d", "section": "queue"}):
            res, _ = _get(client, params=params)
            assert res.status_code == 200, res.text
            assert res.json()["range"]["max_days"] is None
        with pytest.raises(KeyError):
            asyncio.run(dmx._custom_max_days(required=True))


def test_preset_of_the_queue_and_git_sections(client):
    res, _ = _get(client, params={"range": "90d", "section": "queue"})
    app = res.json()["apps"][0]
    d90 = SUMMARY["apps"][0]["ranges"]["d90"]
    assert app["queue"] == d90["queue"] and app["window"] == d90["window"]
    assert "wait_histogram" in app["queue"] and "weekly" in app["queue"]
    res, _ = _get(client, params={"range": "90d", "section": "git"})
    app = res.json()["apps"][0]
    assert app["git"]["range"] == d90["git"]
    assert app["git"]["complete_since"] == "2025-12-20"


def test_summary_without_presets(client):
    old = copy.deepcopy(SUMMARY)
    for a in old["apps"]:
        a.pop("ranges")
        a.update({"window": {"days": 28}, "dora": {}, "ci": {}})
    res, _ = _get(client, summary=old)
    assert res.status_code == 200 and res.json()["apps"][0]["window"] == {"days": 28}
    dmx.reset_cache()
    res, _ = _get(client, summary=old, params={"range": "7d"})
    assert res.status_code == 409


# ── custom ───────────────────────────────────────────────────────────────────────────────────────────────────────


def test_custom_range_is_computed_once_and_cached(client):
    params = {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}
    res, run = _get(client, params=params)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["range"]["start"] == "2026-03-14T00:00:00Z"
    assert body["range"]["end"] == "2026-03-20T12:00:00Z"   # the newest collection, not the end of the last day
    assert (body["range"]["from"], body["range"]["to"], body["range"]["cached"]) == ("2026-03-14", "2026-03-20", False)
    app = body["apps"][0]
    d7 = SUMMARY["apps"][0]["ranges"]["d7"]
    # every event of the demo repo is after 14/03: the same deploys and changes as the 7-day preset
    assert app["dora"]["deployment_frequency"]["attempts"] == d7["dora"]["deployment_frequency"]["attempts"]
    assert app["deploys"] == d7["deploys"]
    assert run.calls[0]["start"] == "2026-03-14T00:00:00Z" and run.calls[0]["prefix"] == "docs-reader/delivery"
    assert "config" in run.calls[0]["summary"]
    res, run2 = _get(client, params=params)
    assert res.json()["range"]["cached"] is True and run2.calls == []
    assert dr.cached_ranges() == 1


def test_cache_keeps_the_newest_ranges_of_the_newest_summary(client):
    async def aget(key, tenant_id=None):
        return 1 if key == ds.CUSTOM_RANGE_CACHE_ENTRIES else 90

    with patch.object(dr.settings, "aget", aget):
        _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"})
        _get(client, params={"range": "custom", "from": "2026-03-15", "to": "2026-03-20"})
    assert dr.cached_ranges() == 1
    newer = {**SUMMARY, "generated_at": "2026-03-20T13:00:00Z"}
    dmx.reset_cache()
    _, run = _get(client, summary=newer, params={"range": "custom", "from": "2026-03-15", "to": "2026-03-20"})
    assert len(run.calls) == 1 and dr.cached_ranges() == 1


@pytest.mark.parametrize("params,status,code", [
    ({"range": "custom"}, 422, "needs from and to"),
    ({"range": "custom", "from": "2026-03-20", "to": "2026-03-10"}, 422, "before"),
    ({"range": "custom", "from": "2025-12-01", "to": "2026-01-10"}, 422, "stored history"),
    ({"range": "custom", "from": "2026-03-10", "to": "2026-03-21"}, 422, "newest collection"),
    ({"range": "custom", "from": "2025-12-12", "to": "2026-03-20"}, 422, "longest custom range is 90"),
    ({"range": "custom", "from": "14/03/2026", "to": "2026-03-20"}, 422, ""),
    ({"range": "45d"}, 422, ""),
])
def test_custom_range_validation(client, params, status, code):
    res, run = _get(client, params=params)
    assert res.status_code == status, res.text
    assert code.lower() in json.dumps(res.json()).lower()
    assert run.calls == []


def test_the_cap_is_the_setting(client):
    async def aget(key, tenant_id=None):
        return 3 if key == ds.CUSTOM_RANGE_MAX_DAYS else 20

    with patch.object(dr.settings, "aget", aget), patch.object(dmx.settings, "aget", aget):
        assert dr.window(SUMMARY, "2026-03-18", "2026-03-20", 3)[0].isoformat().startswith("2026-03-18")
        with pytest.raises(dr.RangeRefused) as e:
            dr.window(SUMMARY, "2026-03-17", "2026-03-20", 3)
    assert e.value.code == "too_long"
    with patch.object(dr.settings, "aget", aget):
        res, run = _get(client, params={"range": "custom", "from": "2026-03-17", "to": "2026-03-20"})
    assert res.status_code == 422 and "3 days" in res.json()["detail"] and run.calls == []


def test_engine_mismatch_pauses_custom_but_not_presets(client):
    other = copy.deepcopy(SUMMARY)
    other["definitions"]["engine_sha256"] = "0" * 64
    res, run = _get(client, summary=other, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"})
    assert res.status_code == 409 and "paused" in res.json()["detail"] and run.calls == []
    res, _ = _get(client, summary=other, params={"range": "7d"})
    assert res.status_code == 200 and res.json()["range"]["custom"] is False


def test_summary_before_the_custom_range(client):
    old = copy.deepcopy(SUMMARY)
    old["definitions"].pop("store_from")
    res, _ = _get(client, summary=old, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"})
    assert res.status_code == 409


def test_worker_failure_is_503(client):
    async def broken(payload):
        raise dr.RangeFailed("The custom range could not be computed")

    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, run=broken)
    assert res.status_code == 503


def test_answer_of_another_engine_is_refused(client):
    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"},
                  run=Worker(lambda out: {**out, "engine_sha256": "f" * 64}))
    assert res.status_code == 503


def test_computed_answer_is_masked(client):
    def plant(out):
        out["apps"][0]["range"]["changes"]["slowest"] = [{"number": 1, "title": "touches 123456789012 by mistake"}]
        return out

    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, run=Worker(plant))
    title = res.json()["apps"][0]["changes"]["slowest"][0]["title"]
    assert "123456789012" not in title and docs_reader.ACCOUNT_ID_MARKER in title


# ── S284 live check: the recent queue entries stay inside the range ─────────────────────────────────────────────

def _entry(pr, outcome, enqueued, left):
    return {"pr": pr, "title": f"Invented change {pr}", "outcome": outcome, "enqueued_at": enqueued, "left_at": left}


def test_clip_recent_keeps_the_entries_of_the_window():
    queue = {
        "window": {"start": "2026-03-14T00:00:00Z", "end": "2026-03-16T00:00:00Z"},
        "entries": 2,
        "recent": [
            _entry(5, "queued", "2026-03-16T08:00:00Z", None),                    # entered after the end
            _entry(4, "merged", "2026-03-16T07:00:00Z", "2026-03-16T07:30:00Z"),  # left after the end
            _entry(3, "queued", "2026-03-15T23:00:00Z", None),                    # in the queue at the end
            _entry(2, "merged", "2026-03-15T22:00:00Z", "2026-03-16T00:00:00Z"),  # left exactly at the end
            _entry(1, "ejected", "2026-03-15T09:00:00Z", "2026-03-15T10:00:00Z"),  # inside
            _entry(0, "merged", "2026-03-14T00:00:00Z", "2026-03-14T00:00:00Z"),  # left at the start
        ],
    }
    out = dr.clip_recent(queue)
    assert [e["pr"] for e in out["recent"]] == [3, 1, 0]
    assert out["entries"] == 2 and out["window"] == queue["window"]
    assert len(queue["recent"]) == 6   # the input is not changed


@pytest.mark.parametrize("queue", [None, {}, {"recent": []}, {"window": {"start": "x"}, "recent": [{"pr": 1}]}])
def test_clip_recent_leaves_a_section_without_a_window_alone(queue):
    assert dr.clip_recent(queue) == queue


def test_custom_range_queue_lists_no_entry_after_its_last_day(client):
    raw: list[dict] = []

    def keep(out):
        raw.append(copy.deepcopy(out["apps"][0]["range"]["queue"]))
        return out

    # the demo repo's queue entries all left on 16/03: a range that ends on 15/03 must list none of them
    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-15", "section": "queue"},
                  run=Worker(keep))
    assert res.status_code == 200, res.text
    q = res.json()["apps"][0]["queue"]
    assert q["window"]["end"] == "2026-03-16T00:00:00Z"
    assert raw[0]["recent"], "the engine lists the later entries: the test would not bite without them"
    assert q["recent"] == []
    # a range that covers 16/03 keeps them, in the engine's order
    dmx.reset_cache()
    res, _ = _get(client, params={"range": "custom", "from": "2026-03-14", "to": "2026-03-16", "section": "queue"})
    assert [e["pr"] for e in res.json()["apps"][0]["queue"]["recent"]] == [11, 11, 10]


def test_preset_queue_recent_is_clipped_too(client):
    res, _ = _get(client, params={"range": "7d", "section": "queue"})
    d7 = SUMMARY["apps"][0]["ranges"]["d7"]["queue"]
    assert res.json()["apps"][0]["queue"]["recent"] == dr.clip_recent(d7)["recent"]

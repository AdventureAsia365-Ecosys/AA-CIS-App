"""AA-1135 (S284 live check) — the cold path of a custom range of Platform › Delivery.

The first custom range after a summary rebuild took 29 s on Dev and API Gateway answered 504: the child started with
`python -m api.services.delivery_range_worker`, which ran api/__init__.py (the whole FastAPI app) before it read about
100 day files and rebuilt every app's context. These tests pin the fix:

  * the child boot never imports the app (api.main, fastapi) — the import-budget guard;
  * the two phases (prepare once per summary, cut a window per range) give exactly engine.custom_range's answer, and
    the indexed store answers `of` exactly like the engine's Store;
  * read budget: a cold range reads each day file once, a range of a prepared summary reads none;
  * the snapshot is used across requests, refused for another summary or engine, and a summary out of step is
    remembered; `warm` prepares it in the background for a preset and stays quiet otherwise.

Fixture: the invented demo repo the root collector publishes (tests/unit/fixtures/delivery_range/). No S3, no database.
"""
from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys
from collections import Counter
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

FIX = Path(__file__).resolve().parent / "fixtures" / "delivery_range"
ROOT = Path(__file__).resolve().parents[2]
SUMMARY = json.loads((FIX / "summary.json").read_text(encoding="utf-8"))
SECRET = "the-real-secret"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/delivery/summary.json"
PREFIX = "docs-reader/delivery"
WINDOWS = [("2026-03-14T00:00:00Z", "2026-03-20T12:00:00Z"), ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
           ("2025-12-12T00:00:00Z", "2026-03-20T12:00:00Z")]


class Reader:
    """The worker's S3 reader answered from the fixture, counting the GETs per key."""

    def __init__(self):
        self.calls: Counter = Counter()

    def __call__(self, key: str):
        self.calls[key] += 1
        p = FIX / key.split("delivery/", 1)[1]
        return p.read_bytes() if p.exists() else None


def _payload(a=None, b=None, snapshot=None, summary=None):
    return dr._payload(summary or SUMMARY, engine.ts(a), engine.ts(b), "bucket", PREFIX, snapshot)


def _texts():
    return worker.read_days(_payload(), Reader())


class FakeS3:
    def __init__(self, summary):
        self.body = json.dumps(summary).encode()

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.body), "ContentLength": len(self.body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    dmx.reset_cache()
    dr.reset_cache()
    monkeypatch.setitem(dr._snap, "dir", None)
    monkeypatch.setitem(dr._snap, "task", None)
    monkeypatch.setattr(dr.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "snap"))
    (tmp_path / "snap").mkdir()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    dmx.reset_cache()
    dr.reset_cache()


# ── the import budget of the child ───────────────────────────────────────────────────────────────────────────────


def _child_modules() -> set[str]:
    probe = dr._STUB + "import json\nprint(json.dumps(sorted(sys.modules)))\n"
    out = subprocess.run([sys.executable, "-c", probe, str(ROOT / "api")], cwd=str(ROOT), capture_output=True,
                         text=True, timeout=60, check=True)
    return set(json.loads(out.stdout))


def test_the_child_boot_never_imports_the_app():
    """The 29 s cold path: `python -m api.services...` ran api/__init__.py, i.e. the whole FastAPI app."""
    mods = _child_modules()
    assert "api.services.delivery_range_worker" in mods and "api.services.delivery_engine" in mods
    heavy = sorted(m for m in mods if m.split(".")[0] in ("fastapi", "starlette", "pydantic", "asyncpg", "boto3",
                                                          "sentry_sdk", "shared", "services")
                   or m in ("api.main", "api.routers"))
    assert heavy == []
    assert len(mods) < 300  # the whole app is about 2,700 modules


def test_worker_argv_runs_the_boot():
    argv = dr._worker_argv()
    assert argv[:2] == [sys.executable, "-c"] and argv[2] == dr._BOOT and argv[3] == str(ROOT / "api")
    assert "-m" not in argv


# ── parity of the two phases ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("a,b", WINDOWS)
def test_two_phases_equal_the_engine_custom_range(a, b):
    """One prepared summary serves every window: the same answer as engine.custom_range, and ranges() does not change
    the prepared contexts (the earlier windows still match after the later ones ran)."""
    texts = _texts()
    prep = worker.prepare(_payload()["summary"], texts)
    for x, y in WINDOWS + [(a, b)]:
        want = engine.custom_range(_payload()["summary"], engine.load_events(texts), engine.ts(x), engine.ts(y))
        got = worker.ranges(prep, engine.ts(x), engine.ts(y))
        assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)


def test_indexed_store_answers_like_the_engine_store():
    events = engine.load_events(_texts())
    plain = engine.Store(copy.deepcopy(events))
    fast = worker.IndexedStore(copy.deepcopy(events))
    kinds = {e["event"] for e in events.values()}
    repos = {e["attributes"].get("vcs.repository.name") for e in events.values()} | {None, "other/repo"}
    for kind in sorted(kinds) + ["no.such.kind"]:
        for repo in repos:
            assert plain.of(kind, repo) == fast.of(kind, repo)
    first = fast.of(sorted(kinds)[0])
    first.clear()
    assert fast.of(sorted(kinds)[0]) == plain.of(sorted(kinds)[0])  # a new list each call
    cut = engine.ts("2026-03-01T00:00:00Z")
    plain.prune(cut)
    fast.prune(cut)
    for kind in kinds:
        assert plain.of(kind) == fast.of(kind)


def test_indexed_store_fails_like_the_engine_on_an_event_without_a_repo():
    e = {"id": "x", "event": "vcs.change", "time": "2026-03-01T00:00:00Z", "attributes": {}}
    with pytest.raises(KeyError):
        engine.Store({"x": e}).of("vcs.change", "some/repo")
    with pytest.raises(KeyError):
        worker.IndexedStore({"x": copy.deepcopy(e)}).of("vcs.change", "some/repo")
    assert worker.IndexedStore({"x": e}).of("vcs.change") == [e]


def test_out_of_step_day_files_are_refused_by_prepare():
    texts = _texts()
    i = next(i for i, t in enumerate(texts) if t)
    texts[i] = "\n".join(texts[i].splitlines()[1:])
    with pytest.raises(engine.StoreMismatch):
        worker.prepare(_payload()["summary"], texts)


# ── read budget and the snapshot ─────────────────────────────────────────────────────────────────────────────────


def test_a_cold_range_reads_each_day_file_once_and_a_prepared_one_none(tmp_path):
    snap = tmp_path / "s.pickle"
    days = len(engine.day_keys(SUMMARY["config"], engine.ts(SUMMARY["generated_at"])))
    cold = Reader()
    out = worker.answer(_payload(*WINDOWS[0], snapshot=snap), lambda bucket: cold)
    assert sum(cold.calls.values()) == days and set(cold.calls.values()) == {1}
    assert snap.is_file()
    warm = Reader()
    again = worker.answer(_payload(*WINDOWS[0], snapshot=snap), lambda bucket: warm)
    assert sum(warm.calls.values()) == 0
    assert again == out == json.loads(json.dumps(worker.run(_payload(*WINDOWS[0]), Reader())))


def test_prepare_only_payload(tmp_path):
    snap = tmp_path / "s.pickle"
    out = worker.answer(_payload(snapshot=snap), lambda bucket: Reader())
    assert out == {"generated_at": SUMMARY["generated_at"], "engine_sha256": engine.engine_sha256(), "prepared": True}
    assert worker.load(snap, SUMMARY) is not None


def test_a_snapshot_of_another_summary_or_engine_is_not_used(tmp_path):
    snap = tmp_path / "s.pickle"
    worker.save(worker.prepare(SUMMARY, _texts()), snap)
    assert worker.load(snap, SUMMARY)["generated_at"] == SUMMARY["generated_at"]
    assert worker.load(snap, {**SUMMARY, "generated_at": "2026-03-20T13:00:00Z"}) is None
    with patch.object(worker.engine, "engine_sha256", lambda: "f" * 64):
        assert worker.load(snap, SUMMARY) is None
    assert worker.load(tmp_path / "none.pickle", SUMMARY) is None
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["s.pickle"]  # no temporary file left


def test_the_real_child_answers_from_a_snapshot_without_storage(tmp_path):
    """End to end through the boot and stdin / stdout: a prepared summary needs no S3 (the bucket is fake)."""
    snap = tmp_path / "s.pickle"
    worker.save(worker.prepare(SUMMARY, _texts()), snap)
    want = SUMMARY["apps"][0]["ranges"]["d7"]
    got = asyncio.run(dr._run_worker(_payload(want["window"]["requested_start"], want["window"]["end"], snap)))
    assert json.dumps(got["apps"][0]["range"], sort_keys=True) == json.dumps(want, sort_keys=True)


def test_snapshot_path_keeps_only_the_newest_summary():
    old = dr._snapshot_path("2026-03-20T11:00:00Z")
    old.write_bytes(b"x")
    assert dr.prepared("2026-03-20T11:00:00Z")
    new = dr._snapshot_path("2026-03-20T12:00:00Z")
    assert not old.exists() and new.parent == old.parent and new != old
    assert not dr.prepared("2026-03-20T11:00:00Z") and not dr.prepared("2026-03-20T12:00:00Z")


# ── through the route ────────────────────────────────────────────────────────────────────────────────────────────


class Child:
    """In-process stand-in for the child that runs worker.answer (snapshot included) on the fixture."""

    def __init__(self):
        self.reader = Reader()
        self.payloads: list[dict] = []

    async def __call__(self, payload):
        self.payloads.append(payload)
        return json.loads(json.dumps(worker.answer(json.loads(json.dumps(payload)), lambda bucket: self.reader)))


def _get(client, params, child):
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3(SUMMARY)), patch.object(dr, "_run_worker", child):
        return client.get("/admin/platform/delivery", headers=ADMIN, params=params)


def test_second_range_of_a_summary_reads_no_day_file():
    client, child = TestClient(main.app), Child()
    days = len(engine.day_keys(SUMMARY["config"], engine.ts(SUMMARY["generated_at"])))
    res = _get(client, {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, child)
    assert res.status_code == 200, res.text
    assert sum(child.reader.calls.values()) == days and child.payloads[0]["snapshot"]
    res = _get(client, {"range": "custom", "from": "2026-03-01", "to": "2026-03-20"}, child)
    assert res.status_code == 200 and res.json()["range"]["cached"] is False
    assert sum(child.reader.calls.values()) == days  # the second range: no read
    assert dr.prepared(SUMMARY["generated_at"])


def test_out_of_step_is_remembered_for_the_summary():
    calls = []

    async def out_of_step(payload):
        calls.append(payload)
        raise dr._out_of_step()

    client = TestClient(main.app)
    for _ in range(2):
        res = _get(client, {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, out_of_step)
        assert res.status_code == 409 and "out of step" in res.json()["detail"]
    assert len(calls) == 1


def test_a_preset_starts_the_preparation_and_a_custom_range_does_not():
    client, seen = TestClient(main.app), []
    with patch.object(dr, "warm", lambda bundle: seen.append(bundle["generated_at"])):
        assert _get(client, {"range": "7d"}, Child()).status_code == 200
        assert _get(client, {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}, Child()).status_code == 200
    assert seen == [SUMMARY["generated_at"]]


# ── warm ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _bundle():
    return asyncio.run(_load())


async def _load():
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3(SUMMARY)):
        return await dmx.load()


def test_warm_prepares_then_the_range_reads_nothing():
    bundle, child = _bundle(), Child()

    async def flow():
        with patch.object(dr, "_run_worker", child):
            dr.warm(bundle)
            await dr._snap["task"]
            assert child.payloads[0]["start"] is None and dr.prepared(bundle["generated_at"])
            before = sum(child.reader.calls.values())
            dr.warm(bundle)  # prepared: nothing more to do
            assert dr._snap["task"].done()
            with patch.object(dr.settings, "aget", _aget):
                got = await dr.compute(bundle, "2026-03-14", "2026-03-20")
            return before, got

    before, got = asyncio.run(flow())
    assert before > 0 and sum(child.reader.calls.values()) == before and got["cached"] is False
    assert len(child.payloads) == 2


async def _aget(key, tenant_id=None):
    return 90 if key.endswith("max_days") else 8


def test_warm_does_nothing_when_custom_ranges_are_off_or_without_a_loop(monkeypatch):
    bundle, child = _bundle(), Child()
    monkeypatch.setattr(dr, "_run_worker", child)
    dr.warm(bundle)  # no running loop: nothing scheduled, no error
    assert dr._snap["task"] is None

    async def flow(b):
        dr.warm(b)
        return dr._snap["task"]

    monkeypatch.setattr(dr, "WARM", False)
    assert asyncio.run(flow(bundle)) is None
    monkeypatch.setattr(dr, "WARM", True)
    other = {**bundle, "definitions": {**bundle["definitions"], "engine_sha256": "f" * 64}}
    assert asyncio.run(flow(other)) is None
    dr._snap["refused"] = bundle["generated_at"]
    assert asyncio.run(flow(bundle)) is None
    assert child.payloads == []


def test_warm_failure_is_logged_not_raised():
    bundle = _bundle()

    async def broken(payload):
        raise dr.RangeFailed("The custom range could not be computed")

    async def flow():
        with patch.object(dr, "_run_worker", broken):
            dr.warm(bundle)
            await dr._snap["task"]

    asyncio.run(flow())
    assert not dr.prepared(bundle["generated_at"]) and dr._snap["refused"] is None


def test_a_prepared_summary_serves_custom_ranges_without_a_summary_re_read(monkeypatch):
    """Unprepared, a custom range re-reads the summary (the day files on storage belong to the newest one); prepared,
    it is served from the cached summary like the presets, so the snapshot of that summary is used."""
    client, child, gets = TestClient(main.app), Child(), []

    class Counting(FakeS3):
        def get_object(self, Bucket, Key):
            gets.append(Key)
            return super().get_object(Bucket, Key)

    clock = [1000.0]
    monkeypatch.setattr(dmx.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dr, "WARM", False)  # the preset does not prepare it here (see the next test)
    with patch.object(docs_reader, "_s3_client", lambda: Counting(SUMMARY)), patch.object(dr, "_run_worker", child):
        assert client.get("/admin/platform/delivery", headers=ADMIN, params={"range": "7d"}).status_code == 200
        clock[0] += 60  # past MIN_REFRESH_S, within CACHE_TTL_S
        params = {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}
        assert client.get("/admin/platform/delivery", headers=ADMIN, params=params).status_code == 200
        assert len(gets) == 2  # not prepared yet: the custom range re-read the summary
        clock[0] += 60
        params = {"range": "custom", "from": "2026-03-01", "to": "2026-03-20"}
        assert client.get("/admin/platform/delivery", headers=ADMIN, params=params).status_code == 200
    assert len(gets) == 2 and len(child.payloads) == 2


def test_a_preset_page_load_prepares_the_summary_through_the_route():
    """Through the app: the preset request schedules the preparation on the request loop; the custom range that
    follows reads no day file."""
    client, child = TestClient(main.app), Child()
    with patch.object(docs_reader, "_s3_client", lambda: FakeS3(SUMMARY)), patch.object(dr, "_run_worker", child):
        assert client.get("/admin/platform/delivery", headers=ADMIN, params={"range": "28d"}).status_code == 200
        task = dr._snap["task"]
        assert task is not None
        if not task.done():  # the TestClient loop may end before it ran: finish it the same way
            pytest.skip("the preparation did not run inside the test client loop")
        reads = sum(child.reader.calls.values())
        params = {"range": "custom", "from": "2026-03-14", "to": "2026-03-20"}
        assert client.get("/admin/platform/delivery", headers=ADMIN, params=params).status_code == 200
    assert dr.prepared(SUMMARY["generated_at"]) and reads > 0 and sum(child.reader.calls.values()) == reads
    assert [p["start"] for p in child.payloads] == [None, "2026-03-14T00:00:00Z"]

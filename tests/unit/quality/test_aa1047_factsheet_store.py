"""AA-1047 (Quality W1) — the FactSheet store (services/quality/factsheet_store.py) and the three writers' hooks: S1
(admin_pipeline._stamp_fact_sheet), T2 (t2_rewrite_job._store_fact_sheet + the stamp in run) and T9
(service._store_fact_sheet + writer_input_snapshot). Fake connections only; the real table: the integration test
test_aa1047_factsheet_store_db.py. Invented names and places only."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from services.quality import factsheet as fs
from services.quality import factsheet_store as store

TENANT = "00000000-0000-4000-8000-000000a10471"
FS_ID = "0f5e0000-0000-4000-8000-000000000001"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """fetchval answers from `answers` in order; execute records; `fail` raises an exception of that class name."""

    def __init__(self, answers=(), fail=None):
        self.answers = list(answers)
        self.calls: list[tuple] = []
        self.fail = fail

    def transaction(self):
        return _Tx()

    def _maybe_fail(self):
        if self.fail:
            raise type(self.fail, (Exception,), {})("relation does not exist")

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", " ".join(sql.split()), args))
        self._maybe_fail()
        return self.answers.pop(0) if self.answers else None

    async def execute(self, sql, *args):
        self.calls.append(("execute", " ".join(sql.split()), args))
        self._maybe_fail()
        return "UPDATE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()


@pytest.fixture(autouse=True)
def _reset_skip():
    store._skip_until = 0.0
    yield
    store._skip_until = 0.0


ROW = {"tour_id": "7a000000-0000-4000-8000-000000000001", "src_name": "Varn Valley Walk", "src_summary": "A walk.",
       "src_highlights": '["Rim path"]', "src_itineraries": "Day 1: Tavo\nWalk 12 km.\nDay 2: Orsa\nRow 3 km.",
       "country": "Zemlia", "duration": "2 days", "price_raw": "1,250 USD", "inclusions": "Guide",
       "exclusions": "Tips", "group_size": "2-12", "best_time_to_go": None}


def _sheet():
    return fs.build_route_factsheet(seed="Lumen Gorge — walk\nLocation: Varn, Zemlia", facts_text="",
                                    brand_audience={"brand_name": "Trailwise"}, trip_name=None, destination="Zemlia")


def test_row_args_order_and_json():
    sheet = _sheet()
    args = store.row_args(sheet, tenant_id=TENANT, workflow="t9", tour_id=None)
    assert args[:6] == (TENANT, "t9", "route", fs.SCHEMA, fs.BUILDER_VERSION, sheet.sha())
    assert args[6] is None and args[7] == len(sheet.lines) and args[8:10] == (0, 0)
    assert json.loads(args[10])[0]["id"] == "S1" and json.loads(args[11]) == [] and args[14] == sheet.render()
    with pytest.raises(ValueError):
        store.row_args(sheet, tenant_id=TENANT, workflow="t7")


@pytest.mark.real_factsheet_store
def test_save_inserts_then_reuses_the_row():
    conn = FakeConn(answers=[uuid.UUID(FS_ID)])
    assert asyncio.run(store.save(conn, _sheet(), tenant_id=TENANT, workflow="t9")) == FS_ID
    assert "ON CONFLICT (tenant_id, workflow, fs_sha) DO NOTHING" in conn.calls[0][1]
    again = FakeConn(answers=[None, uuid.UUID(FS_ID)])             # conflict: the existing row is read back
    assert asyncio.run(store.save(again, _sheet(), tenant_id=TENANT, workflow="t9")) == FS_ID
    assert again.calls[1][1].startswith("SELECT fs_id FROM acp_shared.fact_sheet")
    assert again.calls[1][2] == (TENANT, "t9", _sheet().sha())


@pytest.mark.real_factsheet_store
def test_save_is_best_effort_and_backs_off_on_a_missing_table():
    conn = FakeConn(fail="UndefinedTableError")
    assert asyncio.run(store.save(conn, _sheet(), tenant_id=TENANT, workflow="t9")) is None
    later = FakeConn(answers=[uuid.UUID(FS_ID)])
    assert asyncio.run(store.save(later, _sheet(), tenant_id=TENANT, workflow="t9")) is None
    assert later.calls == []                                       # skipped for _SKIP_SECONDS
    store._skip_until = 0.0
    other = FakeConn(fail="DeadlockDetectedError")                # any other error: logged, no back-off
    assert asyncio.run(store.save(other, _sheet(), tenant_id=TENANT, workflow="t9")) is None
    assert asyncio.run(store.save(FakeConn(answers=[FS_ID]), _sheet(), tenant_id=TENANT, workflow="t9")) == FS_ID


def test_save_does_nothing_when_switched_off_or_without_a_tenant(monkeypatch):
    conn = FakeConn(answers=[FS_ID])
    monkeypatch.setenv("FACTSHEET_PERSIST", "off")
    assert asyncio.run(store.save(conn, _sheet(), tenant_id=TENANT, workflow="t9")) is None
    monkeypatch.setenv("FACTSHEET_PERSIST", "on")
    assert asyncio.run(store.save(conn, _sheet(), tenant_id=None, workflow="t9")) is None
    assert asyncio.run(store.save(conn, None, tenant_id=TENANT, workflow="t9")) is None
    assert conn.calls == []


@pytest.mark.real_factsheet_store
def test_save_tour_sheet_builds_from_the_raw_row():
    conn = FakeConn(answers=[FS_ID])
    got = asyncio.run(store.save_tour_sheet(conn, ROW, tenant_id=TENANT, workflow="t2", tour_id=ROW["tour_id"],
                                            meta={"published_tour_id": "p-1"}))
    assert got == FS_ID
    args = conn.calls[0][2]
    assert args[1:3] == ("t2", "tour") and args[6] == ROW["tour_id"]
    assert json.loads(args[13])["published_tour_id"] == "p-1"
    tour, agency = fs.raw_source_tour(ROW)
    assert args[14] == fs.build_tour_factsheet(tour, agency=agency).render()
    # a row without its raw source (the raw row is gone): nothing stored
    empty = FakeConn(answers=[FS_ID])
    assert asyncio.run(store.save_tour_sheet(empty, {**ROW, "src_name": None}, tenant_id=TENANT,
                                             workflow="t2")) is None
    assert empty.calls == []


@pytest.mark.real_factsheet_store
def test_stamps_are_idempotent_updates_and_best_effort():
    conn = FakeConn()
    assert asyncio.run(store.stamp_generated_content(conn, "v-1", FS_ID)) is True
    assert asyncio.run(store.stamp_tenant_version(conn, "v-2", FS_ID)) is True
    assert "UPDATE silver_aa_internal.generated_content SET fs_id" in conn.calls[0][1]
    assert "fs_id IS DISTINCT FROM" in conn.calls[0][1]
    assert "UPDATE gold_aa_internal.tenant_tour_versions SET fs_id" in conn.calls[1][1]
    assert asyncio.run(store.stamp_tenant_version(conn, "v-2", None)) is False
    assert asyncio.run(store.stamp_tenant_version(FakeConn(fail="UndefinedColumnError"), "v-2", FS_ID)) is False


# ------------------------------------------------------------------ the writers' hooks

@pytest.mark.real_factsheet_store
def test_s1_stamp_stores_the_tour_sheet_and_stamps_the_version():
    from api.routers import admin_pipeline

    conn = FakeConn(answers=[FS_ID])
    assert asyncio.run(admin_pipeline._stamp_fact_sheet(conn, ROW, TENANT, "v-9")) == FS_ID
    assert conn.calls[0][2][1] == "s1" and conn.calls[1][0] == "execute"
    assert conn.calls[1][2] == (FS_ID, "v-9")


@pytest.mark.real_factsheet_store
def test_t2_store_reads_the_pt_row():
    from services.jobs import t2_rewrite_job as job

    for col in ("rt.exclusions", "rt.group_size", "rt.best_time_to_go", "rt.inclusions", "rt.src_itineraries",
                "rt.src_highlights", "rt.price_raw"):
        assert col in job._PT_SQL
    conn = FakeConn(answers=[FS_ID])
    assert asyncio.run(job._store_fact_sheet(FakePool(conn), {**ROW, "id": "p-1"}, TENANT, "p-1")) == FS_ID
    assert conn.calls[0][2][1] == "t2"


def test_t2_store_is_off_in_unit_tests_by_default():
    from services.jobs import t2_rewrite_job as job

    conn = FakeConn(answers=[FS_ID])
    assert asyncio.run(job._store_fact_sheet(FakePool(conn), ROW, TENANT, "p-1")) is None and conn.calls == []


def _t9_context():
    return {"atom_text": "Lumen Gorge — walk\nLocation: Varn, Zemlia", "facts_text": "", "tenant_id": TENANT,
            "brand_audience": {"brand_name": "Trailwise"}, "trip_name": "Zemlia Loop", "destination": "Zemlia",
            "channel": "blog", "atom_id": "atom_1", "facts_excluded": [{"fact_id": "f-1", "scope": "tenant",
                                                                           "chars": 40}]}


def test_t9_sheet_renders_the_grounding_text():
    from services.acp_content_writing import service

    ctx = _t9_context()
    sheet = service._grounding_sheet(ctx)
    assert sheet.render() == service._grounding_text(ctx)
    assert sheet.excluded[0]["reason"] == fs.EXCLUDED_FACTS_BUDGET and sheet.meta == {"channel": "blog",
                                                                                       "atom_id": "atom_1"}


@pytest.mark.real_factsheet_store
def test_t9_store_puts_the_ids_on_the_context_and_writer_input():
    from services.acp_content_writing import service

    ctx = _t9_context()
    sheet = service._grounding_sheet(ctx)
    asyncio.run(service._store_fact_sheet(FakePool(FakeConn(answers=[uuid.UUID(FS_ID)])), sheet, ctx))
    assert ctx["fs_id"] == FS_ID and ctx["fs_sha"] == sheet.sha()
    wi = service.writer_input_snapshot(ctx)
    assert wi["fs_id"] == FS_ID and wi["fs_sha"] == sheet.sha()
    # a failed store leaves no id (the sha stays: it names the sheet the write used)
    ctx2 = _t9_context()
    asyncio.run(service._store_fact_sheet(FakePool(FakeConn(fail="UndefinedTableError")), sheet, ctx2))
    assert "fs_id" not in ctx2 and ctx2["fs_sha"] == sheet.sha()
    assert "fs_id" not in service.writer_input_snapshot(ctx2)


def test_t9_store_off_keeps_writer_input_without_an_id():
    from services.acp_content_writing import service

    ctx = _t9_context()
    asyncio.run(service._store_fact_sheet(FakePool(FakeConn(answers=[FS_ID])), service._grounding_sheet(ctx), ctx))
    assert "fs_id" not in ctx and len(ctx["fs_sha"]) == 64

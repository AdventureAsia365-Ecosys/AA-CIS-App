"""AA-1093 part 3 — per-tenant CIS backup: the table catalogue against the erasure inventory, the checksum format, the
job payload and the pilot schedule, the drill's report rules (no DB).

The DB behaviour (export of every exported table, the gzip / manifest / SHA256SUMS round trip, the pg_temp restore
and its comparisons, the refusals, live tables untouched) is in tests/integration/test_aa1093_tenant_backup.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.erasure import inventory as inv
from services.erasure import plan as pl
from services.jobs import tenant_backup_job as tbj
from services.tenant_backup import catalogue as cat
from services.tenant_backup import export, restore_drill
from shared.jobs.registry import NonRetryable, get_kind

TID = "11111111-2222-4333-8444-555555555555"


def _edge(con, child, parent, ccols=("parent_id",), pcols=("id",)):
    return pl.Edge(con, child, parent, tuple(ccols), tuple(pcols), True, "a")


def _full_catalog(extra_edges=()) -> pl.Catalog:
    """Every inventory table present with a uuid tenant_id (so each one is planned), plus extra FK children."""
    tables = {t.table for t in inv.TENANT_TABLES}
    for e in extra_edges:
        tables |= {e.child, e.parent}
    cols = {t.table: "uuid" for t in inv.TENANT_TABLES if not t.no_tenant_column}
    return pl.Catalog(tables, cols, list(extra_edges), {tb: ("tenant_id",) for tb in cols})


# ── catalogue ───────────────────────────────────────────────────────────────────────────────────

def test_every_exclusion_names_an_inventory_table():
    assert set(cat.EXCLUDED) <= set(inv.TENANT_TABLE_BY_NAME)


def test_every_inventory_table_is_either_exported_or_excluded_with_a_reason():
    plan = pl.plan_tenant(inv.Registry(), _full_catalog())
    bp = cat.backup_plan(plan)
    exported = {t.table for t in bp.tables}
    excluded = {e["table"]: e["reason"] for e in bp.excluded}
    assert not exported & set(excluded)
    assert exported | set(excluded) == set(plan.nodes) == set(inv.TENANT_TABLE_BY_NAME)
    assert all(r and ":" in r for r in excluded.values())
    # what the erasure deletes is backed up, except the named exclusions; the ledgers it keeps are not
    for t in inv.TENANT_TABLES:
        if t.action == "delete" and t.table not in cat.EXCLUDED:
            assert t.table in exported, t.table
        if t.action in ("detach", "merge", "keep"):
            assert t.table in excluded, t.table
    assert "shared.tenants" in exported and "silver_aa_internal.raw_tours" in exported
    assert "acp_contract.tour_atoms" in exported
    assert excluded["acp_shared.audit_log"].startswith("audit_chain")
    assert excluded["shared.llm_call_log"].startswith("platform_ledger")


def test_tenant_rows_only_never_the_shared_catalogue():
    """Every exported table is read through the erasure predicate of the tenant ($1), so the AA master catalogue (the
    platform tenant's rows) is never selected."""
    bp = cat.backup_plan(pl.plan_tenant(inv.Registry(), _full_catalog()))
    for t in bp.tables:
        assert "$1" in t.where, t.table
    atoms = next(t for t in bp.tables if t.table == "acp_contract.tour_atoms")
    assert "owner_scope = $1::text" in atoms.where and "tenant_id = $1::uuid" in atoms.where


def test_cascade_children_follow_their_parents():
    edges = [_edge("v_fk", "acp_shared.quarter_plan_version_x", "acp_shared.quarter_plan", ("plan_id",), ("plan_id",)),
             _edge("j_fk", "shared.job_note_x", "shared.job", ("job_id",)),
             _edge("g_fk", "shared.job_note_child_x", "shared.job_note_x", ("note_id",))]
    bp = cat.backup_plan(pl.plan_tenant(inv.Registry(), _full_catalog(edges)))
    exported = [t.table for t in bp.tables]
    excluded = {e["table"]: e["reason"] for e in bp.excluded}
    assert "acp_shared.quarter_plan_version_x" in exported
    assert exported.index("acp_shared.quarter_plan") < exported.index("acp_shared.quarter_plan_version_x")
    assert excluded["shared.job_note_x"].startswith("child_of_excluded")
    assert excluded["shared.job_note_child_x"].startswith("child_of_excluded")
    assert bp.restore_order == exported


def test_unregistered_tables_are_carried_to_the_manifest():
    catalog = _full_catalog()
    catalog.tables.add("shared.new_tenant_table_x")
    catalog.tenant_columns["shared.new_tenant_table_x"] = "uuid"
    bp = cat.backup_plan(pl.plan_tenant(inv.Registry(), catalog))
    assert bp.unregistered == ["shared.new_tenant_table_x"]


@pytest.mark.parametrize("bad, code", [("not-a-uuid", "bad_tenant_id"), (None, "bad_tenant_id"),
                                       (inv.PLATFORM_TENANT_ID, "platform_tenant")])
def test_check_tenant_id_refuses(bad, code):
    with pytest.raises(cat.BackupRefused) as e:
        cat.check_tenant_id(bad)
    assert e.value.code == code


def test_object_layout():
    assert cat.object_prefix(TID, "b1") == f"exports/tenants/{TID}/cis/b1/"
    assert cat.object_prefix(TID, "b1").startswith("exports/")      # the bucket's 7-day rule covers it
    assert cat.table_file("shared.tenants") == "tables/shared.tenants.jsonl.gz"


# ── checksums ───────────────────────────────────────────────────────────────────────────────────

def test_sums_round_trip_and_shape(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"abc")
    d = export.sha256_file(str(p))
    assert d == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    text = export.sums_text({"tables/b.jsonl.gz": "1" * 64, "manifest.json": d})
    assert text.splitlines()[0].endswith("  manifest.json")
    assert export.parse_sums(text) == {"tables/b.jsonl.gz": "1" * 64, "manifest.json": d}
    with pytest.raises(ValueError):
        export.parse_sums("abc manifest.json\n")


@pytest.mark.parametrize("raw, expected", [("", export.DEFAULT_MAX_BYTES), ("1000", 1000), ("0", 1),
                                           ("x", export.DEFAULT_MAX_BYTES)])
def test_max_bytes_from_env(raw, expected):
    assert export.max_bytes_from_env({"TENANT_BACKUP_MAX_BYTES": raw}) == expected


# ── job ─────────────────────────────────────────────────────────────────────────────────────────

def test_kind_is_registered_as_background():
    from shared.jobs.worker import load_kinds

    load_kinds()
    k = get_kind(tbj.KIND)
    assert k is not None and k.background and k.concurrency == 1 and not k.deploy_blocking


def test_validate_payload():
    assert tbj.validate_payload({"tenant_id": TID.upper()}) == {"tenant_id": TID, "trigger": "manual"}
    assert tbj.validate_payload({"tenant_id": TID, "trigger": "schedule"})["trigger"] == "schedule"
    for bad in ({}, {"tenant_id": "x"}, {"tenant_id": inv.PLATFORM_TENANT_ID}):
        with pytest.raises(NonRetryable):
            tbj.validate_payload(bad)


@pytest.mark.parametrize("raw, on", [(None, False), ("", False), ("off", False), ("on", True), ("TRUE", True)])
def test_schedule_is_off_by_default(raw, on):
    env = {} if raw is None else {tbj.SWITCH_ENV: raw}
    assert tbj.schedule_enabled(env) is on


def _pool(exists=False, pilot=None):
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=lambda sql, *a: exists if "EXISTS" in sql else pilot)
    return pool


@pytest.mark.asyncio
async def test_pilot_nightly(monkeypatch):
    enq = AsyncMock(return_value=("j1", True))
    monkeypatch.setattr(tbj, "enqueue", enq)
    early = datetime(2026, 11, 13, tbj.SCHEDULE_HOUR_UTC - 1, 59, tzinfo=timezone.utc)
    late = datetime(2026, 11, 13, tbj.SCHEDULE_HOUR_UTC, 1, tzinfo=timezone.utc)
    key = "tenant_backup:pilot:2026-11-13"
    assert await tbj.enqueue_pilot_nightly(_pool(pilot=TID), now=early) == (None, False, key)
    assert (await tbj.enqueue_pilot_nightly(_pool(exists=True, pilot=TID), now=late))[:2] == (None, False)
    assert (await tbj.enqueue_pilot_nightly(_pool(pilot=None), now=late))[:2] == (None, False)
    enq.assert_not_called()
    assert await tbj.enqueue_pilot_nightly(_pool(pilot=TID), now=late) == ("j1", True, "tenant_backup:pilot:2026-11-13")
    args, kw = enq.call_args
    assert args[1:] == (tbj.KIND, {"tenant_id": TID, "trigger": "schedule"})
    assert kw == {"idempotency_key": "tenant_backup:pilot:2026-11-13", "created_by": tbj.SCHEDULER}


@pytest.mark.asyncio
async def test_sampler_part_respects_the_switch(monkeypatch):
    from api.core import ops_metrics_sampler as oms

    called = AsyncMock(return_value=("j1", True, "k"))
    monkeypatch.setattr(tbj, "enqueue_pilot_nightly", called)
    out: dict = {}
    monkeypatch.delenv(tbj.SWITCH_ENV, raising=False)
    await oms._tenant_backup_part(MagicMock(), {tbj.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {} and not called.called
    monkeypatch.setenv(tbj.SWITCH_ENV, "on")
    await oms._tenant_backup_part(MagicMock(), {}, datetime.now(timezone.utc), out)
    assert out == {} and not called.called
    await oms._tenant_backup_part(MagicMock(), {tbj.KIND: object()}, datetime.now(timezone.utc), out)
    assert out == {"tenant_backup": {"job_id": "j1", "key": "k"}}


@pytest.mark.asyncio
async def test_run_turns_a_refusal_into_non_retryable(monkeypatch):
    async def refuse(*a, **kw):
        raise cat.BackupRefused("no_tenant", "no row")

    monkeypatch.setattr(export, "run_backup", refuse)
    ctx = MagicMock()
    ctx.payload = {"tenant_id": TID}
    ctx.resources = {"tenant_backup_store": object()}
    with pytest.raises(NonRetryable, match="no_tenant"):
        await tbj.run(ctx)


# ── drill report ────────────────────────────────────────────────────────────────────────────────

def test_failure_never_prints_an_error_message():
    class PgError(Exception):
        sqlstate = "22P02"

    out = restore_drill.failure(PgError("invalid input syntax: 'a value from a row'"))
    assert out == {"ok": False, "error": "unexpected", "error_class": "PgError", "sqlstate": "22P02"}
    assert restore_drill.failure(restore_drill.DrillError("no_backup")) == {"ok": False, "error": "no_backup"}


@pytest.mark.parametrize("report, code", [({"ok": True, "complete": True}, 0), ({"ok": True, "complete": False}, 3),
                                          ({"ok": False, "complete": False}, 1), ({}, 1)])
def test_exit_code(report, code):
    assert restore_drill.exit_code(report) == code


# ── store and the drill's batch reader ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dir_store_round_trip(tmp_path):
    from services.tenant_backup.store import DirStore, ObjectMissing

    src = tmp_path / "src.txt"
    src.write_text("one line\n")
    store = DirStore(str(tmp_path / "bucket"))
    await store.put_file("exports/tenants/t/cis/b/manifest.json", str(src), "application/json")
    assert await store.list_keys("exports/tenants/t/") == ["exports/tenants/t/cis/b/manifest.json"]
    out = tmp_path / "out.txt"
    await store.get_file("exports/tenants/t/cis/b/manifest.json", str(out))
    assert out.read_text() == "one line\n"
    with pytest.raises(ObjectMissing):
        await store.get_file("exports/tenants/t/cis/b/SHA256SUMS", str(out))


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.extra: dict[str, dict] = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[key] = open(path, "rb").read()
        self.extra[key] = ExtraArgs

    def download_file(self, bucket, key, path):
        if key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "404"}}
            raise err
        open(path, "wb").write(self.objects[key])

    def list_objects_v2(self, **kw):
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        if "ContinuationToken" not in kw:
            return {"Contents": [{"Key": k} for k in keys[:1]], "IsTruncated": len(keys) > 1,
                    "NextContinuationToken": "n"}
        return {"Contents": [{"Key": k} for k in keys[1:]], "IsTruncated": False}


@pytest.mark.asyncio
async def test_s3_store_asks_for_sse_and_maps_a_missing_object(tmp_path, monkeypatch):
    from services.tenant_backup.store import ObjectMissing, S3Store, gold_bucket

    monkeypatch.setenv("GOLD_BUCKET", "gold-test")
    assert gold_bucket() == "gold-test"
    client = _FakeS3Client()
    store = S3Store(client=client)
    assert store.bucket == "gold-test"
    src = tmp_path / "a"
    src.write_bytes(b"x")
    await store.put_file("exports/k1", str(src), "text/plain")
    await store.put_file("exports/k2", str(src), "text/plain")
    assert client.extra["exports/k1"] == {"ServerSideEncryption": "AES256", "ContentType": "text/plain"}
    assert await store.list_keys("exports/") == ["exports/k1", "exports/k2"]
    await store.get_file("exports/k1", str(tmp_path / "b"))
    with pytest.raises(ObjectMissing):
        await store.get_file("exports/nope", str(tmp_path / "c"))


def test_batches_split_by_rows_and_skip_blank_lines(tmp_path, monkeypatch):
    import gzip

    path = tmp_path / "t.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("".join(f'{{"n": {i}}}\n' for i in range(5)) + "\n")
    monkeypatch.setattr(restore_drill, "BATCH_ROWS", 2)
    assert [len(b) for b in restore_drill._batches(str(path))] == [2, 2, 1]

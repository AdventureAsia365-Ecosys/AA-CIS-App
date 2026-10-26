"""AA-919 (WM-17) — the importer CLI (services/golden_set/importer.py) and the $0 guard (llm_guard), no DB.

Covers: DRY RUN with --no-db prints counts only (no text) and writes nothing; s3:<key> reads the bronze bucket named by
the environment and stores only the key (an s3:// URI naming a bucket is refused); a bad fixture exits 2 with the
reason; --apply needs the DB; the importer installs the LLM guard before reading anything; the guard makes every
Model Gateway entry point raise and its undo restores them; store.plan's refusals on a fake connection (another sha,
missing / offboarded tenant).
"""
from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone

import pytest

from services.golden_set import derive, importer, llm_guard, store
from tests.unit.aa919_fake_golden import TENANT_A, TENANT_B, fake_fixture


@pytest.fixture
def no_permanent_guard(monkeypatch):
    """main() installs the guard for the rest of the process; undo it after each test."""
    undos = []
    real = llm_guard.install

    def install(reason="x"):
        undo = real(reason)
        undos.append(undo)
        return undo
    monkeypatch.setattr(llm_guard, "install", install)
    yield undos
    for undo in reversed(undos):
        undo()


def _fixture_file(tmp_path, fx=None):
    p = tmp_path / "fake-1.0.0.json"
    p.write_text(json.dumps(fx or fake_fixture()), encoding="utf-8")
    return p


def test_dry_run_no_db_prints_counts_only(tmp_path, capsys, no_permanent_guard):
    code = importer.main(["--fixture", str(_fixture_file(tmp_path)), "--no-db", "--name", "zz919"])
    out = capsys.readouterr().out
    rep = json.loads(out)
    assert code == 0 and rep["mode"] == "dry_run" and rep["db"].startswith("not checked")
    assert rep["set"]["source_uri"] == "file:fake-1.0.0.json" and len(rep["set"]["source_sha256"]) == 64
    assert rep["counts"]["items"] == 24 and rep["counts"]["traps"] == 4
    assert rep["baseline"]["t2"]["skipped_post_edit"] == 1
    assert sorted(rep["tenants"]) == sorted([TENANT_A, TENANT_B])
    for phrase in ("pretend", "Pretend", "invented", "made-up"):
        assert phrase not in out
    assert len(no_permanent_guard) == 1                                # the guard was installed


def test_s3_key_reads_the_env_bucket_and_stores_the_key_only(tmp_path, capsys, monkeypatch, no_permanent_guard):
    body = json.dumps(fake_fixture()).encode()
    calls = []

    class FakeS3:
        def get_object(self, Bucket, Key):
            calls.append((Bucket, Key))
            return {"Body": io.BytesIO(body)}
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeS3())
    monkeypatch.setenv("BRONZE_BUCKET", "zz-bronze-test")
    assert importer.main(["--fixture", "s3:scripts/out/aa729/fixture-1.0.0.json", "--no-db"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert calls == [("zz-bronze-test", "scripts/out/aa729/fixture-1.0.0.json")]
    assert rep["set"]["source_uri"] == "s3:scripts/out/aa729/fixture-1.0.0.json"
    with pytest.raises(SystemExit, match="bucket comes from the environment"):
        importer.main(["--fixture", "s3://some-bucket/key.json", "--no-db"])


def test_bad_fixture_exits_2_with_the_reason(tmp_path, capsys, no_permanent_guard):
    fx = fake_fixture()
    fx["samples"][0]["kind"] = "s9"
    assert importer.main(["--fixture", str(_fixture_file(tmp_path, fx)), "--no-db"]) == 2
    assert "unknown kind" in json.loads(capsys.readouterr().out)["refused"]


def test_apply_needs_the_db(tmp_path, no_permanent_guard):
    with pytest.raises(SystemExit):
        importer.main(["--fixture", str(_fixture_file(tmp_path)), "--apply", "--no-db"])


# ── the $0 guard ────────────────────────────────────────────────────────────────────────────────────────────────


def test_llm_guard_blocks_every_gateway_entry_point_and_undoes():
    from shared.llm_client import bedrock_satellite, client, embed

    before = {name: client.LLMClient.__dict__[name] for name in ("generate", "_generate")}
    undo = llm_guard.install("pytest")
    try:
        with pytest.raises(llm_guard.LLMCallForbidden):
            client.LLMClient.generate(object(), None)
        with pytest.raises(llm_guard.LLMCallForbidden):
            bedrock_satellite.invoke_claude("x")
        with pytest.raises(llm_guard.LLMCallForbidden):
            embed._call(None, "acc3", ["x"], "search_document", None)
    finally:
        undo()
    assert {name: client.LLMClient.__dict__[name] for name in ("generate", "_generate")} == before


def test_main_installs_the_guard_before_reading(tmp_path, monkeypatch, no_permanent_guard):
    order = []
    monkeypatch.setattr(importer, "prepare", lambda *a, **k: order.append("prepare") or (_ for _ in ()).throw(
        derive.FixtureError("stop")))
    real = llm_guard.install
    monkeypatch.setattr(llm_guard, "install", lambda reason="x": order.append("guard") or no_permanent_guard.append(
        real(reason)))
    importer.main(["--fixture", str(_fixture_file(tmp_path)), "--no-db"])
    assert order == ["guard", "prepare"]


# ── store.plan on a fake connection ─────────────────────────────────────────────────────────────────────────────


class FakeConn:
    def __init__(self, existing=None, active=None, tenants=()):
        self.existing, self.active, self.tenants = existing, active, list(tenants)

    async def fetchrow(self, sql, *args):
        return self.existing if "version = $2" in sql else self.active

    async def fetch(self, sql, *args):
        return self.tenants


def _gs():
    return derive.build(fake_fixture(), name="zz919", source_uri="file:x.json", source_sha256="c" * 64,
                        imported_by="pytest")


def _tenant(tid, cancelled=False):
    return {"tenant_id": tid, "cancelled_at": datetime.now(timezone.utc) if cancelled else None, "is_active": True}


def test_plan_insert_when_new_and_tenants_ok():
    p = asyncio.run(store.plan(FakeConn(tenants=[_tenant(TENANT_A), _tenant(TENANT_B)]), _gs()))
    assert p == {"action": "insert", "existing_set_id": None, "supersedes": None, "problems": []}


def test_plan_refuses_another_sha_missing_and_offboarded_tenants():
    existing = {"set_id": "s", "status": "active", "source_sha256": "d" * 64, "item_count": 24}
    p = asyncio.run(store.plan(FakeConn(existing=existing, active={"set_id": "s", "version": "1.0.0"},
                                        tenants=[_tenant(TENANT_A, cancelled=True)]), _gs()))
    assert p["action"] == "exists" and p["supersedes"] is None
    assert any("offboarded" in x for x in p["problems"])
    assert any("is not in shared.tenants" in x and TENANT_B in x for x in p["problems"])
    assert any("other bytes" in x for x in p["problems"])


def test_plan_reports_the_version_it_supersedes():
    p = asyncio.run(store.plan(FakeConn(active={"set_id": "old", "version": "0.9.0"},
                                        tenants=[_tenant(TENANT_A), _tenant(TENANT_B)]), _gs()))
    assert p["supersedes"] == {"set_id": "old", "version": "0.9.0"}


@pytest.mark.parametrize("active", ["1.1.0", "1.0.0", "0.10.0"])
def test_plan_refuses_a_version_not_newer_than_the_active_one(active):
    # the fake fixture is 1.0.0; semver compares numbers ("0.10.0" < "1.0.0" but "1.0.0" is not newer than itself)
    ok = [_tenant(TENANT_A), _tenant(TENANT_B)]
    p = asyncio.run(store.plan(FakeConn(active={"set_id": "act", "version": active}, tenants=ok), _gs()))
    if active == "0.10.0":
        assert p["problems"] == [] and p["supersedes"] == {"set_id": "act", "version": "0.10.0"}
        return
    want = f"1.0.0 is not newer than the active {active} (--allow-downgrade to make an older version active on purpose)"
    assert p["problems"] == [want]
    forced = asyncio.run(store.plan(FakeConn(active={"set_id": "act", "version": active}, tenants=ok), _gs(),
                                    allow_downgrade=True))
    assert forced["problems"] == [] and forced["supersedes"] == {"set_id": "act", "version": active}


def test_plan_of_an_existing_superseded_version_supersedes_nothing():
    existing = {"set_id": "old", "status": "superseded", "source_sha256": "c" * 64, "item_count": 24}
    p = asyncio.run(store.plan(FakeConn(existing=existing, active={"set_id": "new", "version": "1.1.0"},
                                        tenants=[_tenant(TENANT_A), _tenant(TENANT_B)]), _gs()))
    assert (p["action"], p["supersedes"], p["problems"]) == ("exists", None, [])


def test_allow_downgrade_flag_reaches_the_store(tmp_path, monkeypatch, no_permanent_guard):
    seen = {}

    async def fake_run(gs, baseline, *, apply, conn=None, allow_downgrade=False):
        seen["allow_downgrade"] = allow_downgrade
        return {"ok": True}

    async def fake_connect():
        class C:
            async def close(self):
                pass
        return C()

    monkeypatch.setattr(importer, "run", fake_run)
    monkeypatch.setattr(importer, "_connect", fake_connect)
    p = tmp_path / "f.json"
    p.write_text(json.dumps(fake_fixture()))
    assert importer.main(["--fixture", str(p), "--allow-downgrade"]) == 0
    assert seen == {"allow_downgrade": True}

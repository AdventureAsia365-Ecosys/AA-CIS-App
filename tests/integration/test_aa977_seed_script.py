"""AA-977 S4 — scripts/aa977_seed_aa_content.py on real Postgres (CIS 275) as the CIS runtime role cis_app, the way
it runs in the api container. Each test runs in one transaction that is rolled back (the store's own transactions
become savepoints).

DRY RUN writes nothing; --apply --publish creates the 28 seed items and publishes 27 (one prep pack VI kept as a
draft: rule R, never served); a second run is a no-op; --expect refuses a different count; --edit-demo publishes v2 of
a pack while v1 stays fetchable with the same hash (rule E).
"""
import argparse
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from services.aa_content import reader  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("aa977_seed", REPO / "scripts" / "aa977_seed_aa_content.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)
ITEMS, SHA = seed.load_seed(seed.DEFAULT_SEED)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(migration_body(275))          # other modules re-create cis_app (its grants go with it)
        await c.execute("GRANT USAGE ON SCHEMA gold_aa_internal TO cis_app")   # 244 on Dev; the replay skips it
        await c.execute("SET LOCAL ROLE cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _args(**kw):
    base = {"apply": False, "publish": False, "expect": None, "edit_demo": None}
    return argparse.Namespace(**{**base, **kw})


async def _go(conn, **kw):
    return await seed.execute(conn, _args(**kw), ITEMS, {"seed_sha256": SHA})


async def _served(conn, kind, locale):
    page = await reader.list_published(conn, after=None, kind=kind, locale=locale, limit=50)
    return {i["content_id"]: i for i in page.items}


async def _count(conn):
    return await conn.fetchval("SELECT count(*) FROM gold_aa_internal.aa_content_item")


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(conn):
    code, out = await _go(conn)
    assert code == 0 and "created" not in out
    assert out["plan"]["counts"]["to_create"] == 28 and out["plan"]["counts"]["to_publish"] == 27
    assert out["plan"]["kept_draft"] == ["prep-pack-bhutan:vi"]
    assert await _count(conn) == 0


@pytest.mark.asyncio
async def test_expect_mismatch_refuses_before_writing(conn):
    code, out = await _go(conn, apply=True, expect=48)
    assert code == 2 and "48" in out["error"]
    assert await _count(conn) == 0


@pytest.mark.asyncio
async def test_apply_publish_then_rerun_is_a_no_op(conn):
    code, out = await _go(conn, apply=True, publish=True, expect=28)
    assert code == 0 and out["failed"] == []
    assert len(out["created"]) == 28 and len(out["published"]) == 27
    assert all(p["version"] == 1 and p["content_hash"].startswith("sha256:") for p in out["published"])
    assert [k["item"] for k in out["kept_draft"]] == ["prep-pack-bhutan:vi"]

    rows = await conn.fetch("SELECT created_by, count(*) AS n FROM gold_aa_internal.aa_content_item GROUP BY 1")
    assert [(r["created_by"], r["n"]) for r in rows] == [("seed:AA-977", 28)]

    # Served: 10 EN prep packs, 9 VI (the kept draft is never served: rule R), templates and blocks in both.
    assert len(await _served(conn, "prep_pack", "en")) == 10
    vi = await _served(conn, "prep_pack", "vi")
    kept_id = out["kept_draft"][0]["content_id"]
    assert len(vi) == 9 and kept_id not in vi
    assert await reader.get_published_version(conn, uuid.UUID(kept_id), 1) is None
    assert len(await _served(conn, "starter_template", "en")) == 2
    assert len(await _served(conn, "campaign_block", "vi")) == 2

    code, again = await _go(conn, apply=True, publish=True)
    assert code == 0 and again["created"] == [] and again["published"] == []
    assert again["plan"]["counts"]["to_create"] == 0
    assert len(again["skipped_publish"]) == 27 and await _count(conn) == 28


@pytest.mark.asyncio
async def test_edit_demo_publishes_v2_and_keeps_v1(conn):
    await _go(conn, apply=True, publish=True)
    before = await _served(conn, "prep_pack", "en")
    code, out = await _go(conn, edit_demo="prep-pack-vietnam:en")
    assert code == 0, out
    demo = out["edit_demo"]
    assert demo["new_version"] == 2 and demo["old_version_unchanged"] is True
    assert [v["version"] for v in demo["versions"]] == [2, 1]
    cid = uuid.UUID(demo["content_id"])
    v1 = await reader.get_published_version(conn, cid, 1)
    assert v1["content_hash"] == before[str(cid)]["content_hash"]
    v2 = await reader.get_published_version(conn, cid, 2)
    assert "Last reviewed by the Adventure Asia team" in v2["body_markdown"]
    assert (await _served(conn, "prep_pack", "en"))[str(cid)]["version"] == 2

    # On the kept draft (never published) the demo is refused.
    code, out = await _go(conn, edit_demo="prep-pack-bhutan:vi")
    assert code == 2 and "published item with no draft" in out["error"]

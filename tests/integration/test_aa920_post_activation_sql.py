"""AA-920 — the SQL of the insertion path and the post-activation check on the real schema (every migration applied).

The unit tests (tests/unit/test_aa920_injection.py) cover the logic on canned rows; this file proves the queries name
real tables and columns (Postgres plans each one even when it returns no row): the resolver and the rule items
(`insertion`), the writes since an activation for each writer, the live tenants' packs and the active content releases
(`post_activation`), and the sampler's EXISTS. One transaction per test, rolled back; nothing is seeded but what a
test names.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import ops_metrics_sampler as sampler  # noqa: E402
from services.writing_memory import insertion  # noqa: E402
from services.writing_memory import post_activation as pa  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _release(writer, scope="platform", tenant=None):
    return {"release_id": str(uuid.uuid4()), "writer": writer, "scope": scope, "tenant_id": tenant,
            "activated_at": datetime.now(timezone.utc) - timedelta(days=1), "retired_at": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["a1", "t2", "t9_blog", "t9_social"])
async def test_writes_since_runs_for_every_writer(conn, writer):
    if writer == "t2" and await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
        pytest.skip("the integration schema has no gold_aa_internal.tenant_tour_versions (created outside the replay)")
    rows = await pa.writes_since(conn, _release(writer), limit=5)
    assert isinstance(rows, list)
    if writer != "a1":
        rows = await pa.writes_since(conn, _release(writer, "tenant", str(uuid.uuid4())), limit=5)
        assert rows == []


@pytest.mark.asyncio
async def test_content_releases_tenants_and_the_sampler_exists_run(conn):
    assert isinstance(await pa.content_releases(conn), list)
    assert isinstance(await pa.tenant_fingerprints(conn), dict)
    assert await conn.fetchval(sampler._CONTENT_RELEASE_SQL) in (True, False)
    assert await pa.release_rule_texts(conn, {"rule_ids": [str(uuid.uuid4())]}) == []


@pytest.mark.asyncio
async def test_resolve_and_load_items_run_on_the_real_tables(conn):
    # the writer rows are seeded with memory off (CIS 287): no view, the writer runs as without memory
    assert await insertion.resolve(conn, "t9_social", str(uuid.uuid4())) is None
    row = {"release_id": str(uuid.uuid4()), "scope": "platform", "tenant_id": None, "rule_ids": [str(uuid.uuid4())],
           "items_sha256": "0" * 64}
    assert await insertion.load_items(conn, row, "a1") is None          # an unknown rule: refused, renders nothing
    assert await insertion.load_items(conn, {**row, "rule_ids": []}, "a1") == []


@pytest.mark.asyncio
async def test_a_drafted_release_renders_its_rules_with_the_drafted_fingerprint(conn):
    """The hash `create_draft` stores (releases.items_sha256 over its rule rows) is the hash `load_items` checks, so a
    real release with rules renders them and stamps the same fingerprint; an edited rule text renders nothing."""
    from services.writing_memory import releases as rel
    from services.writing_memory import rules as rules_mod

    actor = "test:aa920"
    await rel.create_draft(conn, writer="t9_blog", created_by=actor)              # release 0 (empty) first
    rid = await rules_mod.insert_candidate(
        conn, kind="writer_instruction", scope="platform", tenant_id=None, writer="t9_blog",
        cluster_key=f"zz920:{uuid.uuid4().hex[:8]}", rule_text="Cut any sentence that only restates the heading.",
        lint={"ok": True})
    assert rid
    draft = await rel.create_draft(conn, writer="t9_blog", rule_ids=[rid], created_by=actor)
    assert draft["release_no"] == 1 and draft["content_class"] == "rules"
    row = await insertion.attach_items(conn, draft, "t9_blog")
    view = {"writer": "t9_blog", "platform": insertion.part_of(row), "tenant": None}
    assert insertion.t9_platform_block(view, "blog").endswith("- Cut any sentence that only restates the heading.")
    assert insertion.stamp(view)["platform"]["items_sha256"] == draft["items_sha256"]
    assert insertion.stamp(view)["ignored"] is False
    await conn.execute("UPDATE shared.writing_rule SET rule_text = 'Cut every restated heading.' "
                       "WHERE rule_id = $1::uuid", rid)
    edited = await insertion.attach_items(conn, draft, "t9_blog")
    assert "items" not in edited
    assert insertion.stamp({"writer": "t9_blog", "platform": insertion.part_of(edited), "tenant": None})["ignored"]

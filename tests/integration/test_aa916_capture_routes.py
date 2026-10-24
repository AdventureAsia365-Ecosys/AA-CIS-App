"""AA-916 — the admin capture paths on real Postgres (CIS 277 replayed by conftest).

  * WM-05: PATCH /admin/tours/{tour_id}/generated/{content_id} with 2 changed fields (one JSONB) and 1 unchanged writes
    2 platform signals (writer a1, tenant_id NULL) in the UPDATE's transaction; generated_content.edit_diff names
    them; human_edited / revalidate_passed as before. A tenant's own tour gives tenant-scope signals.
  * the edit-history reader: a signal id of another tenant never resolves (explicit tenant filter for cis_app).
  * WM-06: dismiss with a reason code writes a reason-only signal; an unknown code is refused by the CHECK too.
Each test runs in one transaction that is rolled back.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_pipeline as ap  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "s3cret"
MASTER = wm.PLATFORM_TENANT_ID


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        # Dev has it (107); the CI replay fails 107 (no tenant_tour_versions), so add it in this transaction
        await c.execute("ALTER TABLE silver_aa_internal.review_queue "
                        "ADD COLUMN IF NOT EXISTS tenant_tour_version_id uuid")
        await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'AA master', "
                        "'aa-master-zz916', 3) ON CONFLICT (tenant_id) DO NOTHING", uuid.UUID(MASTER))
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"AA-916 {tid.hex[:8]}", f"zz916-{tid.hex[:8]}")
    return tid


async def _version(c, tenant_id):
    tour_id, content_id = uuid.uuid4(), uuid.uuid4()
    await c.execute("INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country) "
                    "VALUES ($1, $2, gen_random_uuid(), 'aa916 test', 'Laos')", tour_id, uuid.UUID(str(tenant_id)))
    await c.execute("INSERT INTO silver_aa_internal.generated_content (id, tour_id, tenant_id, version_num, aa_name, "
                    "aa_summary, aa_highlights) VALUES ($1, $2, $3, 1, 'Laos Loop', 'Old summary.', $4::jsonb)",
                    content_id, tour_id, uuid.UUID(str(tenant_id)), json.dumps(["b", "a"]))
    return str(tour_id), str(content_id)


def _request(c, body=None):
    async def _json():
        return body
    # no x-admin-user-id (a session without a JWT): reviewed_by stays NULL, the signal's actor is the handle
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(c))), json=_json,
                           headers={"x-reviewer-id": "ana"})


@pytest.mark.asyncio
async def test_a1_patch_with_two_changed_fields_writes_two_platform_signals(conn):
    tour_id, content_id = await _version(conn, MASTER)
    body = {"aa_summary": "New summary.", "aa_highlights": ["c"], "aa_name": "Laos Loop"}
    out = await ap.update_generated_content_fields(tour_id, content_id, _request(conn, body), x_admin_secret=SECRET)
    assert out == {"content_id": content_id, "updated": list(body), "human_edited": True, "revalidate_required": True}
    rows = await conn.fetch("SELECT signal_id::text AS id, writer, scope, tenant_id, field, before_text, after_text, "
                            "action, actor_type, actor_id FROM shared.writing_signal "
                            "WHERE source_table = 'generated_content' AND source_id = $1 ORDER BY field", content_id)
    assert [(r["field"], r["before_text"], r["after_text"]) for r in rows] == [
        ("aa_highlights", '["b", "a"]', '["c"]'), ("aa_summary", "Old summary.", "New summary.")]
    assert {(r["writer"], r["scope"], r["tenant_id"], r["action"], r["actor_type"], r["actor_id"]) for r in rows} == {
        ("a1", "platform", None, "field_edit", "admin", "ana")}
    gc = await conn.fetchrow("SELECT human_edited, revalidate_passed, edit_diff, aa_summary "
                             "FROM silver_aa_internal.generated_content WHERE id = $1::uuid", content_id)
    diff = json.loads(gc["edit_diff"])
    assert gc["human_edited"] is True and gc["revalidate_passed"] is None and gc["aa_summary"] == "New summary."
    assert sorted(diff["signal_ids"]) == sorted(r["id"] for r in rows) and diff["fields"] == list(body)


@pytest.mark.asyncio
async def test_a1_patch_of_a_tenant_tour_is_tenant_scope(conn):
    tenant = await _tenant(conn)
    tour_id, content_id = await _version(conn, tenant)
    await ap.update_generated_content_fields(tour_id, content_id, _request(conn, {"aa_summary": "New."}),
                                             x_admin_secret=SECRET)
    row = await conn.fetchrow("SELECT scope, tenant_id FROM shared.writing_signal WHERE source_id = $1", content_id)
    assert tuple(row) == ("tenant", tenant)


@pytest.mark.asyncio
async def test_history_reader_never_resolves_another_tenants_signal(conn):
    a, b = await _tenant(conn), await _tenant(conn)
    ref = await wm.record_writer_feedback(conn, writer="t9_blog", scope="tenant", tenant_id=a, action="sentence_cut",
                                          actor_type="admin", source_table="content_piece", source_id="p",
                                          before_text="A's sentence.", after_text="",
                                          problem="'A's fact' not present")
    edits = [{"details": {"signal_id": ref.signal_id, "mode": "cut"}}]
    own = (await wm.with_signal_texts(conn, edits, a))[0]["details"]
    assert own["before"] == "A's sentence." and own["problem"] == "'A's fact' not present"   # problem: signal only
    assert (await wm.with_signal_texts(conn, edits, b))[0]["details"] == {"signal_id": ref.signal_id, "mode": "cut"}
    assert (await wm.with_signal_texts(conn, edits, MASTER))[0]["details"].get("before") is None


@pytest.mark.asyncio
async def test_dismiss_with_a_code_writes_a_reason_only_signal(conn):
    tour_id, content_id = await _version(conn, MASTER)
    review = await conn.fetchval("INSERT INTO silver_aa_internal.review_queue (tour_id, tenant_id, "
                                 "generated_content_id) VALUES ($1::uuid, $2::uuid, $3::uuid) RETURNING id::text",
                                 tour_id, MASTER, content_id)
    out = await ap.admin_dismiss_review(review, _request(conn), ap.ReviewReasonBody(reason_code="ai_phrasing"),
                                        x_admin_secret=SECRET, x_admin_user_id="u9")
    assert out["status"] == "dismissed"
    row = await conn.fetchrow("SELECT writer, scope, tenant_id, reason_code, action, before_text, after_text, "
                              "fact_status FROM shared.writing_signal WHERE source_table = 'review_queue' "
                              "AND source_id = $1", review)
    assert tuple(row) == ("a1", "platform", None, "ai_phrasing", "dismiss", None, None, "pending")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE shared.writing_signal SET reason_code = 'bogus' WHERE source_id = $1", review)

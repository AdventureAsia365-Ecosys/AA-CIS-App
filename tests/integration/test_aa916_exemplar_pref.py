"""AA-916 WM-07 — the "use as example" / "never use as example" PATCH on an admin sentence edit, on real Postgres
(CIS 277 replayed by conftest).

  * PATCH /admin/content-quality/pieces/{piece_id}/signals/{signal_id} sets and clears `exemplar_pref` on a signal of
    that piece; the T2 route does the same for a tour version.
  * the owner filter is explicit (cis_app sees every row): a signal of another piece, of another tenant, or a
    reason-only signal (reject / dismiss / regenerate) is a 404 and nothing changes.
  * unknown / malformed ids are 404; a non-admin role is 403; an unknown value or an extra key is refused by the body.
Each test runs in one transaction that is rolled back.
"""
import os
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TTV_STANDIN_DDL  # noqa: E402
from api.routers import admin_content_quality as cq  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"AA-916x {tid.hex[:8]}", f"zz916x-{tid.hex[:8]}")
    return tid


async def _piece(c, tid):
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1, 'aa916x-atom', 'blog', 'approved') RETURNING request_id", tid)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log) VALUES ($1, $2, 1, 'Body.', 'approved', '[]'::jsonb, '[]'::jsonb) "
        "RETURNING piece_id", tid, req))


async def _version(c, tid):
    return str(await c.fetchval("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, "
                                "edit_source) VALUES ($1, $2, 'ai_generated') RETURNING id", tid, uuid.uuid4()))


async def _signal(c, tid, source_table, source_id, action="sentence_rewrite"):
    text = action.startswith("sentence_")
    ref = await wm.record_writer_feedback(
        c, writer="t2" if source_table == "tenant_tour_version" else "t9_blog", scope="tenant", tenant_id=tid,
        action=action, actor_type="admin", source_table=source_table, source_id=source_id,
        before_text="Old sentence." if text else None, after_text="New sentence." if text else None,
        reason_code="ai_phrasing")
    return ref.signal_id


async def _pref(c, sid):
    return await c.fetchval("SELECT exemplar_pref FROM shared.writing_signal WHERE signal_id = $1::uuid", sid)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(TTV_STANDIN_DDL)  # the CI replay has no tenant_tour_versions (001 is skipped); Dev has it
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _patch_piece(c, piece_id, sid, pref, role="admin"):
    return await cq.set_piece_signal_exemplar(piece_id, sid, cq.ExemplarBody(exemplar_pref=pref), _request(c),
                                              x_admin_secret=SECRET, x_admin_user_id=str(uuid.uuid4()),
                                              x_admin_role=role)


@pytest.mark.asyncio
async def test_piece_route_sets_and_clears_the_preference(conn):
    a = await _tenant(conn)
    piece = await _piece(conn, a)
    sid = await _signal(conn, a, "content_piece", piece)
    assert await _patch_piece(conn, piece, sid, "use") == {"signal_id": sid, "exemplar_pref": "use"}
    assert await _pref(conn, sid) == "use"
    assert await _patch_piece(conn, piece, sid.upper(), "never") == {"signal_id": sid, "exemplar_pref": "never"}
    assert (await _patch_piece(conn, piece, sid, None))["exemplar_pref"] is None
    assert await _pref(conn, sid) is None
    # the drawer history reads it back
    edits = await wm.with_signal_texts(conn, [{"details": {"signal_id": sid}}], a)
    assert edits[0]["details"]["exemplar_pref"] is None and edits[0]["details"]["reason_code"] == "ai_phrasing"


@pytest.mark.asyncio
async def test_version_route_sets_the_preference(conn):
    a = await _tenant(conn)
    version = await _version(conn, a)
    sid = await _signal(conn, a, "tenant_tour_version", version)
    out = await cq.set_version_signal_exemplar(version, sid, cq.ExemplarBody(exemplar_pref="never"), _request(conn),
                                               x_admin_secret=SECRET, x_admin_user_id=None, x_admin_role="admin")
    assert out == {"signal_id": sid, "exemplar_pref": "never"}
    # the piece route never reaches a version's signal (source_table is part of the filter)
    with pytest.raises(HTTPException) as e:
        await _patch_piece(conn, version, sid, "use")
    assert e.value.status_code == 404
    assert await _pref(conn, sid) == "never"


@pytest.mark.asyncio
async def test_a_signal_of_another_object_or_tenant_is_404_and_unchanged(conn):
    a, b = await _tenant(conn), await _tenant(conn)
    piece_a, piece_a2, piece_b = await _piece(conn, a), await _piece(conn, a), await _piece(conn, b)
    other_piece = await _signal(conn, a, "content_piece", piece_a2)
    # a row that names piece_a as its source but belongs to tenant b (cannot happen through the API; the filter
    # must still refuse it)
    foreign = await _signal(conn, b, "content_piece", piece_a)
    reason_only = await _signal(conn, a, "content_piece", piece_a, action="reject")
    for sid in (other_piece, foreign, reason_only):
        with pytest.raises(HTTPException) as e:
            await _patch_piece(conn, piece_a, sid, "use")
        assert e.value.status_code == 404
        assert await _pref(conn, sid) is None
    with pytest.raises(HTTPException) as e:
        await _patch_piece(conn, piece_b, other_piece, "use")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_unknown_ids_role_and_body(conn):
    a = await _tenant(conn)
    piece = await _piece(conn, a)
    sid = await _signal(conn, a, "content_piece", piece)
    for piece_id, signal_id in ((str(uuid.uuid4()), sid), ("not-a-uuid", sid), (piece, "nope"),
                                (piece, str(uuid.uuid4()))):
        with pytest.raises(HTTPException) as e:
            await _patch_piece(conn, piece_id, signal_id, "use")
        assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await _patch_piece(conn, piece, sid, "use", role="reviewer")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await cq.set_piece_signal_exemplar(piece, sid, cq.ExemplarBody(exemplar_pref="use"), _request(conn),
                                           x_admin_secret="wrong", x_admin_user_id=None, x_admin_role="admin")
    assert e.value.status_code in (401, 403)
    with pytest.raises(ValidationError):
        cq.ExemplarBody(exemplar_pref="maybe")
    with pytest.raises(ValidationError):
        cq.ExemplarBody(exemplar_pref="use", reason_code="x")
    assert await _pref(conn, sid) is None

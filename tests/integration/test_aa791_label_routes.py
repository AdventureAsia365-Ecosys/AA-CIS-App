"""AA-791 — the Content Quality label routes and the list label filter against real Postgres
(migration 231 applied by conftest): the upsert per labeller, the text hash, the latest-label
filter. Route functions are called directly with a request whose pool is a real asyncpg pool."""
import os
import sys
import uuid
from unittest.mock import MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.routers import admin_content_quality as cq  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


@pytest_asyncio.fixture
async def ctx():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    assert await p.fetchval("SELECT to_regclass('acp_shared.content_piece_label') IS NOT NULL"), \
        "migration 231 not applied by conftest"
    tid = uuid.uuid4()
    await p.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, 'L791', $2, 3)",
                    tid, f"aa791r-{tid.hex[:8]}")
    req = await p.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel) "
                           "VALUES ($1, 'a791', 'linkedin') RETURNING request_id", tid)
    pieces = {}
    for n, status in ((1, "approved"), (2, "held")):
        pieces[status] = str(await p.fetchval(
            "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
            "status) VALUES ($1, $2, $3, $4, $5) RETURNING piece_id", tid, req, n, f"Piece {n} text.", status))
    request = MagicMock()
    request.app.state.pool = p
    yield p, request, pieces
    await p.execute("DELETE FROM acp_shared.angle_gate_request WHERE tenant_id = $1", tid)  # cascades pieces + labels
    await p.execute("DELETE FROM shared.tenants WHERE tenant_id = $1", tid)
    await p.close()


def _hdr(user):
    return {"x_admin_secret": SECRET, "x_admin_user_id": user, "x_admin_role": "admin"}


@pytest.mark.asyncio
async def test_label_upsert_hash_and_agreement_rows(ctx):
    p, request, pieces = ctx
    pid = pieces["held"]
    await cq.set_piece_label(pid, cq.PieceLabelBody(label="light", defects={"fact": "light"}), request, **_hdr("u1"))
    out = await cq.set_piece_label(pid, cq.PieceLabelBody(label="heavy", minutes_to_fix=20, reason="two invented times"),
                                   request, **_hdr("u1"))
    assert [(x["labelled_by"], x["label"]) for x in out["labels"]] == [("admin:u1", "heavy")]  # overwritten
    assert out["mine"]["defects"] == {} and out["mine"]["minutes_to_fix"] == 20
    await cq.set_piece_label(pid, cq.PieceLabelBody(label="reject"), request, **_hdr("u2"))

    got = await cq.get_piece_label(pid, request, **_hdr("u1"))
    assert {x["labelled_by"]: x["label"] for x in got["labels"]} == {"admin:u1": "heavy", "admin:u2": "reject"}
    assert got["labels"][0]["labelled_by"] == "admin:u2"  # newest first
    assert got["mine"]["label"] == "heavy" and got["mine"]["text_changed_since_label"] is False

    stored = await p.fetchval("SELECT content_sha256 FROM acp_shared.content_piece_label "
                              "WHERE piece_id = $1::uuid AND labelled_by = 'admin:u1'", pid)
    assert len(stored) == 64
    # an admin sentence edit after the label shows up as a changed text
    await p.execute("UPDATE acp_shared.content_piece SET content_text = 'Edited.' WHERE piece_id = $1::uuid", pid)
    got = await cq.get_piece_label(pid, request, **_hdr("u1"))
    assert all(x["text_changed_since_label"] for x in got["labels"])


@pytest.mark.asyncio
async def test_latest_label_filter_sql(ctx):
    p, request, pieces = ctx
    await cq.set_piece_label(pieces["approved"], cq.PieceLabelBody(label="as_is"), request, **_hdr("u1"))
    ids = [pieces["approved"], pieces["held"]]

    async def matching(label):
        where, params = cq._label_filter("cp.piece_id = ANY($1::uuid[])", [ids], label)
        rows = await p.fetch(f"SELECT cp.piece_id::text FROM acp_shared.content_piece cp WHERE {where}", *params)
        return {r[0] for r in rows}

    assert await matching("as_is") == {pieces["approved"]}
    assert await matching("labelled") == {pieces["approved"]}
    assert await matching("unlabelled") == {pieces["held"]}
    assert await matching("reject") == set()

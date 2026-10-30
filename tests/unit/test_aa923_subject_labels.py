"""AA-923 WM-32 — the T9 piece label extended to T2 tour versions and A1 (S1) rewrites
(acp_shared.content_label, CIS 315): same criteria, same admin-only rule, one row per subject and labeller, the hash of
the labelled text, 503 before the migration. The T9 routes (content_piece_label) are untouched."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.core import staff_authz
from api.routers import admin_content_quality as cq

SECRET = "s3cret"
VERSION = str(uuid.uuid4())
CONTENT = str(uuid.uuid4())
TENANT = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SHA = "a" * 64

ROUTES = {
    "t2_version": (cq.get_tour_version_label, cq.set_tour_version_label, VERSION),
    "a1_content": (cq.get_a1_content_label, cq.set_a1_content_label, CONTENT),
}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


def _hdr(**over):
    base = {"x_admin_secret": SECRET, "x_admin_user_id": "u1", "x_admin_role": "admin"}
    base.update(over)
    return base


def _subject(status="ai_generated"):
    return {"tenant_id": TENANT, "status": status, "sha": SHA}


def _label_row(by="admin:u1", label="light", changed=False):
    return {"labelled_by": by, "label": label, "defects": json.dumps({"voice": "light"}), "minutes_to_fix": 4,
            "reason": "two stock phrases", "labelled_at": datetime(2026, 10, 30, tzinfo=timezone.utc),
            "text_changed_since_label": changed}


def _conn(subject=None, rows=(), table=True):
    conn = AsyncMock()
    conn.fetchval.return_value = table
    conn.fetchrow.return_value = subject
    conn.fetch.return_value = list(rows)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(ROUTES))
async def test_503_before_migration_315(kind):
    get, post, sid = ROUTES[kind]
    for call in (get(sid, _request(_conn(table=False)), **_hdr()),
                 post(sid, cq.PieceLabelBody(label="light"), _request(_conn(table=False)), **_hdr())):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 503 and "migration 315" in e.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(ROUTES))
@pytest.mark.parametrize("role", [None, "reviewer", "content", "viewer"])
async def test_admin_only(kind, role):
    get, post, sid = ROUTES[kind]
    conn = _conn(_subject())
    for call in (get(sid, _request(conn), **_hdr(x_admin_role=role)),
                 post(sid, cq.PieceLabelBody(label="light"), _request(conn), **_hdr(x_admin_role=role))):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 403
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(ROUTES))
@pytest.mark.parametrize("sid", [None, "not-a-uuid"])
async def test_unknown_or_malformed_subject_is_404(kind, sid):
    get, _post, good = ROUTES[kind]
    conn = _conn(None)
    with pytest.raises(HTTPException) as e:
        await get(sid or good, _request(conn), **_hdr())
    assert e.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"label": "great"}, {"label": "light", "defects": {"tone": "light"}},
                                  {"label": "light", "defects": {"fact": "terrible"}}])
async def test_body_validated(body):
    conn = _conn(_subject())
    with pytest.raises(HTTPException) as e:
        await cq.set_tour_version_label(VERSION, cq.PieceLabelBody(**body), _request(conn), **_hdr())
    assert e.value.status_code == 422
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_needs_an_admin_identity():
    with pytest.raises(HTTPException) as e:
        await cq.set_a1_content_label(CONTENT, cq.PieceLabelBody(label="as_is"), _request(_conn(_subject())),
                                      **_hdr(x_admin_user_id=None))
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_t2_version_without_finished_text_is_409(status):
    conn = _conn(_subject(status))
    with pytest.raises(HTTPException) as e:
        await cq.set_tour_version_label(VERSION, cq.PieceLabelBody(label="heavy"), _request(conn), **_hdr())
    assert e.value.status_code == 409
    conn.execute.assert_not_awaited()
    out = await cq.get_tour_version_label(VERSION, _request(conn), **_hdr())
    assert out["labellable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, status", [("t2_version", "approved"), ("t2_version", "needs_review"),
                                          ("a1_content", "draft"), ("a1_content", "rejected")])
async def test_upsert_one_row_per_subject_and_labeller(kind, status):
    _get, post, sid = ROUTES[kind]
    conn = _conn(_subject(status), rows=[_label_row("admin:u2", "heavy"), _label_row("admin:u1", "light")])
    body = cq.PieceLabelBody(label="light", defects={"voice": "light"}, minutes_to_fix=4,
                             reason="  two stock phrases  ")
    out = await post(sid, body, _request(conn), **_hdr())
    sql, *params = conn.execute.await_args.args
    assert "INSERT INTO acp_shared.content_label" in sql
    assert "ON CONFLICT (subject_type, subject_id, labelled_by) DO UPDATE" in sql
    assert params == [kind, sid, TENANT, "admin:u1", "light", json.dumps({"voice": "light"}), 4,
                      "two stock phrases", SHA]
    assert out["subject_type"] == kind and out["labellable"] is True
    assert out["mine"]["label"] == "light" and out["mine"]["defects"] == {"voice": "light"}
    assert [x["labelled_by"] for x in out["labels"]] == ["admin:u2", "admin:u1"]
    # the subject row is read with the subject's own table, and the labels are compared with its current hash
    subject_sql = conn.fetchrow.await_args.args[0]
    table = "gold_aa_internal.tenant_tour_versions" if kind == "t2_version" else "silver_aa_internal.generated_content"
    assert table in subject_sql and "sha256" in subject_sql
    assert conn.fetch.await_args.args[1:] == (kind, sid, SHA)


@pytest.mark.asyncio
async def test_get_returns_every_label_and_mine():
    conn = _conn(_subject("approved"), rows=[_label_row("admin:u2", "reject", changed=True)])
    out = await cq.get_a1_content_label(CONTENT, _request(conn), **_hdr())
    assert out["mine"] is None and out["labels"][0]["label"] == "reject"
    assert out["labels"][0]["text_changed_since_label"] is True
    assert out["choices"]["label"] == ["as_is", "light", "heavy", "reject"]
    conn.execute.assert_not_awaited()


def test_policy_and_bff_write_rule():
    for path in ("/admin/content-quality/tour-versions/{version_id}/label",
                 "/admin/content-quality/a1-content/{content_id}/label"):
        assert staff_authz.POLICY[("GET", path)] == staff_authz.TENANT_READ
        assert staff_authz.POLICY[("POST", path)] == staff_authz.QUALITY_WRITE
    from pathlib import Path
    rbac = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "rbac.ts").read_text()
    assert '{ prefix: "content-quality/a1-content/", roles: ["admin"] }' in rbac
    assert '{ prefix: "content-quality/tour-versions/", roles: ["admin"] }' in rbac

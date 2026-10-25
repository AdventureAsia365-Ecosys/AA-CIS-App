"""AA-990 — writing a T9 piece for a request whose atom a later atomize run replaced answers a typed 409
`atom_replaced` ("start a new post in Social Content"), not HTTP 500 "atom_id='…' not found (deleted, or not a live
platform/tenant atom)" (seen live 25/10/2026). The data is left as it is (carrying requests over is AA-990 proper)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.routers import v1_content_writing
from api.routers.v1_tours import get_tenant
from services.acp_content_writing import service
from services.acp_shared import atom_replaced
from services.acp_shared.atom_replaced import AtomReplaced

TENANT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


# ── service: the two "atom gone" spots raise the typed error ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_atom_text_raises_atom_replaced_for_a_dead_atom():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with pytest.raises(service.AtomReplacedError) as e:
        await service._fetch_atom_text(TENANT_ID, "atom_gone", _pool(conn))
    # still a ContentWritingError (every other caller keeps catching it); the ids stay in the internal message
    assert isinstance(e.value, service.ContentWritingError)
    assert "atom_gone" in str(e.value)
    assert e.value.status_code == 409 and e.value.code == "atom_replaced"
    assert e.value.detail.startswith("atom_replaced: ") and "atom_gone" not in e.value.detail
    assert "in Social Content to start a new post" in e.value.detail
    assert e.value.headers == {"X-Error-Code": "atom_replaced"}
    assert e.value.atom_id == "atom_gone" and e.value.segment_ids is None


def test_sentences_do_not_send_the_tenant_back_to_the_same_request():
    """Re-picking the angle (/reopen + /choose) keeps the same atom_id, so it would 409 again: the way out is a new
    post from Social Content (S251 review)."""
    for sentence in (atom_replaced.TENANT_SENTENCE, atom_replaced.ADMIN_SENTENCE):
        assert "Social Content" in sentence and "new post" in sentence
        assert "pick the angle again" not in sentence.lower()


@pytest.mark.asyncio
async def test_route_with_every_segment_rebuilt_away_raises_atom_replaced():
    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, None]
    with pytest.raises(service.AtomReplacedError) as e:
        await service._fetch_route_segments(TENANT_ID, "trip1", ["gone1", "gone2"], _pool(conn))
    assert e.value.segment_ids == ["gone1", "gone2"] and e.value.atom_id is None


@pytest.mark.asyncio
async def test_route_without_trip_stays_a_plain_content_writing_error():
    with pytest.raises(service.ContentWritingError) as e:
        await service._fetch_route_segments(TENANT_ID, None, ["seg1"], _pool(AsyncMock()))
    assert not isinstance(e.value, AtomReplaced)


@pytest.mark.asyncio
async def test_atom_is_live_uses_the_t8_t9_filter():
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    assert await atom_replaced.atom_is_live(conn, "atom_1", TENANT_ID) is True
    sql, *args = conn.fetchval.await_args.args
    assert "acp_contract.v_active_tour_atoms" in sql and "owner_scope IN ('platform', $2)" in sql
    assert "NOT deleted" in sql and "NOT is_empty_marker" in sql
    assert args == ["atom_1", str(TENANT_ID)]
    conn.fetchval.return_value = None
    assert await atom_replaced.atom_is_live(conn, "atom_1", TENANT_ID) is False
    assert await atom_replaced.atom_is_live(conn, None, TENANT_ID) is False


# ── tenant route: POST /v1/content-writing/requests/{id}/write ─────────────────────────────────────

def _client():
    app = FastAPI()
    app.include_router(v1_content_writing.router)
    app.dependency_overrides[get_tenant] = lambda: {"sub": str(TENANT_ID)}
    app.state.pool = _pool(AsyncMock())
    return TestClient(app, raise_server_exceptions=False)


def test_write_answers_409_atom_replaced_not_500():
    err = service.AtomReplacedError(
        "atom_id='atom_gone' not found (deleted, or not a live platform/tenant atom)").about(atom_id="atom_gone")
    with patch.object(v1_content_writing.service, "start_write", new=AsyncMock(side_effect=err)), \
         patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()) as audit, \
         patch("shared.jobs.registry.enqueue", new=AsyncMock()) as enqueue, \
         capture_logs() as logs:
        res = _client().post(f"/v1/content-writing/requests/{REQUEST_ID}/write", json={})
    assert res.status_code == 409
    assert res.headers["X-Error-Code"] == "atom_replaced"
    detail = res.json()["detail"]
    assert detail.startswith("atom_replaced: ") and "Social Content" in detail
    # the ids are logged (the list of affected requests for AA-990 proper), never returned
    line = [entry for entry in logs if entry["event"] == "aa990_atom_replaced"]
    assert len(line) == 1 and line[0]["log_level"] == "warning" and line[0]["path"] == "t9_write"
    assert line[0]["tenant_id"] == str(TENANT_ID) and line[0]["request_id"] == str(REQUEST_ID)
    assert line[0]["atom_id"] == "atom_gone"
    assert "atom_gone" not in detail and "not found" not in detail
    audit.assert_not_awaited()
    enqueue.assert_not_awaited()


def test_other_content_writing_errors_still_500():
    with patch.object(v1_content_writing.service, "start_write",
                      new=AsyncMock(side_effect=service.ContentWritingError("unexpected"))):
        res = _client().post(f"/v1/content-writing/requests/{REQUEST_ID}/write", json={})
    assert res.status_code == 500


def test_app_registers_a_handler_for_atom_replaced():
    """A service that raises the typed error outside the router's own try still answers 409, not the 500 handler."""
    from api.main import app
    assert AtomReplaced in app.exception_handlers

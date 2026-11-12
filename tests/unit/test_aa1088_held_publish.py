"""AA-1088 (M3, S278 system audit) — a held piece is not published without the account owner's explicit, audited
"publish anyway".

Before: POST /v1/publish-log/{piece_id}/publish accepted a 'held' piece like an approved one (AA-770), so a piece the
writer could not ground reached the agency's site with no word to the agency. Now the first publish answers a typed
409 (`content_needs_final_check`) with a content-style sentence; the owner may send {"publish_anyway": true}, which is
written to the audit log before the post goes out (fails closed). Mocks only: no network, no AWS.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers import v1_publish as mod
from api.services import tenant_account
from services.acp_s4_blog.cms.base import CMSPostResult
from services.acp_s4_blog.cms.wordpress import WordPressAdapter
from services.acp_shared.audit_log import TenantAuditAction
from tests.unit.test_aa458_publish import (PIECE_ID, TENANT, _claimed, _integ_row, _make_request, _piece_row,
                                           _sequential_pool)

OWNER = {**TENANT, "csr": "owner", "mid": "7b1c2d3e-0000-4000-8000-00000000a001"}
EDITOR = {**TENANT, "csr": "editor", "mid": "7b1c2d3e-0000-4000-8000-00000000a002"}
WP_CREDS = {"wp_url": "https://invented-blog.example", "username": "u", "app_password": "p"}


def _held():
    return {**_piece_row(), "status": "held"}


def _written():
    return {"publish_id": str(uuid.uuid4()), "status": "published", "external_id": "7",
            "external_url": "https://invented-blog.example/7",
            "published_at": datetime(2026, 11, 12, tzinfo=timezone.utc), "last_error": None}


def _body(resp) -> dict:
    return json.loads(resp.body)


def _audit_actions(conn) -> list[str]:
    return [c.args[3] for c in conn.execute.call_args_list if "INSERT INTO acp_shared.audit_log" in c.args[0]]


async def _publish(tenant, body=None, *, piece=None):
    pool, conn = _sequential_pool(piece or _held(), _integ_row(), _written())
    create = AsyncMock(return_value=CMSPostResult(post_id=7, post_url="https://invented-blog.example/7",
                                                  status="publish", cms_type="wordpress"))
    with patch.object(mod, "_get_secret", return_value=WP_CREDS), patch.object(mod, "claim", _claimed()), \
            patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, _make_request(pool), tenant, body)
    return result, conn, create


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [OWNER, EDITOR, TENANT], ids=["owner", "editor", "no-role-token"])
async def test_held_piece_without_publish_anyway_is_a_typed_409_and_nothing_goes_out(tenant):
    resp, conn, create = await _publish(tenant)
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == mod.PUBLISH_NEEDS_CHECK_CODE == "content_needs_final_check"
    body = _body(resp)
    assert body["code"] == "content_needs_final_check"
    assert isinstance(body["detail"], str) and body["detail"]  # every studio build shows a string detail
    assert body["can_publish_anyway"] is (tenant is not EDITOR)
    assert create.await_count == 0
    assert _audit_actions(conn) == []


@pytest.mark.asyncio
async def test_the_sentence_names_no_score_gate_or_reason():
    """Tenant-safe rule (AA-770 / AA-796): the agency reads a content-style sentence, never internal gate data."""
    for text in (mod.PUBLISH_NEEDS_CHECK_OWNER, mod.PUBLISH_NEEDS_CHECK_EDITOR):
        low = text.lower()
        for word in ("held", "score", "gate", "judge", "f9", "fact_check", "grounding", "review queue"):
            assert word not in low, (word, text)
    assert "Publish anyway" in mod.PUBLISH_NEEDS_CHECK_OWNER
    assert "account owner" in mod.PUBLISH_NEEDS_CHECK_EDITOR


@pytest.mark.asyncio
async def test_editor_asking_to_publish_anyway_is_still_refused():
    resp, conn, create = await _publish(EDITOR, mod.PublishBody(publish_anyway=True))
    assert resp.status_code == 409 and _body(resp)["can_publish_anyway"] is False
    assert create.await_count == 0 and _audit_actions(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [OWNER, TENANT], ids=["owner", "no-role-token"])
async def test_owner_publish_anyway_is_audited_before_the_post(tenant):
    order: list[str] = []
    pool, conn = _sequential_pool(_held(), _integ_row(), _written())

    async def _execute(sql, *args):
        if "INSERT INTO acp_shared.audit_log" in sql:
            order.append(args[2])
    conn.execute = AsyncMock(side_effect=_execute)

    async def _create(*_a, **_k):
        order.append("post")
        return CMSPostResult(post_id=7, post_url="https://invented-blog.example/7", status="publish",
                             cms_type="wordpress")
    with patch.object(mod, "_get_secret", return_value=WP_CREDS), patch.object(mod, "claim", _claimed()), \
            patch.object(WordPressAdapter, "create_post", AsyncMock(side_effect=_create)):
        result = await mod.publish(PIECE_ID, _make_request(pool), tenant, mod.PublishBody(publish_anyway=True))

    assert result["success"] is True
    assert order[:2] == [TenantAuditAction.CONTENT_PUBLISHED_ANYWAY, "post"]
    audit = next(c for c in conn.execute.call_args_list if c.args[3] == "content_piece.published_anyway")
    _, tenant_id, actor, _action, resource_type, resource_id, details = audit.args
    assert tenant_id == TENANT["sub"] and actor == f"tenant:{TENANT['sub']}"
    assert resource_type == "content_piece" and resource_id == str(PIECE_ID)
    details = json.loads(details)
    assert details["channel"] == "blog" and details["studio_role"] == "owner"
    assert details["membership_id"] == tenant.get("mid")
    assert details["revision_key"].startswith(f"{PIECE_ID}:blog:")


@pytest.mark.asyncio
async def test_publish_anyway_fails_closed_when_the_audit_row_cannot_be_written():
    pool, conn = _sequential_pool(_held(), _integ_row(), _written())
    conn.execute = AsyncMock(side_effect=RuntimeError("audit down"))
    create = AsyncMock()
    with patch.object(mod, "_get_secret", return_value=WP_CREDS), patch.object(mod, "claim", _claimed()) as claim, \
            patch.object(WordPressAdapter, "create_post", create):
        with pytest.raises(HTTPException) as exc:
            await mod.publish(PIECE_ID, _make_request(pool), OWNER, mod.PublishBody(publish_anyway=True))
    assert exc.value.status_code == 503 and exc.value.detail == mod.PUBLISH_ANYWAY_UNAVAILABLE
    assert create.await_count == 0 and claim.await_count == 0


@pytest.mark.asyncio
async def test_approved_piece_publishes_as_before_and_ignores_publish_anyway():
    for body in (None, mod.PublishBody(publish_anyway=True)):
        result, conn, create = await _publish(OWNER, body, piece=_piece_row())
        assert result["success"] is True and create.await_count == 1
        assert TenantAuditAction.CONTENT_PUBLISHED_ANYWAY not in _audit_actions(conn)


def test_the_choice_shows_in_the_tenant_activity_feed():
    assert tenant_account.TENANT_VISIBLE_ACTIONS["content_piece.published_anyway"][0] == "content"
    assert tenant_account.detail_text("content_piece.published_anyway", {"channel": "blog"})

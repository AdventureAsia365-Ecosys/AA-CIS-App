"""AA-1151 (S289): the two-person publish rule of tour content (``cis.content.publish_two_person``,
``api/services/content_publish_gate.py``) also guards the publish of supply content (the customer text of a Booking
hotel or activity, ``/admin/supply-content``).

- switch off (the Dev default): any review.act holder publishes, the version's author is not even read;
- switch on: no ``content.publish`` -> 403 ``two_person_required``; the author of a human version -> 403
  ``same_person``; another content lead publishes; an LLM draft has no human author.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import admin, admin_supply_content
from api.services import content_publish_gate
from services.supply_content import review

SECRET = "aa1151-unit-secret"
ENTITY = "5e000000-0000-4000-8000-000000001151"
VERSION = "5f000000-0000-4000-8000-000000001151"
AUTHOR = "a1151000-0000-4000-8000-00000000000a"
OTHER = "a1151000-0000-4000-8000-00000000000b"
URL = f"/admin/supply-content/{ENTITY}/versions/{VERSION}/publish"


class _Conn:
    def __init__(self, origin: str, created_by: str) -> None:
        self.row = {"origin": origin, "created_by": created_by}
        self.reads = 0

    async def fetchrow(self, sql, *args):
        assert "shared.supply_content_version" in sql and args == (VERSION, ENTITY)
        self.reads += 1
        return self.row


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn


def _client(monkeypatch, *, switch: bool, origin: str = "human", author: str = f"staff:{AUTHOR}"):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    conn = _Conn(origin, author)

    async def switch_on(_conn):
        return switch

    async def publish(_conn, entity_id, version_id, *, actor, note=None):
        return {"entity_id": entity_id, "version_id": version_id, "content_status": "published", "by": actor}

    monkeypatch.setattr(content_publish_gate, "switch_on", switch_on)
    monkeypatch.setattr(review, "publish", publish)
    app = FastAPI()
    app.include_router(admin_supply_content.router)
    app.state.pool = _Pool(conn)
    return TestClient(app), conn


def _post(c: TestClient, role: str, user: str):
    return c.post(URL, headers={"x-admin-secret": SECRET, "x-admin-role": role, "x-admin-user-id": user}, json={})


def test_switch_off_publishes_without_reading_the_author(monkeypatch):
    c, conn = _client(monkeypatch, switch=False)
    r = _post(c, "content", AUTHOR)
    assert r.status_code == 200, r.text
    assert conn.reads == 0


@pytest.mark.parametrize(
    "role, user, status, code",
    [
        ("content", OTHER, 403, "two_person_required"),
        ("content_lead", AUTHOR, 403, "same_person"),
        ("content_lead", OTHER, 200, None),
        ("admin", OTHER, 200, None),
    ],
)
def test_switch_on_applies_the_two_person_rule(monkeypatch, role, user, status, code):
    c, _ = _client(monkeypatch, switch=True)
    r = _post(c, role, user)
    assert r.status_code == status, r.text
    if code:
        assert r.headers.get("x-error-code") == code


def test_an_llm_draft_has_no_human_author(monkeypatch):
    c, _ = _client(monkeypatch, switch=True, origin="llm", author="system:supply_content_draft")
    assert _post(c, "content_lead", AUTHOR).status_code == 200


def test_editor_key_maps_the_supply_actor_to_the_gate_actor():
    assert review.editor_key(f"staff:{AUTHOR}") == f"admin:{AUTHOR}"
    assert review.editor_key("staff:content") is None  # a role name, not a person
    assert review.editor_key("system:job") is None
    assert review.editor_key(None) is None

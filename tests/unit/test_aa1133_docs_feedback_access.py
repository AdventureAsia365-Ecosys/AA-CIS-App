"""AA-1133 follow-up (S284 live check) — the docs manifest says whether this caller may answer "Was this page
helpful?" (``can_feedback``, api/routers/admin_docs.py), so the reader offers no vote the API would refuse.

The live check found the read-only Dev E2E session (role cookie "admin") offered the vote and then shown the raw code
``e2e_read_only``. Pins: ``can_feedback`` is staff_authz.denial_reason on POST /admin/docs/feedback for the same caller:
True for every staff role that may post, False for the viewer role and for any read-only Dev E2E identity (the A10b
admin and the A10d customer_success one), and the POST itself still refuses them. Every page here is invented.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import e2e_staff, staff_authz
from api.routers import admin, admin_docs
from api.services import docs_reader

SECRET = "the-real-secret"
M_KEY = "docs-reader/manifest.json"


def _manifest() -> dict:
    page = {"repo": "AA-Ecosys", "path": "docs/guides/kite.md", "key": "pages/AA-Ecosys/docs/guides/kite.md",
            "id": "GUIDE-91", "title": "Kite festival", "description": "Invented", "type": "how-to",
            "status": "active", "audience": ["staff"], "owner": "tech-lead", "last_reviewed": "2026-11-01",
            "generated": False, "access": "staff", "bytes": 10, "sha256": "0" * 64, "redactions": 0}
    return {"format": 1, "rule": "v1", "generated_at": "2026-11-18T03:00:00+00:00",
            "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master"}, "counts": {}, "pages": [page]}


class FakeS3:
    def get_object(self, Bucket, Key):
        assert Key == M_KEY
        body = json.dumps(_manifest()).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    docs_reader.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"), \
            patch.object(docs_reader, "_s3_client", lambda: FakeS3()):
        yield
    docs_reader.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _identity(**flags):
    identity = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=False, e2e_allowlisted_writes=False,
                               admin_id="22222222-2222-4222-8222-222222222222")
    for k, v in flags.items():
        setattr(identity, k, v)
    return patch.object(staff_authz, "request_staff_identity", lambda request: identity)


def _manifest_for(client, role: str) -> dict:
    r = client.get("/admin/docs/manifest", headers={"X-Admin-Secret": SECRET, "x-admin-role": role})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("role", ["admin", "content", "support", "customer_success", "finance", "ai_ops"])
def test_staff_roles_that_may_post_get_can_feedback(client, role):
    with _identity():
        assert _manifest_for(client, role)["can_feedback"] is True


def test_viewer_gets_no_vote(client):
    with _identity():
        assert _manifest_for(client, "viewer")["can_feedback"] is False


@pytest.mark.parametrize("allowlisted", [True, False])
def test_read_only_e2e_session_gets_no_vote_even_as_admin(client, allowlisted):
    # A10b (the E2E admin, with its write allow-list) and A10d (no allow-list): POST /admin/docs/feedback is on neither.
    with _identity(e2e_read_only=True, e2e_allowlisted_writes=allowlisted):
        assert _manifest_for(client, "admin")["can_feedback"] is False


def test_can_feedback_follows_the_route_policy():
    assert admin_docs.FEEDBACK_ROUTE == ("POST", "/admin/docs/feedback")
    assert staff_authz.POLICY[admin_docs.FEEDBACK_ROUTE] == staff_authz.DOCS_READ
    assert not e2e_staff.is_allowed_e2e_write_route(*admin_docs.FEEDBACK_ROUTE)
    e2e_admin = staff_authz.StaffPrincipal(kind="staff", role="admin", perms=staff_authz.ROLE_PERMS["admin"],
                                           auth="cognito", e2e_read_only=True, e2e_allowlisted_writes=True)
    assert admin_docs.can_feedback(e2e_admin) is False
    assert staff_authz.denial_reason(e2e_admin, *admin_docs.FEEDBACK_ROUTE) == e2e_staff.E2E_READ_ONLY
    staff = staff_authz.StaffPrincipal(kind="staff", role="support", perms=staff_authz.ROLE_PERMS["support"])
    assert admin_docs.can_feedback(staff) is True
    assert admin_docs.can_feedback(staff_authz.MACHINE) is False

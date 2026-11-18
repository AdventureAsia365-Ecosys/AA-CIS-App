"""AA-1132 — page history and the "needs review" flag in the docs manifest (api/services/docs_page_history.py, wired in
api/services/docs_reader.py and served by GET /admin/docs/manifest and /admin/docs/page).

Pins: ``history`` items pass through in their shape (newest first as the bundle sends them, at most HISTORY_ITEMS; a
redacted line without sha or PR stays; an item without a date or subject is dropped; 12-digit runs are masked);
``needs_review`` passes only as a boolean and its reason only on a flagged page; an older manifest reads as [] / null;
the manifest route serves the fields to a staff caller (the page route shares its entry shape). Every page, subject
and reason here is invented (this repo is public).
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin
from api.services import docs_page_history, docs_reader

SECRET = "the-real-secret"
SUPPORT = {"X-Admin-Secret": SECRET, "x-admin-role": "support"}
M_KEY = "docs-reader/manifest.json"


def _entry(path: str, **over) -> dict:
    e = {"repo": "AA-Ecosys", "path": path, "key": f"pages/AA-Ecosys/{path}", "id": "GUIDE-91",
         "title": "Kite festival", "description": "Invented", "type": "how-to", "status": "active",
         "audience": ["staff"], "owner": "tech-lead", "last_reviewed": "2026-11-01", "generated": False,
         "access": "staff", "bytes": 10, "sha256": "0" * 64, "redactions": 0}
    e.update(over)
    return e


HISTORY = [
    {"sha": "c" * 40, "date": "2026-11-17T09:00:00+07:00", "subject": "docs: kite   festival route for 123456789012",
     "pr": 875},
    {"date": "2026-11-12T10:00:00+07:00", "subject": "A change to 2 pages with restricted access"},
    {"sha": "not-a-sha", "date": "2026-11-10", "subject": "docs: first kite page", "pr": True},
    {"sha": "d" * 40, "date": "yesterday", "subject": "no usable date", "pr": 3},
    {"sha": "e" * 40, "date": "2026-11-09", "subject": None, "pr": 4},
    "not an item",
]


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        body = json.dumps(self.objects[Key]).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    docs_reader.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    docs_reader.reset_cache()


def test_history_items_keep_their_shape_and_order():
    e = docs_reader._clean_entry(_entry("docs/guides/kite.md", history=HISTORY))
    assert e["history"] == [
        {"sha": "c" * 40, "date": "2026-11-17T09:00:00+07:00",
         "subject": "docs: kite festival route for [redacted-account-id]", "pr": 875},
        {"sha": None, "date": "2026-11-12T10:00:00+07:00", "subject": "A change to 2 pages with restricted access",
         "pr": None},
        {"sha": None, "date": "2026-11-10", "subject": "docs: first kite page", "pr": None},
    ]


def test_history_is_capped_and_subjects_cut():
    long = [{"sha": f"{i:07x}", "date": "2026-11-01", "subject": "x" * 500, "pr": i + 1} for i in range(15)]
    out = docs_page_history.clean_history(long)
    assert len(out) == docs_page_history.HISTORY_ITEMS
    assert out[0]["pr"] == 1 and out[-1]["pr"] == docs_page_history.HISTORY_ITEMS
    assert all(len(i["subject"]) == docs_page_history.SUBJECT_CHARS for i in out)


@pytest.mark.parametrize("raw", [None, "history", 5, {"sha": "c" * 40}])
def test_history_of_the_wrong_shape_is_empty(raw):
    assert docs_page_history.clean_history(raw) == []


@pytest.mark.parametrize("over, expected", [
    ({"needs_review": True, "needs_review_reason": "  The kite   route changed 123456789012."},
     (True, "The kite route changed [redacted-account-id].")),
    ({"needs_review": True}, (True, None)),
    ({"needs_review": True, "needs_review_reason": "   "}, (True, None)),
    ({"needs_review": True, "needs_review_reason": 9}, (True, None)),
    ({"needs_review": False, "needs_review_reason": "stale reason"}, (False, None)),
    ({"needs_review": "yes", "needs_review_reason": "a reason"}, (None, None)),
    ({"needs_review": 1}, (None, None)),
    ({}, (None, None)),
])
def test_needs_review_shape(over, expected):
    e = docs_reader._clean_entry(_entry("docs/guides/kite.md", **over))
    assert (e["needs_review"], e["needs_review_reason"]) == expected


def test_reason_is_cut():
    flag, reason = docs_page_history.clean_review({"needs_review": True, "needs_review_reason": "y" * 900})
    assert flag is True and len(reason) == docs_page_history.REASON_CHARS


def test_older_manifest_reads_as_empty_history_and_null_flag():
    e = docs_reader._clean_entry(_entry("docs/guides/kite.md"))
    assert (e["history"], e["needs_review"], e["needs_review_reason"]) == ([], None, None)


def test_manifest_route_serves_the_fields():
    manifest = {"format": 1, "rule": "v1", "generated_at": "2026-11-18T03:00:00+00:00",
                "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master"}, "counts": {},
                "pages": [_entry("docs/guides/kite.md", history=HISTORY[:2], needs_review=True,
                                 needs_review_reason="The kite route changed.", unknown_field="dropped")]}
    fake = FakeS3({M_KEY: manifest})
    with patch.object(docs_reader, "_s3_client", lambda: fake):
        client = TestClient(main.app)
        listed = client.get("/admin/docs/manifest", headers=SUPPORT).json()["pages"][0]
    assert [h["pr"] for h in listed["history"]] == [875, None]
    assert (listed["needs_review"], listed["needs_review_reason"]) == (True, "The kite route changed.")
    assert "unknown_field" not in listed

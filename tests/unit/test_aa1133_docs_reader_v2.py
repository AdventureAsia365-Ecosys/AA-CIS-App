"""AA-1133 — docs reader v2 on the API side (api/services/docs_reader.py v2 fields, docs_search.py, docs_feedback.py,
api/routers/admin_docs.py search and feedback routes).

Pins: the v2 manifest fields are passed through only in their shape (an older manifest reads as nulls); the search
index is loaded only when its sha256 matches the manifest, opened read only, queried with terms the caller cannot turn
into FTS5 syntax, and owner-only sections never reach a caller without the owner flag; page feedback goes only to a
page the caller may read, the viewer cannot post it, the admin view is admin only and never names a hidden page.
Every page, place and note here is invented: this repo is public and ships no docs.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz, tenant_scope
from api.routers import admin
from api.services import docs_feedback, docs_reader, docs_search

SECRET = "the-real-secret"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
M_KEY = "docs-reader/manifest.json"
S_KEY = "docs-reader/search/index.sqlite"
BODY = "# Lantern market\n\nOpen at dusk.\n"
SEC_BODY = "# Private ledger\n\nOwner notes.\n"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _entry(path: str, access: str = "staff", **over) -> dict:
    e = {"repo": "AA-Ecosys", "path": path, "key": f"pages/AA-Ecosys/{path}", "id": "GUIDE-90", "title": "Lantern market",
         "description": "Invented", "type": "how-to", "status": "active", "audience": ["staff"], "owner": "tech-lead",
         "last_reviewed": "2026-11-01", "generated": False, "access": access, "bytes": 10, "sha256": "0" * 64,
         "redactions": 0}
    e.update(over)
    return e


def build_index(rows: list[tuple]) -> bytes:
    """An index in the root builder's format 1 (root tools/docs/docs_search_index.py)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE sections USING fts5(title, heading, body, folded, repo UNINDEXED, "
                 "path UNINDEXED, lang UNINDEXED, access UNINDEXED, slug UNINDEXED, "
                 "tokenize = 'unicode61 remove_diacritics 2')")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('format', '1'), ('source_sha', 'abc')")
    conn.executemany("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    data = conn.serialize()
    conn.close()
    return bytes(data)


INDEX_ROWS = [
    ("Lantern market", "Lantern market", "Open at dusk.", "", "AA-Ecosys", "docs/guides/lantern.md", "en", "staff", ""),
    ("Lantern market", "Opening hours", "The lantern stalls open at dusk; account 123456789012 noted.", "",
     "AA-Ecosys", "docs/guides/lantern.md", "en", "staff", "opening-hours"),
    ("Chợ đêm", "Đèn lồng", "Đèn lồng ở chợ.", "Chợ dêm Dèn lồng Dèn lồng ở chợ.", "AA-Ecosys",
     "docs/guides/lantern.vi.md", "vi", "staff", "đèn-lồng"),
    ("Private ledger", "Lantern ledger", "Lantern ledger for owners.", "", "AA-Ecosys", "docs/legal/ledger.md", "en",
     "owner", "lantern-ledger"),
    ("Gone page", "Lantern", "Lantern removed.", "", "AA-Ecosys", "docs/guides/gone.md", "en", "staff", "lantern"),
]


def _manifest(index: bytes | None = None, pages=None, **search_over) -> dict:
    pages = pages if pages is not None else [
        _entry("docs/guides/lantern.md", nav="guides", nav_order=2, nav_derived=False,
               known_issue="Step two   is old for 123456789012.",
               last_changed={"sha": "b" * 40, "date": "2026-11-17T07:32:35+07:00"},
               translations=[{"lang": "vi", "path": "docs/guides/lantern.vi.md",
                              "key": "pages/AA-Ecosys/docs/guides/lantern.vi.md", "title": "Chợ đêm"}]),
        _entry("docs/guides/lantern.vi.md", lang="vi", translation_of="docs/guides/lantern.md", title="Chợ đêm"),
        _entry("docs/legal/ledger.md", access="owner", id="SEC-90", title="Private ledger"),
    ]
    m = {"format": 1, "rule": "v1", "generated_at": "2026-11-18T03:00:00+00:00",
         "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master", "github": "Org-x/Repo-y"},
         "counts": {}, "pages": pages}
    if index is not None:
        m["search"] = {"key": "search/index.sqlite", "format": 1, "bytes": len(index), "sha256": _sha(index),
                       "sections": 5, **search_over}
    return m


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.calls: list[str] = []

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        value = self.objects[Key]
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    docs_reader.reset_cache()
    docs_search.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    docs_reader.reset_cache()
    docs_search.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _use(fake):
    return patch.object(docs_reader, "_s3_client", lambda: fake)


def _as_owner():
    identity = SimpleNamespace(auth="cognito", is_owner=True, e2e_read_only=False)
    return patch.object(staff_authz, "request_staff_identity", lambda request: identity)


def _objects(index: bytes | None = None, manifest=None) -> dict:
    out = {M_KEY: manifest if manifest is not None else _manifest(index)}
    if index is not None:
        out[S_KEY] = index
    return out


# ── manifest v2 fields ─────────────────────────────────────────────────────────────────────────────

def test_v2_fields_pass_through_cleaned(client):
    with _use(FakeS3(_objects())):
        body = client.get("/admin/docs/manifest", headers={"X-Admin-Secret": SECRET, "x-admin-role": "support"}).json()
    page = next(p for p in body["pages"] if p["path"] == "docs/guides/lantern.md")
    assert (page["nav"], page["nav_order"], page["nav_derived"]) == ("guides", 2, False)
    assert page["known_issue"] == "Step two is old for [redacted-account-id]."
    assert page["last_changed"] == {"sha": "b" * 40, "date": "2026-11-17T07:32:35+07:00"}
    assert body["source"]["github"] == "Org-x/Repo-y"
    assert body["search"] == {"available": False}
    assert {p["path"] for p in body["pages"]} == {"docs/guides/lantern.md", "docs/guides/lantern.vi.md"}


@pytest.mark.parametrize("over", [
    {"nav": "nowhere", "nav_order": 5000, "known_issue": "  ", "last_changed": {"sha": "zz", "date": "2026-11-17"}},
    {"nav": 3, "nav_order": True, "known_issue": 7, "last_changed": "b" * 40},
])
def test_v2_fields_of_the_wrong_shape_become_null(over):
    e = docs_reader._clean_entry(_entry("docs/x.md", **over))
    assert (e["nav"], e["nav_order"], e["nav_derived"], e["known_issue"], e["last_changed"]) == (None,) * 5


def test_older_manifest_reads_as_nulls():
    e = docs_reader._clean_entry(_entry("docs/x.md"))
    assert all(e[k] is None for k in ("nav", "nav_order", "nav_derived", "known_issue", "last_changed"))
    assert docs_reader._clean_source({"repo": "AA-Ecosys"})["github"] is None
    assert docs_reader._clean_source({"github": "not a/repo/name"})["github"] is None


@pytest.mark.parametrize("search", [
    {"key": "search/other.sqlite"}, {"format": 2}, {"sha256": "x"}, {"bytes": 0},
    {"bytes": docs_reader.MAX_SEARCH_BYTES + 1}, {"bytes": True},
])
def test_bad_search_reference_is_ignored(search):
    ref = {"key": "search/index.sqlite", "format": 1, "bytes": 10, "sha256": "c" * 64, **search}
    assert docs_reader._clean_search(ref) is None
    assert docs_reader._clean_search({"key": "search/index.sqlite", "format": 1, "bytes": 10, "sha256": "c" * 64})


# ── search ─────────────────────────────────────────────────────────────────────────────────────────

def test_terms_are_words_only():
    assert docs_search.terms('lantern" OR x* NEAR(a b) -- ; DROP') == ["lantern", "or", "x", "near", "a", "b", "drop"]
    assert docs_search.terms("a " * 20) == ["a"]
    assert len(docs_search.terms(" ".join(f"w{i}" for i in range(20)))) == docs_search.MAX_TERMS
    assert docs_search.fts_query(["chợ", "dem"]) == '"chợ"* "dem"*'


def test_snippet_segments_mark_hits_and_mask_ids():
    segs = docs_search.snippet_segments("a lantern at 123456789012")
    assert segs == [{"text": "a ", "hit": False}, {"text": "lantern", "hit": True},
                    {"text": " at [redacted-account-id]", "hit": False}]


def test_staff_search_hides_owner_and_unlisted_pages(client):
    index = build_index(INDEX_ROWS)
    with _use(FakeS3(_objects(index))):
        res = client.get("/admin/docs/search", params={"q": "lantern"},
                         headers={"X-Admin-Secret": SECRET, "x-admin-role": "finance"})
    assert res.status_code == 200 and res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert [r["path"] for r in body["results"]] == ["docs/guides/lantern.md"]
    hit = body["results"][0]
    assert hit["title"] == "Lantern market" and hit["nav"] == "guides"
    assert {s["slug"] for s in hit["sections"]} == {"", "opening-hours"}
    text = json.dumps(body)
    assert "123456789012" not in text and "ledger" not in text.lower() and "gone" not in text


def test_owner_search_sees_owner_pages(client):
    index = build_index(INDEX_ROWS)
    with _use(FakeS3(_objects(index))), _as_owner():
        body = client.get("/admin/docs/search", params={"q": "lantern ledger"}, headers=ADMIN).json()
    assert [r["path"] for r in body["results"]] == ["docs/legal/ledger.md"]


def test_vietnamese_without_marks_and_with_the_stroke(client):
    index = build_index(INDEX_ROWS)
    with _use(FakeS3(_objects(index))):
        for q in ("den long", "đèn lồng", "cho"):
            body = client.get("/admin/docs/search", params={"q": q}, headers=ADMIN).json()
            assert [r["path"] for r in body["results"]] == ["docs/guides/lantern.vi.md"], q
            assert body["results"][0]["lang"] == "vi"


def test_index_is_loaded_once_per_hash(client):
    index = build_index(INDEX_ROWS)
    fake = FakeS3(_objects(index))
    with _use(fake):
        for _ in range(3):
            assert client.get("/admin/docs/search", params={"q": "dusk"}, headers=ADMIN).status_code == 200
    assert fake.calls.count(S_KEY) == 1


def test_no_index_404_and_hash_mismatch_503(client):
    with _use(FakeS3(_objects())):
        assert client.get("/admin/docs/search", params={"q": "dusk"}, headers=ADMIN).status_code == 404
    docs_reader.reset_cache()
    index = build_index(INDEX_ROWS)
    objects = _objects(index)
    objects[S_KEY] = build_index(INDEX_ROWS[:1])          # a newer index under an older manifest
    with _use(FakeS3(objects)):
        res = client.get("/admin/docs/search", params={"q": "dusk"}, headers=ADMIN)
    assert res.status_code == 503 and "updated" in res.json()["detail"]


def test_a_file_that_is_not_an_index_503(client):
    junk = b"not a database at all" * 10
    with _use(FakeS3({M_KEY: _manifest(junk), S_KEY: junk})):
        assert client.get("/admin/docs/search", params={"q": "dusk"}, headers=ADMIN).status_code == 503


def test_index_is_read_only():
    conn = docs_search._open(build_index(INDEX_ROWS))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM meta")
    conn.close()


def test_query_validation_and_unknown_role(client):
    with _use(FakeS3(_objects(build_index(INDEX_ROWS)))):
        assert client.get("/admin/docs/search", params={"q": "a"}, headers=ADMIN).status_code == 422
        assert client.get("/admin/docs/search", params={"q": "lantern", "limit": 99}, headers=ADMIN).status_code == 422
        assert client.get("/admin/docs/search", params={"q": "lantern"},
                          headers={"X-Admin-Secret": SECRET, "x-admin-role": "tenant"}).status_code == 403
        body = client.get("/admin/docs/search", params={"q": "!!"}, headers=ADMIN).json()
    assert body["results"] == [] and body["terms"] == []


# ── feedback ───────────────────────────────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, fail=None):
        self.rows: dict[tuple, dict] = {}
        self.fail = fail

    async def fetchrow(self, sql, repo, path, lang, helpful, comment, actor, role):
        if self.fail:
            raise self.fail
        now = datetime.datetime(2026, 11, 18, tzinfo=datetime.timezone.utc)
        key = (repo, path, actor)
        inserted = key not in self.rows
        self.rows[key] = {"repo": repo, "path": path, "lang": lang, "helpful": helpful, "comment": comment,
                          "actor": actor, "role": role, "updated_at": now}
        return {"created_at": now, "updated_at": now, "inserted": inserted}

    async def fetch(self, sql, limit):
        if self.fail:
            raise self.fail
        if "row_number" in sql:
            return [r for r in self.rows.values() if r["comment"]]
        agg: dict[tuple, dict] = {}
        for r in self.rows.values():
            a = agg.setdefault((r["repo"], r["path"]), {"repo": r["repo"], "path": r["path"], "yes": 0, "no": 0,
                                                       "last_at": r["updated_at"]})
            a["yes" if r["helpful"] else "no"] += 1
        return list(agg.values())


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    async def execute(self, *a, **k):   # the staff audit middleware's write
        return None

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.fixture
def db():
    conn = FakeConn()
    old = getattr(main.app.state, "pool", None)
    main.app.state.pool = FakePool(conn)
    yield conn
    main.app.state.pool = old


class _Identity:
    """A verified Cognito identity on the request (as StaffBearerMiddleware leaves it), seen by the policy and by
    tenant_scope.staff_id_of (which imported the helper by name)."""

    def __init__(self, admin_id="11111111-1111-4111-8111-111111111111"):
        identity = SimpleNamespace(auth="cognito", is_owner=False, e2e_read_only=False, admin_id=admin_id)
        self._patches = [patch.object(staff_authz, "request_staff_identity", lambda request: identity),
                         patch.object(tenant_scope, "request_staff_identity", lambda request: identity)]

    def __enter__(self):
        for p in self._patches:
            p.start()

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()


_identity = _Identity


def test_policy():
    assert staff_authz.POLICY[("GET", "/admin/docs/search")] == staff_authz.DOCS_READ
    assert staff_authz.POLICY[("POST", "/admin/docs/feedback")] == staff_authz.DOCS_READ
    assert staff_authz.POLICY[("GET", "/admin/docs/feedback")] == staff_authz.OPS_READ


def test_feedback_upserts_one_answer_per_person(client, db):
    hdr = {"X-Admin-Secret": SECRET, "x-admin-role": "customer_success"}
    with _use(FakeS3(_objects())), _identity():
        r1 = client.post("/admin/docs/feedback", json={"path": "docs/guides/lantern.md", "helpful": True}, headers=hdr)
        r2 = client.post("/admin/docs/feedback", headers=hdr,
                         json={"path": "docs/guides/lantern.md", "lang": "vi", "helpful": False,
                               "comment": "  Step two names account 123456789012.  "})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["created"] is True and r2.json()["created"] is False
    row = db.rows[("AA-Ecosys", "docs/guides/lantern.md", "admin:11111111-1111-4111-8111-111111111111")]
    assert (row["helpful"], row["lang"], row["role"]) == (False, "vi", "customer_success")
    assert row["comment"] == "Step two names account [redacted-account-id]."


def test_feedback_refuses_unlisted_and_owner_pages(client, db):
    hdr = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
    with _use(FakeS3(_objects())), _identity():
        for path in ("docs/legal/ledger.md", "docs/guides/missing.md"):
            assert client.post("/admin/docs/feedback", json={"path": path, "helpful": False},
                               headers=hdr).status_code == 404
    assert db.rows == {}


def test_feedback_viewer_and_bad_bodies(client, db):
    with _use(FakeS3(_objects())), _identity():
        assert client.post("/admin/docs/feedback", json={"path": "docs/guides/lantern.md", "helpful": True},
                           headers={"X-Admin-Secret": SECRET, "x-admin-role": "viewer"}).status_code == 403
        for body in ({"path": "docs/guides/lantern.md"}, {"path": "docs/guides/lantern.md", "helpful": True, "x": 1},
                     {"path": "docs/guides/lantern.md", "helpful": True, "comment": "x" * 1001},
                     {"path": "docs/guides/lantern.md", "helpful": True, "lang": "eng"}):
            assert client.post("/admin/docs/feedback", json=body, headers=ADMIN).status_code == 422, body
    assert db.rows == {}


def test_feedback_503_before_the_table(client):
    old = getattr(main.app.state, "pool", None)
    main.app.state.pool = FakePool(FakeConn(fail=asyncpg.UndefinedTableError("no table")))
    try:
        with _use(FakeS3(_objects())), _identity():
            res = client.post("/admin/docs/feedback", json={"path": "docs/guides/lantern.md", "helpful": True},
                              headers=ADMIN)
            res2 = client.get("/admin/docs/feedback", headers=ADMIN)
    finally:
        main.app.state.pool = old
    assert res.status_code == res2.status_code == 503
    assert res.json()["detail"]["code"] == "unavailable"


def test_feedback_view_admin_only_and_hidden_pages_counted(client, db):
    now = datetime.datetime(2026, 11, 18, tzinfo=datetime.timezone.utc)
    db.rows = {
        ("AA-Ecosys", "docs/guides/lantern.md", "admin:a"): {"repo": "AA-Ecosys", "path": "docs/guides/lantern.md",
                                                              "lang": "en", "helpful": False, "comment": "Old step.",
                                                              "actor": "admin:a", "role": "support",
                                                              "updated_at": now},
        ("AA-Ecosys", "docs/legal/ledger.md", "admin:b"): {"repo": "AA-Ecosys", "path": "docs/legal/ledger.md",
                                                            "lang": "en", "helpful": False, "comment": "Owner note.",
                                                            "actor": "admin:b", "role": "admin", "updated_at": now},
    }
    with _use(FakeS3(_objects())):
        assert client.get("/admin/docs/feedback",
                          headers={"X-Admin-Secret": SECRET, "x-admin-role": "content_lead"}).status_code == 403
        body = client.get("/admin/docs/feedback", headers=ADMIN).json()
    assert [p["path"] for p in body["pages"]] == ["docs/guides/lantern.md"]
    assert body["hidden_pages"] == 1 and body["totals"] == {"yes": 0, "no": 1}
    assert body["pages"][0]["notes"][0]["comment"] == "Old step."
    assert "Owner note" not in json.dumps(body) and "admin:a" not in json.dumps(body)


def test_clean_comment():
    assert docs_feedback.clean_comment(None) is None
    assert docs_feedback.clean_comment("   ") is None
    assert len(docs_feedback.clean_comment("y" * 5000)) == docs_feedback.COMMENT_CHARS

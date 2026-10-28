"""AA-898 — GET /admin/docs/manifest and /admin/docs/page (api/routers/admin_docs.py, api/services/docs_reader.py).

Auth (admin only via staff_authz POLICY ops.read), owner-only pages (N-3: listed and served only with a verified
Cognito owner session; 404 for anyone else), manifest validation (bad entries dropped, keys never taken from the
caller), 404 / 503 mapping and the caches. The fixture pages are invented: this repo is public and ships no docs.
"""
from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import docs_reader

SECRET = "the-real-secret"
MANIFEST = "/admin/docs/manifest"
PAGE = "/admin/docs/page"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
M_KEY = "docs-reader/manifest.json"

STAFF_BODY = "# Runbook\n\nDo the thing.\n"
OWNER_BODY = "# Legal register\n\nOwner only.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _entry(path: str, access: str = "staff", body: str = STAFF_BODY, **over) -> dict:
    e = {"repo": "AA-Ecosys", "path": path, "key": f"pages/AA-Ecosys/{path}", "id": "OPS-01", "title": "Runbook",
         "description": "How to", "type": "how-to", "status": "active", "audience": ["engineer"], "owner": "nghiep",
         "last_reviewed": "2026-10-28", "generated": False, "access": access, "bytes": len(body),
         "sha256": _sha(body), "redactions": 0}
    e.update(over)
    return e


def _manifest(pages=None) -> dict:
    if pages is None:
        pages = [_entry("docs/ops/runbook.md"),
                 _entry("docs/legal/register.md", access="owner", body=OWNER_BODY, id="SEC-04", title="Register")]
    return {"format": 1, "rule": "v1", "generated_at": "2026-10-28T03:00:00+00:00",
            "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master"},
            "counts": {"published": len(pages)}, "pages": pages}


def _objects(manifest=None) -> dict:
    return {
        M_KEY: _manifest() if manifest is None else manifest,
        "docs-reader/pages/AA-Ecosys/docs/ops/runbook.md": STAFF_BODY.encode(),
        "docs-reader/pages/AA-Ecosys/docs/legal/register.md": OWNER_BODY.encode(),
    }


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
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    docs_reader.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    docs_reader.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _use(fake):
    return patch.object(docs_reader, "_s3_client", lambda: fake)


def _as_owner(owner: bool = True):
    """A verified Cognito identity on the request, as StaffBearerMiddleware leaves it."""
    identity = SimpleNamespace(auth="cognito", is_owner=owner, e2e_read_only=False)
    return patch.object(staff_authz, "request_staff_identity", lambda request: identity)


# ── auth ────────────────────────────────────────────────────────────────────────────────────────────

def test_policy_is_admin_only():
    for path in (MANIFEST, PAGE):
        assert staff_authz.POLICY[("GET", path)] == staff_authz.OPS_READ
    for role in ("viewer", "reviewer", "content"):
        assert not staff_authz.has_perm(role, staff_authz.OPS_READ), role


@pytest.mark.parametrize("path", [MANIFEST, f"{PAGE}?path=docs/ops/runbook.md"])
def test_missing_secret_and_non_admin_roles_403_without_storage_reads(client, path):
    fake = FakeS3(_objects())
    with _use(fake):
        assert client.get(path, headers={"x-admin-role": "admin"}).status_code == 403
        for role in ("viewer", "reviewer", "content", "tenant"):
            assert client.get(path, headers={"X-Admin-Secret": SECRET, "x-admin-role": role}).status_code == 403
        assert client.get(path, headers={"X-Admin-Secret": SECRET}).status_code == 403   # machine
    assert fake.calls == []


# ── manifest ────────────────────────────────────────────────────────────────────────────────────────

def test_admin_sees_staff_pages_only(client):
    with _use(FakeS3(_objects())):
        res = client.get(MANIFEST, headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert [p["path"] for p in body["pages"]] == ["docs/ops/runbook.md"]
    assert body["counts"] == {"shown": 1, "owner_only_shown": 0, "hidden_owner_only": 1}
    assert body["is_owner"] is False
    assert body["source"]["sha"] == "a" * 40
    assert "key" not in body["pages"][0] and "sha256" not in body["pages"][0]
    assert "redactions" not in body["pages"][0]
    assert res.headers["cache-control"] == "private, no-store"


def test_owner_sees_owner_pages(client):
    with _use(FakeS3(_objects())), _as_owner():
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert {p["path"] for p in body["pages"]} == {"docs/ops/runbook.md", "docs/legal/register.md"}
    assert body["is_owner"] is True and body["counts"]["owner_only_shown"] == 1


def test_cognito_non_owner_does_not_see_owner_pages(client):
    with _use(FakeS3(_objects())), _as_owner(False):
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert [p["path"] for p in body["pages"]] == ["docs/ops/runbook.md"]


def test_manifest_missing_404_and_storage_failure_503(client):
    with _use(FakeS3({})):
        assert client.get(MANIFEST, headers=ADMIN).status_code == 404
    docs_reader.reset_cache()
    with _use(FakeS3({M_KEY: RuntimeError("network down")})):
        res = client.get(MANIFEST, headers=ADMIN)
    assert res.status_code == 503 and "network" not in res.json()["detail"]


@pytest.mark.parametrize("payload", [b"not json", json.dumps([1]).encode(),
                                     json.dumps({"format": 2, "pages": []}).encode(),
                                     json.dumps({"format": 1, "pages": {}}).encode()])
def test_bad_manifest_503(client, payload):
    with _use(FakeS3({M_KEY: payload})):
        assert client.get(MANIFEST, headers=ADMIN).status_code == 503


def test_bad_entries_are_dropped_and_unknown_access_is_owner(client):
    pages = [
        _entry("docs/ok.md"),
        _entry("docs/../secret.md"),
        _entry("docs/a.md", key="pages/AA-Ecosys/docs/other.md"),       # key does not match repo/path
        _entry("docs/b.md", key="../../etc/passwd"),
        _entry("/abs.md"),
        _entry("docs/c.txt", key="pages/AA-Ecosys/docs/c.txt"),
        _entry("docs/ok.md"),                                            # duplicate
        _entry("docs/odd.md", access="everyone"),                        # unknown tier -> owner
        {"nonsense": True},
    ]
    with _use(FakeS3({M_KEY: _manifest(pages)})):
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert [p["path"] for p in body["pages"]] == ["docs/ok.md"]
    docs_reader.reset_cache()
    with _use(FakeS3({M_KEY: _manifest(pages)})), _as_owner():
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert {p["path"]: p["access"] for p in body["pages"]} == {"docs/ok.md": "staff", "docs/odd.md": "owner"}


def test_manifest_cached_and_refresh_throttled(client):
    fake = FakeS3(_objects())
    with _use(fake):
        client.get(MANIFEST, headers=ADMIN)
        client.get(MANIFEST, headers=ADMIN)
        client.get(f"{MANIFEST}?refresh=1", headers=ADMIN)   # cached copy younger than MIN_REFRESH_S
    assert fake.calls.count(M_KEY) == 1


def test_prefix_from_env(client, monkeypatch):
    monkeypatch.setenv("DOCS_READER_S3_PREFIX", "/other/")
    fake = FakeS3({"other/manifest.json": _manifest()})
    with _use(fake):
        assert client.get(MANIFEST, headers=ADMIN).status_code == 200
    assert fake.calls == ["other/manifest.json"]


# ── pages ───────────────────────────────────────────────────────────────────────────────────────────

def test_admin_reads_a_staff_page(client):
    fake = FakeS3(_objects())
    with _use(fake):
        res = client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["markdown"] == STAFF_BODY
    assert body["page"]["id"] == "OPS-01" and "key" not in body["page"]
    assert body["source"]["ref"] == "master"
    assert fake.calls == [M_KEY, "docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"]


def test_owner_page_is_404_for_a_non_owner_and_never_read(client):
    fake = FakeS3(_objects())
    with _use(fake):
        res = client.get(PAGE, params={"path": "docs/legal/register.md"}, headers=ADMIN)
    assert res.status_code == 404
    assert not any("register" in k for k in fake.calls)


def test_owner_reads_an_owner_page(client):
    with _use(FakeS3(_objects())), _as_owner():
        res = client.get(PAGE, params={"path": "docs/legal/register.md"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["markdown"] == OWNER_BODY


@pytest.mark.parametrize("path", ["docs/unlisted.md", "docs/../docs/ops/runbook.md",
                                  "pages/AA-Ecosys/docs/ops/runbook.md"])
def test_unlisted_paths_404_without_a_page_read(client, path):
    fake = FakeS3(_objects())
    with _use(fake):
        assert client.get(PAGE, params={"path": path}, headers=ADMIN).status_code == 404
    assert fake.calls == [M_KEY]


def test_other_repo_404(client):
    with _use(FakeS3(_objects())):
        res = client.get(PAGE, params={"path": "docs/ops/runbook.md", "repo": "AA-CIS-Infra"}, headers=ADMIN)
    assert res.status_code == 404


def test_listed_but_not_stored_404_and_failure_503(client):
    objects = _objects()
    del objects["docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"]
    with _use(FakeS3(objects)):
        assert client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN).status_code == 404
    objects["docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"] = _ClientError("AccessDenied")
    with _use(FakeS3(objects)):
        assert client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN).status_code == 503


def test_page_too_big_503(client):
    objects = _objects()
    objects["docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"] = b"x" * (docs_reader.MAX_PAGE_BYTES + 1)
    with _use(FakeS3(objects)):
        assert client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN).status_code == 503


def test_page_cached(client):
    fake = FakeS3(_objects())
    with _use(fake):
        for _ in range(3):
            assert client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN).status_code == 200
    assert fake.calls.count("docs-reader/pages/AA-Ecosys/docs/ops/runbook.md") == 1


def test_sha_mismatch_still_served(client):
    objects = _objects()
    objects["docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"] = b"# Newer body\n"
    with _use(FakeS3(objects)):
        res = client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["markdown"] == "# Newer body\n"


def test_account_id_left_in_a_page_is_masked_on_serve(client):
    # Built at run time: this repo is public and its resource-ID check refuses literal account IDs and ARNs.
    acct_a, acct_b = "1234" * 3, "9876" * 3
    leaked = f"bucket aa-x-{acct_a} and arn:aws:iam::{acct_b}:role/x\n"
    objects = _objects()
    objects["docs-reader/pages/AA-Ecosys/docs/ops/runbook.md"] = leaked.encode()
    with _use(FakeS3(objects)):
        res = client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN)
    assert res.status_code == 200
    md = res.json()["markdown"]
    assert acct_a not in md and acct_b not in md
    assert md.count(docs_reader.ACCOUNT_ID_MARKER) == 2


# ── docs.owner_read: owners, and the Dev-only E2E CIS admin (A10b, TEMPORARY) ───────────────────────────────────────

def _as_identity(**kw):
    identity = SimpleNamespace(**{"auth": "cognito", "is_owner": False, "e2e_read_only": False, **kw})
    return patch.object(staff_authz, "request_staff_identity", lambda request: identity)


def _principal(**kw):
    base = {"kind": "staff", "role": "admin", "perms": staff_authz.ROLE_PERMS["admin"], "auth": "cognito",
            "is_owner": False, "e2e_read_only": False}
    base.update(kw)
    return staff_authz.StaffPrincipal(**base)


def test_docs_owner_read_is_not_a_role_or_route_capability():
    assert staff_authz.DOCS_OWNER_READ == "docs.owner_read"
    assert staff_authz.DOCS_OWNER_READ not in staff_authz.CAPABILITIES
    assert staff_authz.DOCS_OWNER_READ not in staff_authz.POLICY.values()


@pytest.mark.parametrize("env,kw,expected", [
    ("dev", {"is_owner": True}, "owner"),
    ("prod", {"is_owner": True}, "owner"),                                  # a real owner is not env-gated
    ("dev", {"e2e_read_only": True}, "e2e_dev"),
    ("prod", {"e2e_read_only": True}, None),
    ("staging", {"e2e_read_only": True}, None),
    ("Dev", {"e2e_read_only": True}, None),                                  # exact match only
    (None, {"e2e_read_only": True}, None),                                   # unset refuses
    ("dev", {"e2e_read_only": True, "auth": None}, None),                    # no verified Cognito session
    ("dev", {"e2e_read_only": True, "role": "viewer",
             "perms": staff_authz.ROLE_PERMS["viewer"]}, None),               # not the admin scope
    ("dev", {}, None),                                                       # an ordinary admin
    ("dev", {"is_owner": True, "auth": None}, None),                         # owner flag without Cognito
])
def test_docs_owner_read_via(monkeypatch, env, kw, expected):
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)
    p = _principal(**kw)
    assert staff_authz.docs_owner_read_via(p) == expected
    assert staff_authz.can_read_owner_docs(p) is (expected is not None)
    assert staff_authz.docs_owner_read_via(staff_authz.MACHINE) is None


def test_e2e_admin_reads_owner_pages_on_dev_but_stays_not_owner(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    with _use(FakeS3(_objects())), _as_identity(e2e_read_only=True):
        body = client.get(MANIFEST, headers=ADMIN).json()
        page = client.get(PAGE, params={"path": "docs/legal/register.md"}, headers=ADMIN)
    assert {p["path"] for p in body["pages"]} == {"docs/ops/runbook.md", "docs/legal/register.md"}
    assert body["is_owner"] is False and body["owner_read"] is True
    assert page.status_code == 200 and page.json()["markdown"] == OWNER_BODY


@pytest.mark.parametrize("env", ["prod", "staging", ""])
def test_e2e_admin_gets_no_owner_pages_outside_dev(client, monkeypatch, env):
    monkeypatch.setenv("ENVIRONMENT", env)
    fake = FakeS3(_objects())
    with _use(fake), _as_identity(e2e_read_only=True):
        body = client.get(MANIFEST, headers=ADMIN).json()
        page = client.get(PAGE, params={"path": "docs/legal/register.md"}, headers=ADMIN)
    assert [p["path"] for p in body["pages"]] == ["docs/ops/runbook.md"]
    assert body["owner_read"] is False and body["counts"]["hidden_owner_only"] == 1
    assert page.status_code == 404
    assert not any("register" in k for k in fake.calls)


def test_e2e_admin_owner_read_grants_no_staff_write(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    p = _principal(e2e_read_only=True)
    assert staff_authz.can_read_owner_docs(p)
    for (method, route), cap in staff_authz.POLICY.items():
        if cap == staff_authz.STAFF_WRITE:
            assert staff_authz.denial_reason(p, method, route), (method, route)


def test_ordinary_admin_on_cognito_gets_no_owner_pages(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    with _use(FakeS3(_objects())), _as_identity():
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert body["owner_read"] is False and body["is_owner"] is False
    assert [p["path"] for p in body["pages"]] == ["docs/ops/runbook.md"]


# ── translations (lang / translation_of / translations, root build_docs_reader.py rule 8) ────────────────────────────

VI_BODY = "# Sổ tay\n\nLàm việc đó.\n"


def _tr(path: str, title: str = "Sổ tay", lang: str = "vi", **over) -> dict:
    t = {"lang": lang, "path": path, "key": f"pages/AA-Ecosys/{path}", "title": title}
    t.update(over)
    return t


def _with_translation(register_vi_access="owner"):
    return _manifest([
        _entry("docs/ops/runbook.md", lang="en", translation_of=None, translations=[_tr("docs/ops/runbook.vi.md")]),
        _entry("docs/ops/runbook.vi.md", body=VI_BODY, title="Sổ tay", lang="vi",
               translation_of="docs/ops/runbook.md", translations=[]),
        _entry("docs/legal/register.md", access="owner", body=OWNER_BODY, id="SEC-04", title="Register", lang="en",
               translation_of=None, translations=[_tr("docs/legal/register.vi.md", "Đăng ký")]),
        _entry("docs/legal/register.vi.md", access=register_vi_access, body=OWNER_BODY, id="SEC-04", title="Đăng ký",
               lang="vi", translation_of="docs/legal/register.md", translations=[]),
    ])


def _objects_vi(manifest):
    objs = _objects(manifest)
    objs["docs-reader/pages/AA-Ecosys/docs/ops/runbook.vi.md"] = VI_BODY.encode()
    objs["docs-reader/pages/AA-Ecosys/docs/legal/register.vi.md"] = OWNER_BODY.encode()
    return objs


def test_translation_fields_are_served_without_keys(client):
    with _use(FakeS3(_objects_vi(_with_translation()))):
        body = client.get(MANIFEST, headers=ADMIN).json()
        vi = client.get(PAGE, params={"path": "docs/ops/runbook.vi.md"}, headers=ADMIN)
        en = client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN)
    pages = {p["path"]: p for p in body["pages"]}
    assert set(pages) == {"docs/ops/runbook.md", "docs/ops/runbook.vi.md"}
    assert pages["docs/ops/runbook.md"]["lang"] == "en" and pages["docs/ops/runbook.md"]["translation_of"] is None
    assert pages["docs/ops/runbook.md"]["translations"] == [
        {"lang": "vi", "path": "docs/ops/runbook.vi.md", "title": "Sổ tay"}]
    assert (pages["docs/ops/runbook.vi.md"]["lang"], pages["docs/ops/runbook.vi.md"]["translation_of"],
            pages["docs/ops/runbook.vi.md"]["translations"]) == ("vi", "docs/ops/runbook.md", [])
    assert vi.status_code == 200 and vi.json()["markdown"] == VI_BODY
    assert vi.json()["page"]["translation_of"] == "docs/ops/runbook.md"
    assert en.json()["page"]["translations"][0]["path"] == "docs/ops/runbook.vi.md"
    assert "key" not in json.dumps(body["pages"])


def test_old_manifest_without_fields_reads_as_english(client):
    with _use(FakeS3(_objects())):
        body = client.get(MANIFEST, headers=ADMIN).json()
        page = client.get(PAGE, params={"path": "docs/ops/runbook.md"}, headers=ADMIN).json()
    p = body["pages"][0]
    assert (p["lang"], p["translation_of"], p["translations"]) == ("en", None, [])
    assert page["page"]["translations"] == []


@pytest.mark.parametrize("vi_access", ["owner", "staff"])
def test_translation_of_an_owner_page_is_owner_too(client, vi_access):
    fake = FakeS3(_objects_vi(_with_translation(vi_access)))
    with _use(fake):
        body = client.get(MANIFEST, headers=ADMIN).json()
        page = client.get(PAGE, params={"path": "docs/legal/register.vi.md"}, headers=ADMIN)
    assert not any(p["path"].startswith("docs/legal/") for p in body["pages"])
    assert body["counts"]["hidden_owner_only"] == 1   # the page and its translation count once
    assert page.status_code == 404 and not any("register" in k for k in fake.calls)
    with _use(FakeS3(_objects_vi(_with_translation(vi_access)))), _as_owner():
        body = client.get(MANIFEST, headers=ADMIN).json()
    pages = {p["path"]: p for p in body["pages"]}
    assert pages["docs/legal/register.vi.md"]["access"] == "owner"
    assert [t["path"] for t in pages["docs/legal/register.md"]["translations"]] == ["docs/legal/register.vi.md"]


def test_translation_unlisted_by_its_original_is_still_owner(client):
    pages = [_entry("docs/legal/register.md", access="owner", lang="en", translations=[]),
             _entry("docs/legal/register.vi.md", access="staff", lang="vi", translation_of="docs/legal/register.md")]
    with _use(FakeS3({M_KEY: _manifest(pages)})):
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert body["pages"] == [] and body["counts"]["hidden_owner_only"] == 1


@pytest.mark.parametrize("item", [
    _tr("docs/ops/missing.vi.md"),                                       # no such entry
    _tr("docs/ops/runbook.vi.md", key="pages/AA-Ecosys/docs/other.md"),   # key does not match the path
    _tr("../etc/passwd.md"),                                             # climbing
    _tr("docs/ops/runbook.vi.md", lang="VI"),                            # bad language code
    _tr("docs/ops/runbook.vi.md", lang="en"),                            # English is not a translation
    _tr("docs/ops/runbook.vi.md", lang="fr"),                            # the entry says vi
    "docs/ops/runbook.vi.md",                                            # not an object
])
def test_bad_or_one_sided_translation_items_are_dropped(client, item):
    pages = [_entry("docs/ops/runbook.md", lang="en", translations=[item]),
             _entry("docs/ops/runbook.vi.md", lang="vi", translation_of="docs/ops/runbook.md")]
    with _use(FakeS3({M_KEY: _manifest(pages)})):
        body = client.get(MANIFEST, headers=ADMIN).json()
    by = {p["path"]: p for p in body["pages"]}
    assert by["docs/ops/runbook.md"]["translations"] == []
    assert by["docs/ops/runbook.vi.md"]["translation_of"] is None   # one-sided: no toggle, not listed (lang vi)
    assert by["docs/ops/runbook.vi.md"]["lang"] == "vi"


def test_bad_lang_and_translation_of_fall_back_to_english_page(client):
    pages = [_entry("docs/a.md", lang="VI; drop", translation_of="../etc/passwd", translations="x"),
             _entry("docs/b.md", lang="en", translation_of="docs/a.md")]     # English pages translate nothing
    with _use(FakeS3({M_KEY: _manifest(pages)})):
        body = client.get(MANIFEST, headers=ADMIN).json()
    for p in body["pages"]:
        assert (p["lang"], p["translation_of"], p["translations"]) == ("en", None, [])

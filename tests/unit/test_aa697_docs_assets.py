"""AA-697 — GET /admin/docs/asset: SVG diagrams of the docs bundle (root build_docs_reader.py rule 9).

Admin only (same policy as the other docs routes), only assets the manifest lists for the caller, the safety re-check
on every read (an unsafe SVG is never served), the sandbox headers, and the manifest's `assets` list without keys.
The fixture SVGs are invented: this repo is public and ships no docs.
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
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
ASSET = "/admin/docs/asset"
MANIFEST = "/admin/docs/manifest"
M_KEY = "docs-reader/manifest.json"
SVG_PATH = "docs/architecture/diagrams/x.svg"
OWNER_SVG_PATH = "docs/architecture/diagrams/owner.svg"

# A tiny PNG data URI whose base64 carries a 12-digit run: digits inside an inline PNG are not text.
PNG_URI = "data:image/png;base64,iVBORw0KGgo000000000000AAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"


def _svg(inner: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10"><g id="a"><text>Diagram</text>'
            f'<image xlink:href="{PNG_URI}" width="1" height="1"/><use href="#a"/>{inner}</g></svg>\n')


SAFE = _svg()


def _asset(path: str = SVG_PATH, body: str = SAFE, **over) -> dict:
    a = {"repo": "AA-Ecosys", "path": path, "key": f"assets/AA-Ecosys/{path}", "type": "image/svg+xml",
         "bytes": len(body), "sha256": hashlib.sha256(body.encode()).hexdigest(), "access": "staff"}
    a.update(over)
    return a


def _manifest(assets=None) -> dict:
    page = {"repo": "AA-Ecosys", "path": "docs/architecture/diagrams/README.md",
            "key": "pages/AA-Ecosys/docs/architecture/diagrams/README.md", "id": "ARCH-01-diagrams",
            "title": "Diagrams", "type": "reference", "access": "staff", "sha256": None}
    m = {"format": 1, "rule": "v1", "generated_at": "2026-10-28T03:00:00+00:00",
         "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master"}, "pages": [page]}
    if assets is not None:
        m["assets"] = assets
    return m


def _objects(assets=None, files=None) -> dict:
    objs = {M_KEY: _manifest([_asset()] if assets is None else assets),
            f"docs-reader/assets/AA-Ecosys/{SVG_PATH}": SAFE.encode()}
    objs.update(files or {})
    return objs


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
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    docs_reader.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _use(fake):
    return patch.object(docs_reader, "_s3_client", lambda: fake)


def _as_owner():
    identity = SimpleNamespace(auth="cognito", is_owner=True, e2e_read_only=False)
    return patch.object(staff_authz, "request_staff_identity", lambda request: identity)


def test_policy_is_admin_only():
    assert staff_authz.POLICY[("GET", ASSET)] == staff_authz.OPS_READ


def test_non_admin_403_without_storage_reads(client):
    fake = FakeS3(_objects())
    with _use(fake):
        for role in ("viewer", "reviewer", "content", "tenant"):
            res = client.get(ASSET, params={"path": SVG_PATH}, headers={"X-Admin-Secret": SECRET, "x-admin-role": role})
            assert res.status_code == 403
    assert fake.calls == []


def test_admin_gets_the_svg_with_sandbox_headers(client):
    fake = FakeS3(_objects())
    with _use(fake):
        res = client.get(ASSET, params={"path": SVG_PATH}, headers=ADMIN)
    assert res.status_code == 200
    assert res.content == SAFE.encode()
    assert res.headers["content-type"].startswith("image/svg+xml")
    csp = res.headers["content-security-policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert fake.calls == [M_KEY, f"docs-reader/assets/AA-Ecosys/{SVG_PATH}"]


def test_asset_cached(client):
    fake = FakeS3(_objects())
    with _use(fake):
        for _ in range(3):
            assert client.get(ASSET, params={"path": SVG_PATH}, headers=ADMIN).status_code == 200
    assert fake.calls.count(f"docs-reader/assets/AA-Ecosys/{SVG_PATH}") == 1


@pytest.mark.parametrize("path", ["docs/other.svg", "docs/ops/runbook.md", "../x.svg", "/etc/x.svg",
                                  "docs/architecture/diagrams/README.md"])
def test_unlisted_paths_404_without_an_asset_read(client, path):
    fake = FakeS3(_objects())
    with _use(fake):
        assert client.get(ASSET, params={"path": path}, headers=ADMIN).status_code == 404
    assert fake.calls == [M_KEY]


@pytest.mark.parametrize("inner", [
    "<script>alert(1)</script>",
    "<foreignObject><div>x</div></foreignObject>",
    '<rect onload="x()"/>',
    '<a href="javascript:x()"><text>x</text></a>',
    '<image href="https://example.com/x.png"/>',
    "<style>g{fill:url(https://example.com/x)}</style>",
    '<animate attributeName="href" to="#a"/>',
    "<text>000000000000</text>",
    # The parsed re-check: namespace prefixes and XML character references the raw-text regexes cannot see.
    '<s:script xmlns:s="http://www.w3.org/2000/svg">x()</s:script>',
    '<h:script xmlns:h="http://www.w3.org/1999/xhtml">x()</h:script>',
    '<h:iframe xmlns:h="http://www.w3.org/1999/xhtml" src="https://example.com/"/>',
    '<s:set xmlns:s="http://www.w3.org/2000/svg" attributeName="href" to="#a"/>',
    '<use xmlns:x="http://www.w3.org/1999/xlink" x:href="https://example.com/a.svg#p"/>',
    '<rect style="fill:u&#114;l(https://example.com/x)"/>',
    "<style>&#64;import 'x.css';</style>",
    "<style>g{fill:u\\72l(https://example.com/x)}</style>",
    '<?xml-stylesheet type="text/css" href="#a"?>',
    '<g xml:base="https://example.com/"/>',
    "<text>00000&#48;000000</text>",
])
def test_unsafe_svg_is_never_served(client, inner):
    bad = _svg(inner)
    fake = FakeS3(_objects(assets=[_asset(body=bad)],
                           files={f"docs-reader/assets/AA-Ecosys/{SVG_PATH}": bad.encode()}))
    with _use(fake):
        res = client.get(ASSET, params={"path": SVG_PATH}, headers=ADMIN)
    assert res.status_code == 503
    assert bad.encode() not in res.content


def test_listed_but_not_stored_404(client):
    fake = FakeS3({M_KEY: _manifest([_asset()])})
    with _use(fake):
        assert client.get(ASSET, params={"path": SVG_PATH}, headers=ADMIN).status_code == 404


@pytest.mark.parametrize("bad", [
    {"key": "assets/AA-Ecosys/docs/other.svg"},          # key does not match the path
    {"key": "pages/AA-Ecosys/" + SVG_PATH},               # not under assets/
    {"type": "text/html"},
    {"path": "docs/x.html", "key": "assets/AA-Ecosys/docs/x.html"},
    {"path": "docs/../x.svg", "key": "assets/AA-Ecosys/docs/../x.svg"},
])
def test_bad_manifest_assets_are_dropped(client, bad):
    fake = FakeS3(_objects(assets=[_asset(**bad)]))
    with _use(fake):
        body = client.get(MANIFEST, headers=ADMIN).json()
        assert body["assets"] == []
        assert client.get(ASSET, params={"path": SVG_PATH}, headers=ADMIN).status_code == 404


def test_manifest_lists_assets_without_keys_and_owner_assets_for_owners_only(client):
    owner_body = _svg('<text>owner</text>')
    objs = _objects(assets=[_asset(), _asset(OWNER_SVG_PATH, owner_body, access="owner")],
                    files={f"docs-reader/assets/AA-Ecosys/{OWNER_SVG_PATH}": owner_body.encode()})
    with _use(FakeS3(objs)):
        body = client.get(MANIFEST, headers=ADMIN).json()
        assert body["assets"] == [{"repo": "AA-Ecosys", "path": SVG_PATH}]
        assert client.get(ASSET, params={"path": OWNER_SVG_PATH}, headers=ADMIN).status_code == 404
        with _as_owner():
            body = client.get(MANIFEST, headers=ADMIN).json()
            assert {a["path"] for a in body["assets"]} == {SVG_PATH, OWNER_SVG_PATH}
            assert client.get(ASSET, params={"path": OWNER_SVG_PATH}, headers=ADMIN).status_code == 200


def test_manifest_without_assets_reads_as_none(client):
    with _use(FakeS3({M_KEY: _manifest()})):
        body = client.get(MANIFEST, headers=ADMIN).json()
    assert body["assets"] == []


def test_svg_is_safe_accepts_fragments_and_inline_png_only():
    assert docs_reader.svg_is_safe(SAFE)
    assert not docs_reader.svg_is_safe("<html><body/></html>")
    assert not docs_reader.svg_is_safe(_svg('<image xlink:href="/home/someone/icon.png"/>'))
    assert not docs_reader.svg_is_safe('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg></svg>')

"""AA-1064 — GET /admin/docs/asset also serves PNG and WebP screenshots (root build_docs_reader.py rule 10).

Allowed types only (the manifest type must match the suffix), only assets the manifest lists for the caller (no caller
path, no traversal), the 300 KB cap and the signature re-checked on every read, the image headers, and the base64 JSON
form the admin BFF reads (the API Gateway in front is not binary safe). The fixture images are built in code.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin
from api.services import docs_reader

SECRET = "the-real-secret"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
ASSET = "/admin/docs/asset"
MANIFEST = "/admin/docs/manifest"
M_KEY = "docs-reader/manifest.json"
PNG_PATH = "docs/guides/images/list-page.png"
WEBP_PATH = "docs/guides/images/list-page.webp"
SVG_PATH = "docs/architecture/diagrams/x.svg"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_bytes(pad: int = 0) -> bytes:
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    if pad:
        data += _chunk(b"prVt", b"\0" * pad)
    return data + _chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0")) + _chunk(b"IEND", b"")


def webp_bytes() -> bytes:
    body = b"WEBP" + b"VP8L" + struct.pack("<I", 6) + b"\x2f\x00\x00\x00\x00\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


PNG = png_bytes()
WEBP = webp_bytes()
SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
       b'<g id="a"><text>Diagram</text></g></svg>\n')


def _asset(asset_path: str, body: bytes, media_type: str, **over) -> dict:
    path = asset_path
    a = {"repo": "AA-Ecosys", "path": path, "key": f"assets/AA-Ecosys/{path}", "type": media_type,
         "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "access": "staff"}
    a.update(over)
    return a


def _manifest(assets: list[dict]) -> dict:
    page = {"repo": "AA-Ecosys", "path": "docs/guides/run.md", "key": "pages/AA-Ecosys/docs/guides/run.md",
            "id": "GUIDE-01", "title": "Run", "type": "how-to", "access": "staff", "sha256": None}
    return {"format": 1, "rule": "v1", "generated_at": "2026-11-10T03:00:00+00:00",
            "source": {"repo": "AA-Ecosys", "sha": "b" * 40, "ref": "master"}, "pages": [page], "assets": assets}


def _objects(files: dict[str, tuple[bytes, str]], assets: list[dict] | None = None) -> dict:
    objs: dict = {M_KEY: _manifest(assets if assets is not None
                                   else [_asset(p, body, t) for p, (body, t) in files.items()])}
    for path, (body, _t) in files.items():
        objs[f"docs-reader/assets/AA-Ecosys/{path}"] = body
    return objs


DEFAULT = {PNG_PATH: (PNG, "image/png"), WEBP_PATH: (WEBP, "image/webp"), SVG_PATH: (SVG, "image/svg+xml")}


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


@pytest.mark.parametrize("path,body,media_type", [
    (PNG_PATH, PNG, "image/png"), (WEBP_PATH, WEBP, "image/webp"), (SVG_PATH, SVG, "image/svg+xml")])
def test_each_type_served_raw_with_image_headers(client, path, body, media_type):
    with _use(FakeS3(_objects(DEFAULT))):
        res = client.get(ASSET, params={"path": path}, headers=ADMIN)
    assert res.status_code == 200
    assert res.content == body
    assert res.headers["content-type"].startswith(media_type)
    assert res.headers["cache-control"] == "private, max-age=300"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in res.headers["content-security-policy"]


@pytest.mark.parametrize("path,body,media_type", [
    (PNG_PATH, PNG, "image/png"), (WEBP_PATH, WEBP, "image/webp"), (SVG_PATH, SVG, "image/svg+xml")])
def test_base64_form_for_the_bff(client, path, body, media_type):
    with _use(FakeS3(_objects(DEFAULT))):
        res = client.get(ASSET, params={"path": path, "encoding": "base64"}, headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.headers["cache-control"] == "private, no-store"
    got = res.json()
    assert got == {"repo": "AA-Ecosys", "path": path, "type": media_type, "bytes": len(body),
                   "sha256": hashlib.sha256(body).hexdigest(), "data": base64.b64encode(body).decode()}


def test_unknown_encoding_is_422(client):
    with _use(FakeS3(_objects(DEFAULT))):
        assert client.get(ASSET, params={"path": PNG_PATH, "encoding": "hex"}, headers=ADMIN).status_code == 422


@pytest.mark.parametrize("path", [
    "../docs/guides/images/list-page.png", "docs/guides/images/../images/list-page.png", "/etc/passwd.png",
    "docs/guides/images/other.png", "docs/guides/images/list-page.jpg", "docs/guides/images/list-page.PNG.md",
    "docs/guides/images/./list-page.png", "docs\\guides\\images\\list-page.png", "docs//guides/images/list-page.png"])
def test_traversal_and_unlisted_paths_404_without_an_asset_read(client, path):
    fake = FakeS3(_objects(DEFAULT))
    with _use(fake):
        assert client.get(ASSET, params={"path": path}, headers=ADMIN).status_code == 404
    assert fake.calls == [M_KEY]


@pytest.mark.parametrize("bad", [
    {"type": "image/svg+xml"},                                          # a .png listed as SVG
    {"type": "image/jpeg"},
    {"path": "docs/x.jpg", "key": "assets/AA-Ecosys/docs/x.jpg", "type": "image/jpeg"},
    {"path": "docs/x.gif", "key": "assets/AA-Ecosys/docs/x.gif", "type": "image/gif"},
    {"path": "docs/../x.png", "key": "assets/AA-Ecosys/docs/../x.png"},
    {"key": "assets/AA-Ecosys/docs/other.png"},
])
def test_manifest_entries_with_a_wrong_type_or_shape_are_dropped(client, bad):
    fake = FakeS3(_objects({}, assets=[_asset(PNG_PATH, PNG, "image/png", **bad)]))
    with _use(fake):
        assert client.get(MANIFEST, headers=ADMIN).json()["assets"] == []
        assert client.get(ASSET, params={"path": PNG_PATH}, headers=ADMIN).status_code == 404


def test_manifest_lists_screenshots(client):
    with _use(FakeS3(_objects(DEFAULT))):
        paths = {a["path"] for a in client.get(MANIFEST, headers=ADMIN).json()["assets"]}
    assert paths == {PNG_PATH, WEBP_PATH, SVG_PATH}


def test_oversize_screenshot_is_never_served(client):
    big = png_bytes(pad=docs_reader.MAX_RASTER_BYTES)
    assert len(big) > docs_reader.MAX_RASTER_BYTES
    with _use(FakeS3(_objects({PNG_PATH: (big, "image/png")}))):
        for encoding in ("raw", "base64"):
            res = client.get(ASSET, params={"path": PNG_PATH, "encoding": encoding}, headers=ADMIN)
            assert res.status_code == 503
            assert big not in res.content


def test_at_the_cap_is_served(client):
    body = png_bytes(pad=docs_reader.MAX_RASTER_BYTES - 200)
    assert len(body) <= docs_reader.MAX_RASTER_BYTES
    with _use(FakeS3(_objects({PNG_PATH: (body, "image/png")}))):
        assert client.get(ASSET, params={"path": PNG_PATH}, headers=ADMIN).status_code == 200


@pytest.mark.parametrize("path,body,media_type", [
    (PNG_PATH, SVG, "image/png"),          # an SVG stored under a .png name: never served as a PNG
    (PNG_PATH, WEBP, "image/png"),
    (WEBP_PATH, PNG, "image/webp"),
    (WEBP_PATH, b"RIFF\0\0\0\0WAVE", "image/webp"),
])
def test_wrong_signature_is_never_served(client, path, body, media_type):
    with _use(FakeS3(_objects({path: (body, media_type)}))):
        res = client.get(ASSET, params={"path": path}, headers=ADMIN)
    assert res.status_code == 503
    assert body not in res.content


def test_non_admin_403_without_storage_reads(client):
    fake = FakeS3(_objects(DEFAULT))
    with _use(fake):
        for role in ("viewer", "content", "tenant"):
            res = client.get(ASSET, params={"path": PNG_PATH, "encoding": "base64"},
                             headers={"X-Admin-Secret": SECRET, "x-admin-role": role})
            assert res.status_code == 403
    assert fake.calls == []


def test_raster_check_helpers():
    assert docs_reader.raster_is_valid(PNG, "image/png")
    assert docs_reader.raster_is_valid(WEBP, "image/webp")
    assert not docs_reader.raster_is_valid(PNG, "image/svg+xml")
    assert not docs_reader.raster_is_valid(b"", "image/png")
    assert docs_reader.asset_type("docs/a.PNG") == "image/png"
    assert docs_reader.asset_type("docs/a.webp") == "image/webp"
    assert docs_reader.asset_type("docs/a.svg") == "image/svg+xml"

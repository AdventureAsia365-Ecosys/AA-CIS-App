"""AA-1064 — the AI gateway Guide tab shows the images its page links (GET /admin/ai-gateway/guide/asset).

GET /admin/ai-gateway/guide lists in ``doc.assets`` the staff-tier bundle images the guide page (or its translation)
links; GET /admin/ai-gateway/guide/asset serves only those (spend.read like the tab, so finance and ai_ops, who cannot
open /admin/docs, see the page's pictures), never another image of the bundle, never an owner-tier one, nothing when
the guide itself is owner tier. Images are re-checked on read (docs_reader.load_asset). Also the link resolver
(docs_reader.linked_images), which must resolve like the builder and the admin reader. Invented page text and names
only; the fixture images are built in code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import ai_gateway_guide as guide
from api.services import docs_reader
from tests.unit.test_aa1064_docs_screenshots import PNG, SVG, WEBP

SECRET = "the-real-secret"
ASSET = "/admin/ai-gateway/guide/asset"
EN_PATH = "docs/architecture/ai-gateway.md"
VI_PATH = "docs/architecture/ai-gateway.vi.md"
SHOT = "docs/architecture/images/zz-route-panel.png"
VI_SHOT = "docs/architecture/images/zz-route-panel-vi.webp"
DIAGRAM = "docs/architecture/diagrams/zz-flow.svg"
OTHER = "docs/guides/images/zz-unlinked.png"         # published (another page links it) but not on the guide
OWNER_SHOT = "docs/architecture/images/zz-owner.png"  # linked by the guide, but owner tier in the manifest
EN = ("# Zz gateway\n\n![The route panel](images/zz-route-panel.png)\n\n![Flow](diagrams/zz-flow.svg \"flow\")\n\n"
      "![Owner only](images/zz-owner.png)\n\n![Remote](https://zz.example/x.png)\n\n"
      "`![in code](images/zz-in-code.png)`\n\n```md\n![fenced](images/zz-fenced.png)\n```\n")
VI = "# Cổng zz\n\n![Bảng route](./images/zz-route-panel-vi.webp)\n"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _page(path: str, body: str, **over) -> dict:
    e = {"repo": "AA-Ecosys", "path": path, "key": f"pages/AA-Ecosys/{path}", "id": guide.GUIDE_PAGE_ID,
         "title": "Zz gateway", "type": "explanation", "access": "staff", "bytes": len(body),
         "sha256": _sha(body.encode())}
    e.update(over)
    return e


def _asset(path: str, body: bytes, media_type: str, access: str = "staff") -> dict:
    return {"repo": "AA-Ecosys", "path": path, "key": f"assets/AA-Ecosys/{path}", "type": media_type,
            "bytes": len(body), "sha256": _sha(body), "access": access}


IMAGES = {SHOT: (PNG, "image/png", "staff"), VI_SHOT: (WEBP, "image/webp", "staff"),
          DIAGRAM: (SVG, "image/svg+xml", "staff"), OTHER: (PNG, "image/png", "staff"),
          OWNER_SHOT: (PNG, "image/png", "owner")}


def _bundle(*, access: str = "staff", images: dict | None = None) -> dict:
    images = IMAGES if images is None else images
    vi_item = {"lang": "vi", "path": VI_PATH, "key": f"pages/AA-Ecosys/{VI_PATH}", "title": "Cổng zz"}
    pages = [_page(EN_PATH, EN, access=access, translations=[vi_item]),
             _page(VI_PATH, VI, lang="vi", translation_of=EN_PATH, title="Cổng zz")]
    objects: dict = {
        "docs-reader/manifest.json": {
            "format": 1, "rule": "v1", "generated_at": "2026-11-10T01:00:00+00:00",
            "source": {"repo": "AA-Ecosys", "sha": "c" * 40, "ref": "master"}, "pages": pages,
            "assets": [_asset(p, b, t, a) for p, (b, t, a) in images.items()]},
        f"docs-reader/pages/AA-Ecosys/{EN_PATH}": EN.encode(),
        f"docs-reader/pages/AA-Ecosys/{VI_PATH}": VI.encode(),
    }
    for p, (b, _t, _a) in images.items():
        objects[f"docs-reader/assets/AA-Ecosys/{p}"] = b
    return objects


class _Missing(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            raise _Missing()
        v = self.objects[Key]
        body = v if isinstance(v, bytes) else json.dumps(v).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    docs_reader.reset_cache()
    with patch.object(docs_reader, "_bucket", lambda: "bucket"), patch.object(admin, "ADMIN_SECRET", SECRET):
        yield
    docs_reader.reset_cache()


def _use(objects):
    fake = FakeS3(objects)
    return fake, patch.object(docs_reader, "_s3_client", lambda: fake)


def _h(role: str = "finance") -> dict:
    return {"X-Admin-Secret": SECRET, "x-admin-role": role}


# ── the link resolver ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("src,expected", [
    ("images/a.png", "docs/architecture/images/a.png"),
    ("./images/a.png", "docs/architecture/images/a.png"),
    ("../guides/images/a.webp", "docs/guides/images/a.webp"),
    ("/docs/x/a.svg", "docs/x/a.svg"),
    ("images/a%20b.png?v=2#top", "docs/architecture/images/a b.png"),
    ("../../../a.png", None),
    ("https://zz.example/a.png", None), ("//zz.example/a.png", None), ("data:image/png;base64,AAAA", None),
    ("#frag", None), ("", None), ("images/%ff.png", None),
])
def test_resolve_image_like_the_reader(src, expected):
    assert docs_reader.resolve_image(src, EN_PATH) == expected


def test_linked_images_skip_code_and_remote():
    assert docs_reader.linked_images(EN, EN_PATH) == {SHOT, DIAGRAM, OWNER_SHOT}
    assert docs_reader.linked_images("![x](<images/a b.png> 'title')", EN_PATH) == {"docs/architecture/images/a b.png"}
    assert docs_reader.linked_images("no images here", EN_PATH) == set()


# ── doc.assets ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_doc_lists_only_the_staff_images_the_page_links():
    fake = FakeS3(_bundle())
    (doc, reason) = asyncio.run(guide.read_doc(reader=False, client_factory=lambda: fake))
    assert reason is None
    # the English page's PNG and SVG, the translation's WebP; never the owner one, never an unlinked one
    assert doc["assets"] == sorted([DIAGRAM, SHOT, VI_SHOT])
    assert not any(k.startswith("docs-reader/assets/") for k in fake.calls)       # listing reads no image


def test_doc_without_images_reads_no_extra_manifest_and_lists_none():
    objects = _bundle(images={})
    objects["docs-reader/pages/AA-Ecosys/" + EN_PATH] = b"# Zz gateway\n\nNo pictures.\n"
    objects["docs-reader/pages/AA-Ecosys/" + VI_PATH] = b"# Cong zz\n"
    fake = FakeS3(objects)
    doc, _ = asyncio.run(guide.read_doc(reader=False, client_factory=lambda: fake))
    assert doc["assets"] == []


def test_an_owner_guide_lists_nothing():
    fake = FakeS3(_bundle(access="owner"))
    doc, reason = asyncio.run(guide.read_doc(reader=False, client_factory=lambda: fake))
    assert doc is None and reason == "owner_tier"


# ── the route ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_policy_is_spend_read():
    assert staff_authz.POLICY[("GET", ASSET)] == staff_authz.SPEND_READ


@pytest.mark.parametrize("role", ["admin", "finance", "ai_ops"])
@pytest.mark.parametrize("path,body,media_type", [(SHOT, PNG, "image/png"), (VI_SHOT, WEBP, "image/webp"),
                                                  (DIAGRAM, SVG, "image/svg+xml")])
def test_a_linked_image_is_served_to_spend_readers(role, path, body, media_type):
    _fake, use = _use(_bundle())
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": path}, headers=_h(role))
    assert res.status_code == 200, res.text
    assert res.content == body and res.headers["content-type"].startswith(media_type)
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in res.headers["content-security-policy"]


def test_base64_form_for_the_bff():
    _fake, use = _use(_bundle())
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": SHOT, "encoding": "base64"}, headers=_h())
    assert res.status_code == 200 and res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert body == {"repo": "AA-Ecosys", "path": SHOT, "type": "image/png", "bytes": len(PNG), "sha256": _sha(PNG),
                    "data": base64.b64encode(PNG).decode("ascii")}


@pytest.mark.parametrize("path", [OTHER, OWNER_SHOT, "docs/architecture/images/zz-in-code.png",
                                  "docs/architecture/images/zz-fenced.png", "docs/architecture/../x.png",
                                  "docs-reader/manifest.json"])
def test_any_other_image_is_404_and_never_read(path):
    fake, use = _use(_bundle())
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": path}, headers=_h("admin"))
    assert res.status_code == 404
    assert not any(k.startswith("docs-reader/assets/") for k in fake.calls)


def test_owner_guide_serves_no_image():
    fake, use = _use(_bundle(access="owner"))
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": SHOT}, headers=_h("admin"))
    assert res.status_code == 404
    assert not any(k.startswith("docs-reader/assets/") for k in fake.calls)


def test_a_stored_image_that_fails_the_check_is_503():
    objects = _bundle()
    objects[f"docs-reader/assets/AA-Ecosys/{SHOT}"] = b"not a png at all"
    _fake, use = _use(objects)
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": SHOT}, headers=_h())
    assert res.status_code == 503


@pytest.mark.parametrize("role", ["viewer", "content"])
def test_other_roles_are_refused(role):
    fake, use = _use(_bundle())
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": SHOT}, headers=_h(role))
    assert res.status_code == 403 and fake.calls == []


def test_no_bundle_is_404():
    _fake, use = _use({})
    with use:
        res = TestClient(main.app).get(ASSET, params={"path": SHOT}, headers=_h())
    assert res.status_code == 404

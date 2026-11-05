"""AA-1042 (S270) — the AI gateway Guide tab: GET /admin/ai-gateway/guide (api/services/ai_gateway_guide.py) and the
docs reader's by-id read (docs_reader.load_staff_page_by_id).

The doc: one fixed catalog id, staff tier only (an owner entry is never read), translations, a missing bundle shown as
a note, links to /admin/docs only for a caller who may read it. The live tables: the payload shape, "first" from the
serving resolver (a refused first deployment is never named first), names only (no secret value, account ID, role or
URL). The policy: spend.read (admin, finance, ai_ops), not docs ops.read. Invented page text and names only.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin, admin_ai_gateway
from api.services import ai_gateway_guide as guide
from api.services import docs_reader

SECRET = "the-real-secret"
GUIDE = "/admin/ai-gateway/guide"
EN = "# Zz gateway\n\nSee [the routes](../generated/zz-routes.md) and stage `q7_write`.\n"
VI = "# Cổng zz\n\nXem [route](../generated/zz-routes.md).\n"
PLANTED = "zz-planted-secret-value-0000"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _entry(path: str, *, id_: str = guide.GUIDE_PAGE_ID, access: str = "staff", body: str = EN, **over) -> dict:
    e = {"repo": "AA-Ecosys", "path": path, "key": f"pages/AA-Ecosys/{path}", "id": id_, "title": "Zz gateway",
         "type": "explanation", "status": "draft", "audience": ["engineer"], "owner": "nghiep",
         "last_reviewed": "2026-11-05", "generated": False, "access": access, "bytes": len(body), "sha256": _sha(body)}
    e.update(over)
    return e


EN_PATH = "docs/architecture/ai-gateway.md"
VI_PATH = "docs/architecture/ai-gateway.vi.md"
VI_ITEM = {"lang": "vi", "path": VI_PATH, "key": f"pages/AA-Ecosys/{VI_PATH}", "title": "Cổng zz"}


def _bundle(*, access="staff", vi=True, vi_stored=True, extra=()) -> dict:
    pages = [_entry(EN_PATH, access=access, translations=[VI_ITEM] if vi else [])]
    if vi:
        pages.append(_entry(VI_PATH, body=VI, lang="vi", translation_of=EN_PATH, title="Cổng zz"))
    pages += list(extra)
    objects = {"docs-reader/manifest.json": {"format": 1, "rule": "v1", "generated_at": "2026-11-05T01:00:00+00:00",
                                             "source": {"repo": "AA-Ecosys", "sha": "b" * 40, "ref": "master"},
                                             "pages": pages},
               f"docs-reader/pages/AA-Ecosys/{EN_PATH}": EN.encode()}
    if vi and vi_stored:
        objects[f"docs-reader/pages/AA-Ecosys/{VI_PATH}"] = VI.encode()
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
    with patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    docs_reader.reset_cache()


def _doc(objects, reader=False):
    docs_reader.reset_cache()           # the manifest is cached per process for 5 minutes
    fake = FakeS3(objects)
    out = asyncio.run(guide.read_doc(reader=reader, client_factory=lambda: fake))
    return out, fake


# ── the doc ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_staff_page_with_its_translation():
    (doc, reason), _ = _doc(_bundle())
    assert reason is None and doc["missing"] is False
    assert (doc["id"], doc["path"], doc["lang"], doc["markdown"]) == (guide.GUIDE_PAGE_ID, EN_PATH, "en", EN)
    assert doc["translations"] == [{"lang": "vi", "path": VI_PATH, "title": "Cổng zz", "markdown": VI}]
    assert doc["source"] == {"repo": "AA-Ecosys", "sha": "b" * 40}
    assert doc["links"] == {"reader": False, "published": []}       # links stay text for a non-reader


def test_reader_gets_the_published_paths():
    other = _entry("docs/generated/zz-routes.md", id_="APP-06-routes", body="x")
    secret = _entry("docs/security/zz.md", id_="SEC-09", access="owner", body="y")
    (doc, _), _ = _doc(_bundle(extra=[other, secret]), reader=True)
    assert doc["links"]["reader"] is True
    assert doc["links"]["published"] == ["docs/architecture/ai-gateway.md", "docs/architecture/ai-gateway.vi.md",
                                         "docs/generated/zz-routes.md"]     # never an owner page


@pytest.mark.parametrize("access", ["owner", "partner", None])
def test_an_owner_entry_is_refused_and_never_read(access):
    (doc, reason), fake = _doc(_bundle(access=access))
    assert doc is None and reason == "owner_tier"
    assert fake.calls == ["docs-reader/manifest.json"]                 # no page body was read


def test_two_english_entries_with_the_id_are_refused():
    dup = _entry("docs/architecture/zz-copy.md")
    (doc, reason), _ = _doc(_bundle(vi=False, extra=[dup]))
    assert doc is None and reason == "owner_tier"


def test_missing_bundle_page_and_storage_failure_are_a_note():
    (doc, reason), _ = _doc({})
    assert doc == {"missing": True, "reason": "not_published"} and reason is None
    objects = _bundle()
    objects["docs-reader/manifest.json"]["pages"][0]["id"] = "ARCH-09-other"
    (doc, _), _ = _doc(objects)
    assert doc == {"missing": True, "reason": "not_published"}
    objects = _bundle()
    objects["docs-reader/manifest.json"] = b"not json"
    (doc, _), _ = _doc(objects)
    assert doc == {"missing": True, "reason": "unavailable"}


def test_a_translation_not_stored_yet_leaves_english():
    (doc, _), _ = _doc(_bundle(vi_stored=False))
    assert doc["markdown"] == EN and doc["translations"] == []


def test_the_id_is_fixed_and_no_caller_path_reaches_the_reader():
    import inspect
    assert guide.GUIDE_PAGE_ID == "ARCH-01-ai-gateway" and guide.GUIDE_REPO == "AA-Ecosys"
    assert list(inspect.signature(guide.read_doc).parameters) == ["reader", "client_factory"]


# ── the live tables ──────────────────────────────────────────────────────────────────────────────────────────────

STATE = {
    "version": 6, "order_mode": "model_first", "updated_at": "2026-11-05T06:00:00+00:00", "updated_by": "admin:zz",
    "max_attempts": 6,
    "stages": [
        {"stage": "q7_check", "role": "validate", "is_active": True, "product": "catalogue", "pii_class": "content",
         "chain": ["zeta-5-5", "zeta-4-1"], "options": [{"model_key": "zeta-5-5", "available": True}]},
        {"stage": "q7_dead", "role": "writer", "is_active": False, "product": "catalogue", "pii_class": "content",
         "chain": ["zeta-5-5"], "options": []},
    ],
    "provider_order": [{"model_key": "zeta-5-5", "deployments": [{"connection_key": "acc8"}]}],
    "presets": [{"key": "prefer_aws"}],
}


def test_a_refused_first_deployment_is_not_shown_as_first():
    resolved = {"q7_check": {"first": "zeta-4-1@acc8", "called_first": "zeta-5-5@acc8",
                             "skipped": [{"model": "zeta-5-5", "connection": "acc8",
                                          "reason": "refused (access_denied)"}]}}
    out = guide.routes_view(STATE, resolved)
    row = out["stages"][0]
    assert row["chain"][0] == "zeta-5-5"
    assert (row["first"], row["first_model"], row["first_connection"]) == ("zeta-4-1@acc8", "zeta-4-1", "acc8")
    assert row["skipped"] == [{"model": "zeta-5-5", "connection": "acc8", "reason": "refused (access_denied)"}]
    dead = out["stages"][1]
    assert dead["is_active"] is False and dead["first"] is None and dead["first_model"] is None
    assert "options" not in row and "presets" not in out
    assert (out["version"], out["order_mode"]) == (6, "model_first")


def test_serving_passes_the_refusal_evidence_and_the_live_config(monkeypatch):
    """The guide's "first" is llm_route_chains.resolve_stage with the refusal evidence (the #1307 preview
    resolver), under the live priorities and order mode."""
    from api.services import llm_route_chains
    from api.services import llm_route_control as rc
    from shared.llm_client import deployments, route_settings
    seen = []

    def fake_resolve(cfg, evidence=None):
        seen.append((cfg.stage, evidence, deployments._priority_override.get(), route_settings.order_mode_sync()))
        refused = evidence.refused("zeta-5-5", "acc8") if evidence else None
        return {"first": "zeta-4-1@acc8" if refused else "zeta-5-5@acc8"}
    monkeypatch.setattr(llm_route_chains, "resolve_stage", fake_resolve)

    class Evidence:
        def refused(self, model, conn):
            return "refused (access_denied)" if (model, conn) == ("zeta-5-5", "acc8") else None
    rows = [{"stage": "q7_check", "role": "validate", "provider": "bedrock", "model_id": "zeta-5-5",
             "fallback_model_ids": ["zeta-4-1"], "is_active": True},
            {"stage": "q7_dead", "role": "writer", "provider": "bedrock", "model_id": "zeta-5-5",
             "fallback_model_ids": [], "is_active": False}]
    world = SimpleNamespace(
        stages=rows, evidence=Evidence(),
        live={"order_mode": "aws_first", "priorities": {"zeta-5-5": {"acc8": 10}},
              "routes": {"q7_check": {"model_id": "zeta-5-5", "fallback_model_ids": ["zeta-4-1"]}}})
    out = asyncio.run(rc.serving(world))
    assert out == {"q7_check": {"first": "zeta-4-1@acc8"}}               # inactive stages are not resolved
    assert seen[0][0] == "q7_check" and isinstance(seen[0][1], Evidence)
    assert seen[0][2] == {"zeta-5-5": {"acc8": 10}} and seen[0][3] == "aws_first"


class _Conn(SimpleNamespace):
    def credential_source(self):
        return "env" if self.credential_env else "secret"


def _connections(monkeypatch):
    monkeypatch.setenv("ZZ_API_KEY", PLANTED)
    common = dict(enabled=True, access_state="ok", last_probe_at=None, failure_domain="zz", balance_source="none",
                  served_roles=("writer",), account_id="1" * 12, role_name="zz-role", base_url="https://zz.example",
                  external_id="zz-external")
    return {
        "zeta-direct": _Conn(connection_key="zeta-direct", kind="anthropic", label="Zeta API",
                             data_scope="vendor_direct", secret_name="zz-llm/dev/zeta-direct", credential_env=None,
                             **common),
        "acc8": _Conn(connection_key="acc8", kind="bedrock", label="acc8", data_scope="aws_only", secret_name=None,
                      credential_env="ZZ_API_KEY", **common),
    }


def test_register_names_only(monkeypatch):
    cells = [{"connection_key": "acc8", "enabled": True}, {"connection_key": "acc8", "enabled": False}]
    rows = guide.register_view(_connections(monkeypatch), cells)
    assert [r["connection_key"] for r in rows] == ["acc8", "zeta-direct"]
    assert set(rows[0]) == set(guide.REGISTER_FIELDS) | {"served_roles", "credential_source", "deployments",
                                                          "deployments_enabled"}
    assert (rows[0]["deployments"], rows[0]["deployments_enabled"], rows[0]["credential_env"]) == (2, 1, "ZZ_API_KEY")
    assert rows[1]["secret_name"] == "zz-llm/dev/zeta-direct"
    text = json.dumps(rows, default=str)
    for leak in (PLANTED, "1" * 12, "zz-role", "https://zz.example", "zz-external"):
        assert leak not in text


def test_caps_and_coverage_views():
    caps = guide.caps_view([{"connection_key": "zeta-direct", "kind": "anthropic", "label": "Zeta API",
                             "enabled": True, "account_id": "2" * 12, "monthly_budget_usd": 150.0,
                             "month_spent_usd": 1.5, "month_calls": 9, "used_pct": 1.0, "alert_pct": 80,
                             "hard_stop": True, "state": "ok"}])
    assert caps == [{k: v for k, v in caps[0].items()}] and "account_id" not in caps[0]
    assert caps[0]["monthly_budget_usd"] == 150.0
    report = {"ready": True, "ok": False, "checked_at": "2026-11-05T01:00:00+00:00", "stages_checked": 2,
              "uncovered": ["q7_dead_route"], "disabled": ["q7_off"], "proof_max_age_hours": 36,
              "stages": [{"stage": "q7_check", "role": "validate", "status": "live", "live_model": "zeta-4-1",
                          "live_connection": "acc8", "unproven": 0, "models": [{"model_key": "zeta-5-5"}]}]}
    cov = guide.coverage_view(report)
    assert cov["disabled"] == ["q7_off"] and cov["uncovered"] == ["q7_dead_route"]
    assert cov["stages"] == [{"stage": "q7_check", "role": "validate", "status": "live", "live_model": "zeta-4-1",
                              "live_connection": "acc8", "unproven": 0}]
    assert guide.coverage_view({"ready": False}) == {"ready": False, "stages": [], "uncovered": [], "disabled": []}


def test_matrix_uses_the_probe_rows():
    evidence = SimpleNamespace(deployments={
        ("acc8", "zeta-5-5"): {"model_key": "zeta-5-5", "connection_key": "acc8", "priority": 10, "enabled": True,
                               "access_state": "access_denied", "last_probe_at": "2026-11-05T05:00:00+00:00"},
        ("zeta-direct", "zeta-5-5"): {"model_key": "zeta-5-5", "connection_key": "zeta-direct", "priority": 5,
                                      "enabled": True, "access_state": "ok", "last_probe_at": None}})
    world = SimpleNamespace(evidence=evidence, deployments=[],
                            models=[SimpleNamespace(model_key="zeta-5-5", label="Zeta 5.5", family_key="zeta",
                                                    enabled=True)],
                            connections={"acc8": {"label": "acc8", "kind": "bedrock", "enabled": True}})
    m = guide.matrix_view(world)
    assert [c["connection_key"] for c in m["cells"]] == ["zeta-direct", "acc8"]
    assert m["cells"][1]["access_state"] == "access_denied" and m["cells"][1]["last_probe_at"]
    assert m["models"] == [{"model_key": "zeta-5-5", "label": "Zeta 5.5", "family": "zeta", "enabled": True}]
    assert m["connections"][1] == {"connection_key": "zeta-direct", "label": "zeta-direct", "kind": None,
                                   "enabled": None}


def test_read_live_shape_and_one_failing_source(monkeypatch):
    conns = _connections(monkeypatch)

    async def routes_and_matrix(pool):
        return {"routes": guide.routes_view(STATE, {}), "matrix": {"models": [], "connections": [], "cells": []}}

    async def register(pool):
        return conns

    async def caps(pool):
        raise RuntimeError("zz storage down " + PLANTED)

    async def coverage(pool):
        return guide.coverage_view({"ready": False})
    monkeypatch.setattr(guide, "_routes_and_matrix", routes_and_matrix)
    monkeypatch.setattr(guide, "_register", register)
    monkeypatch.setattr(guide, "_caps", caps)
    monkeypatch.setattr(guide, "_coverage", coverage)
    live = asyncio.run(guide.read_live(object()))
    assert set(live) == {"routes", "matrix", "register", "caps", "coverage", "errors"}
    assert live["caps"] is None and [e["source"] for e in live["errors"]] == ["caps"]
    assert len(live["register"]) == 2 and live["routes"]["version"] == 6
    assert PLANTED not in json.dumps(live, default=str)                   # the error text is the generic one


# ── the route ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_policy_is_spend_read_not_docs():
    assert staff_authz.POLICY[("GET", GUIDE)] == staff_authz.SPEND_READ
    for role in ("admin", "finance", "ai_ops"):
        assert staff_authz.has_perm(role, staff_authz.SPEND_READ), role
    assert not staff_authz.has_perm("finance", staff_authz.OPS_READ)


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_live(pool):
        return {"routes": None, "matrix": None, "register": None, "caps": None, "coverage": None, "errors": []}
    monkeypatch.setattr(guide, "read_live", fake_live)
    fake = FakeS3(_bundle())
    monkeypatch.setattr(docs_reader, "_s3_client", lambda: fake)
    real_read_doc = guide.read_doc

    async def spy(*, reader, client_factory=None):
        calls.append(reader)
        return await real_read_doc(reader=reader, client_factory=client_factory)
    monkeypatch.setattr(guide, "read_doc", spy)
    had = hasattr(main.app.state, "pool")
    old = getattr(main.app.state, "pool", None)
    main.app.state.pool = object()
    with patch.object(admin, "ADMIN_SECRET", SECRET):
        yield TestClient(main.app), calls
    if had:
        main.app.state.pool = old


@pytest.mark.parametrize("role,status,reader", [("admin", 200, True), ("finance", 200, False),
                                                ("ai_ops", 200, False), ("viewer", 403, None),
                                                ("content", 403, None)])
def test_roles_and_the_reader_flag(client, role, status, reader):
    http, calls = client
    res = http.get(GUIDE, headers={"X-Admin-Secret": SECRET, "x-admin-role": role})
    assert res.status_code == status, res.text
    if status == 200:
        body = res.json()
        assert set(body) == {"generated_at", "page_id", "doc", "doc_reason", "live"}
        assert body["doc"]["links"]["reader"] is reader and calls == [reader]
        assert body["doc"]["markdown"] == EN
        expected = staff_authz.denial_reason(
            staff_authz.principal_from_role(role, SimpleNamespace(auth="cognito", is_owner=False,
                                                                  e2e_read_only=False)),
            *admin_ai_gateway.DOCS_READER_ROUTE) is None
        assert expected is reader
    else:
        assert calls == []


def test_no_secret_header_is_refused(client):
    http, calls = client
    assert http.get(GUIDE, headers={"x-admin-role": "admin"}).status_code == 403
    assert calls == []

"""AA-977 (CRM-B5) — catalog.http.v1 ``/aa-content`` served by CIS: auth, contract bodies (contract 1.2), caching,
paging and the card's two rules.

R: draft content is never served (the reader reads only v_aa_content_published; a draft version is a plain 404).
E: after a newer version is published, the CRM still fetches the version it uses, with the same body and hash; a
retired item leaves the list but its versions still answer 200.
"""
import asyncio

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import m2m_auth
from api.routers.catalog_http import CatalogProblem, catalog_problem_handler, router
from services.aa_content import reader as aa_reader
from tests.unit.aa976_m2m import OTHER_CLIENT, install_m2m
from tests.unit.aa977_fakes import DRAFT_ONLY, RETIRED_EN, TPL_EN, VN_EN, VN_VI, FakeMainPool, published_row, seeded
from tests.unit.test_aa976_catalog_http_contract import DOC, _check

LIST = "/catalog/v1/aa-content"


def _version_path(content_id, version):
    return f"/catalog/v1/aa-content/{content_id}/versions/{version}"


@pytest.fixture(autouse=True)
def issuer(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    local, _ = install_m2m()
    yield local
    m2m_auth.set_m2m_verifier(None)


def _app(pool):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(CatalogProblem, catalog_problem_handler)
    app.state.pool = pool
    return app


@pytest.fixture
def view():
    return seeded()


@pytest.fixture
def pool(view):
    return FakeMainPool(view)


@pytest.fixture
def client(pool):
    return TestClient(_app(pool))


def _h(issuer, **claims):
    return {"Authorization": f"Bearer {issuer.token(**claims)}"}


# ── auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [LIST, _version_path(VN_EN, 1)])
def test_auth(client, issuer, path):
    op = "listAaContent" if path == LIST else "getAaContentVersion"
    r = _check(op, client.get(path))
    assert r.status_code == 401 and r.json()["error"] == "unauthorized" and "www-authenticate" in r.headers
    r = _check(op, client.get(path, headers=_h(issuer, client_id=OTHER_CLIENT)))
    assert r.status_code == 403
    r = _check(op, client.get(path, headers=_h(issuer, scope="booking.b2b/read")))
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


# ── list ──────────────────────────────────────────────────────────────────────

def test_list_latest_published_of_each_live_item(client, issuer, view):
    r = _check("listAaContent", client.get(LIST, headers=_h(issuer)))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["content_id"], i["version"]) for i in items] == [(str(VN_EN), 2), (str(VN_VI), 1), (str(TPL_EN), 1)]
    assert str(RETIRED_EN) not in {i["content_id"] for i in items}            # retired: hidden from the list
    assert str(DRAFT_ONLY) not in {i["content_id"] for i in items}            # draft only: never served (R)
    assert items[0]["group_key"] == items[1]["group_key"] == "prep-vietnam"   # EN / VI paired by group_key
    assert r.json()["next_cursor"] is None
    assert r.headers["cache-control"] == "private, max-age=60" and r.headers["vary"] == "Authorization"
    again = client.get(LIST, headers={**_h(issuer), "If-None-Match": r.headers["etag"]})
    _check("listAaContent", again)
    assert again.status_code == 304 and again.content == b""


@pytest.mark.parametrize("params, expect", [
    ({"kind": "starter_template"}, [TPL_EN]), ({"locale": "vi"}, [VN_VI]),
    ({"kind": "prep_pack", "locale": "en"}, [VN_EN]), ({"kind": "campaign_block"}, []),
])
def test_list_filters(client, issuer, params, expect):
    r = _check("listAaContent", client.get(LIST, params=params, headers=_h(issuer)))
    assert [i["content_id"] for i in r.json()["items"]] == [str(c) for c in expect]


def test_list_paging_and_cursor_bound_to_filters(client, issuer):
    first = _check("listAaContent", client.get(LIST, params={"limit": "2"}, headers=_h(issuer))).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = _check("listAaContent", client.get(LIST, params={"limit": "2", "cursor": first["next_cursor"]},
                                                headers=_h(issuer))).json()
    assert [i["content_id"] for i in second["items"]] == [str(TPL_EN)] and second["next_cursor"] is None
    for params in ({"limit": "3", "cursor": first["next_cursor"]},
                   {"limit": "2", "kind": "prep_pack", "cursor": first["next_cursor"]},
                   {"cursor": "v1.garbage.00000000"}, {"cursor": "nope"}, {"cursor": "x" * 201},
                   {"kind": "newsletter"}, {"locale": "fr"}, {"limit": "0"}, {"limit": "51"}, {"limit": "a"}):
        r = _check("listAaContent", client.get(LIST, params=params, headers=_h(issuer)))
        assert r.status_code == 400 and r.json()["error"] == "invalid", params


def test_cursor_round_trip_and_length():
    cursor = aa_reader.encode_cursor(str(VN_EN), "prep_pack", "en", 20)
    assert len(cursor) <= 200
    assert aa_reader.decode_cursor(cursor, "prep_pack", "en", 20) == VN_EN
    with pytest.raises(aa_reader.InvalidRequest):
        aa_reader.decode_cursor(cursor, "prep_pack", "vi", 20)


# ── one version ───────────────────────────────────────────────────────────────

def test_version_body_etag_and_304(client, issuer, view):
    r = _check("getAaContentVersion", client.get(_version_path(VN_VI, 1), headers=_h(issuer)))
    body = r.json()
    assert r.status_code == 200 and r.headers["etag"] == f'"{body["content_hash"]}"'
    assert body["applies_to"] == {"country_codes": ["VN"]} and body["locale"] == "vi"
    assert body["content_hash"] == aa_reader.hash_of({k: v for k, v in body.items() if k != "content_hash"})
    assert "status" not in body and "retired_at" not in body
    again = client.get(_version_path(VN_VI, 1), headers={**_h(issuer), "If-None-Match": r.headers["etag"]})
    _check("getAaContentVersion", again)
    assert again.status_code == 304


def test_rule_e_old_version_unchanged_after_newer_publish(client, issuer, view):
    v1 = client.get(_version_path(VN_EN, 1), headers=_h(issuer))
    view.add(VN_EN, version=3, title="Before you go: Vietnam (v3)")
    after = client.get(_version_path(VN_EN, 1), headers=_h(issuer))
    assert after.status_code == 200 and after.json() == v1.json() and after.headers["etag"] == v1.headers["etag"]
    listed = client.get(LIST, params={"locale": "en", "kind": "prep_pack"}, headers=_h(issuer)).json()["items"]
    assert [i["version"] for i in listed] == [3]
    assert _check("getAaContentVersion", client.get(_version_path(VN_EN, 3), headers=_h(issuer))).status_code == 200


def test_rule_r_draft_unknown_and_never_published_are_404(client, issuer):
    for path in (_version_path(DRAFT_ONLY, 1), _version_path(VN_EN, 9),
                 _version_path("20000000-0000-4000-8000-000000000000", 1)):
        r = _check("getAaContentVersion", client.get(path, headers=_h(issuer)))
        assert r.status_code == 404 and r.json()["error"] == "not_found" and "reason" not in r.json()


def test_retired_item_versions_still_answer(client, issuer):
    r = _check("getAaContentVersion", client.get(_version_path(RETIRED_EN, 1), headers=_h(issuer)))
    assert r.status_code == 200 and r.json()["title"] == "Old pack"


@pytest.mark.parametrize("content_id, version", [("not-a-uuid", 1), (VN_EN, 0), (VN_EN, "abc"), (VN_EN, "-1"),
                                                 (VN_EN, "99999999999"), (VN_EN, "01"), (VN_EN, "0001")])
def test_bad_path_values_are_400(client, issuer, content_id, version):
    r = _check("getAaContentVersion", client.get(_version_path(content_id, version), headers=_h(issuer)))
    assert r.status_code == 400 and r.json()["error"] == "invalid"


def test_database_unavailable_is_503(issuer):
    for pool in (None, FakeMainPool(fail=asyncio.TimeoutError())):
        c = TestClient(_app(pool))
        for op, path in (("listAaContent", LIST), ("getAaContentVersion", _version_path(VN_EN, 1))):
            r = _check(op, c.get(path, headers=_h(issuer)))
            assert r.status_code == 503 and r.headers["retry-after"] == "5"


class _MissingView:
    """A connection on a database where CIS 275 is not applied yet: every read fails like Postgres would."""

    def transaction(self, isolation=None, readonly=False):
        return seeded().transaction(readonly=readonly)

    async def fetch(self, sql, *args):
        raise asyncpg.UndefinedTableError('relation "gold_aa_internal.v_aa_content_published" does not exist')

    fetchrow = fetch


def test_code_deployed_before_the_migration_is_503_not_500(issuer):
    pool = FakeMainPool(_MissingView())
    c = TestClient(_app(pool), raise_server_exceptions=False)
    for op, path in (("listAaContent", LIST), ("getAaContentVersion", _version_path(VN_EN, 1))):
        r = _check(op, c.get(path, headers=_h(issuer)))
        assert r.status_code == 503 and r.json()["error"] == "unavailable" and r.headers["retry-after"] == "5"
    assert pool.acquired == pool.released == 2


def test_reads_are_read_only_on_the_main_pool(client, issuer, view, pool):
    client.get(LIST, headers=_h(issuer))
    client.get(_version_path(VN_EN, 1), headers=_h(issuer))
    assert view.readonly == [True, True] and pool.acquired == pool.released == 2


# ── the served body and the hash of a publish ────────────────────────────────

def test_published_row_hash_is_reproducible_from_the_view_row():
    row = published_row(VN_EN, applies_to={"tours": [{"source_system": "cis", "source_id": "k1"}],
                                           "country_codes": ["VN", "KH"]})
    body = aa_reader.served_body(row)
    assert aa_reader.hash_of(body) == row["content_hash"]
    assert body["published_at"].endswith("Z") and ".123456" in body["published_at"]


def test_openapi_doc_models_match_the_contract(client, issuer):
    from api.schemas import public_api as m

    pairs = {"AaContentPage": m.CatalogAaContentPage, "AaContentSummary": m.CatalogAaContentSummary,
             "AaContent": m.CatalogAaContent}
    for name, model in pairs.items():
        schema = DOC["components"]["schemas"][name]
        assert set(model.model_fields) == set(schema["properties"]), name
        required = {f for f, info in model.model_fields.items() if info.is_required()}
        assert required == set(schema.get("required", [])), name
    m.CatalogAaContentPage.model_validate(client.get(LIST, headers=_h(issuer)).json())
    m.CatalogAaContent.model_validate(client.get(_version_path(VN_EN, 1), headers=_h(issuer)).json())

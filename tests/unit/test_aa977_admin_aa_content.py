"""AA-977 (CRM-B5) — /admin/aa-content through the real app, and the field rules of services/aa_content/validate.py.

The store (services/aa_content/store.py) is replaced by recording stubs here: its SQL and the CIS 275 trigger are
exercised on real Postgres in tests/integration/test_aa977_aa_content_store.py. Covered here: who may call what
(content.write drafts, master.admin publish / retire / restore, the viewer reads only, the machine never writes), the
error mapping (422 field rule incl. visa_needs_official_link, 409 stale, 404), parameter checks and the actor label.
"""
from __future__ import annotations

import unicodedata
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from api.core.staff_authz import CONTENT_WRITE, MASTER_ADMIN, POLICY
from api.main import app
from api.routers import admin, admin_settings
from services.aa_content import store, validate
from services.aa_content.store import ContentConflict, ContentNotFound
from services.aa_content.validate import ContentInvalid
from tests.unit.aa977_fakes import FakeMainPool

SECRET = "aa977-test-admin-secret"
CID = "10000000-0000-4000-8000-0000000000aa"
BASE = "/admin/aa-content"
STAMP = "2026-10-24T08:00:00.123456Z"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    monkeypatch.setattr(app.state, "pool", FakeMainPool(conn=object()), raising=False)


@pytest.fixture
def calls(monkeypatch):
    """Every store call, recorded; each answers a small item dict."""
    seen: list[tuple[str, tuple, dict]] = []

    def stub(name):
        async def fn(conn, *args, **kwargs):
            seen.append((name, args, kwargs))
            return {"content_id": CID, "op": name}
        return fn

    for name in ("create_item", "save_draft", "discard_draft", "publish", "retire", "restore", "get_item",
                 "get_version", "list_items"):
        monkeypatch.setattr(store, name, stub(name))

    async def group(conn, group_key):
        seen.append(("get_group", (group_key,), {}))
        return [{"content_id": CID}] if group_key == "prep-vietnam" else []

    monkeypatch.setattr(store, "get_group", group)
    return seen


@pytest.fixture
def client(calls):
    return TestClient(app)


def _h(role="admin", user=None):
    h = {"X-Admin-Secret": SECRET}
    if role is not None:
        h["x-admin-role"] = role
    if user is not None:
        h["x-admin-user-id"] = user
    return h


CREATE = {"kind": "prep_pack", "locale": "en", "group_key": "prep-vietnam", "title": "Before you go: Vietnam"}
DRAFT = {"title": "t", "body_markdown": "b", "applies_to": {"country_codes": ["VN"]}, "expected_updated_at": STAMP}
EXPECTED = {"expected_updated_at": STAMP}

WRITES = [
    ("post", BASE, CREATE, "create_item"),
    ("put", f"{BASE}/{CID}/draft", DRAFT, "save_draft"),
    ("delete", f"{BASE}/{CID}/draft?expected_updated_at={STAMP}", None, "discard_draft"),
    ("post", f"{BASE}/{CID}/publish", EXPECTED, "publish"),
    ("post", f"{BASE}/{CID}/retire", EXPECTED, "retire"),
    ("post", f"{BASE}/{CID}/restore", EXPECTED, "restore"),
]
ADMIN_ONLY = {"publish", "retire", "restore"}


def _call(client, method, path, body, headers):
    if body is None:
        return getattr(client, method)(path, headers=headers)
    return client.request(method.upper(), path, json=body, headers=headers)


# ── policy ────────────────────────────────────────────────────────────────────

def test_policy_entries():
    assert POLICY[("POST", BASE)] == CONTENT_WRITE
    assert POLICY[("PUT", f"{BASE}/{{content_id}}/draft")] == CONTENT_WRITE
    assert POLICY[("DELETE", f"{BASE}/{{content_id}}/draft")] == CONTENT_WRITE
    for action in ("publish", "retire", "restore"):
        assert POLICY[("POST", f"{BASE}/{{content_id}}/{action}")] == MASTER_ADMIN


@pytest.mark.parametrize("role", ["content", "reviewer"])
@pytest.mark.parametrize("method, path, body, op", WRITES)
def test_content_team_drafts_but_never_publishes(client, calls, role, method, path, body, op):
    r = _call(client, method, path, body, _h(role))
    if op in ADMIN_ONLY:
        assert r.status_code == 403, r.text
        assert calls == []
    else:
        assert r.status_code in (200, 201), r.text
        assert calls[0][0] == op


@pytest.mark.parametrize("method, path, body, op", WRITES)
def test_admin_may_do_everything(client, calls, method, path, body, op):
    r = _call(client, method, path, body, _h("admin", user="0b6f1a7e-0000-4000-8000-000000000001"))
    assert r.status_code in (200, 201), r.text
    name, _args, kwargs = calls[0]
    assert name == op
    if "actor" in kwargs:
        assert kwargs["actor"] == "admin:0b6f1a7e-0000-4000-8000-000000000001"


@pytest.mark.parametrize("method, path, body, op", WRITES)
def test_viewer_and_machine_never_write(client, calls, method, path, body, op):
    for headers in (_h("viewer"), _h(None), {"x-admin-role": "admin"}):
        r = _call(client, method, path, body, headers)
        assert r.status_code in (401, 403), (headers, r.status_code)
    assert calls == []


@pytest.mark.parametrize("path", [BASE, f"{BASE}/{CID}", f"{BASE}/{CID}/versions/2", f"{BASE}/groups/prep-vietnam"])
def test_every_staff_role_reads(client, calls, path):
    for role in ("viewer", "content", "reviewer", "admin"):
        assert client.get(path, headers=_h(role)).status_code == 200, (role, path)


# ── parameters and error mapping ──────────────────────────────────────────────

def test_list_passes_filters_and_paging(client, calls):
    r = client.get(BASE, params={"kind": "prep_pack", "locale": "vi", "status": "draft", "country": "vn",
                                 "limit": "10", "offset": "20"}, headers=_h("viewer"))
    assert r.status_code == 200
    assert calls[0][2] == {"kind": "prep_pack", "locale": "vi", "status": "draft", "country": "VN", "limit": 10,
                           "offset": 20}


@pytest.mark.parametrize("params", [{"kind": "x"}, {"locale": "fr"}, {"status": "live"}, {"country": "FR"},
                                    {"limit": "0"}, {"limit": "101"}, {"offset": "-1"}])
def test_list_refuses_bad_parameters(client, calls, params):
    assert client.get(BASE, params=params, headers=_h()).status_code == 422
    assert calls == []


def test_unknown_group_is_404_and_bad_key_422(client):
    assert client.get(f"{BASE}/groups/prep-nowhere", headers=_h()).status_code == 404
    assert client.get(f"{BASE}/groups/Bad_Key", headers=_h()).status_code == 422


def test_bad_ids_and_stamps(client, calls):
    assert client.get(f"{BASE}/not-a-uuid", headers=_h()).status_code == 404
    assert client.post(f"{BASE}/not-a-uuid/publish", json=EXPECTED, headers=_h()).status_code == 404
    assert client.post(f"{BASE}/{CID}/publish", json={"expected_updated_at": "yesterday"},
                       headers=_h()).status_code == 422
    assert client.post(f"{BASE}/{CID}/publish", json={"expected_updated_at": "2026-10-24T08:00:00"},
                       headers=_h()).status_code == 422
    assert client.post(f"{BASE}/{CID}/publish", json={}, headers=_h()).status_code == 422
    assert client.put(f"{BASE}/{CID}/draft", json={**DRAFT, "extra": 1}, headers=_h()).status_code == 422
    assert calls == []


@pytest.mark.parametrize("missing", ["body_markdown", "applies_to", "title"])
def test_draft_put_is_a_full_replace(client, calls, missing):
    """A PUT without one of the draft's fields is 422, never a silent wipe of that field."""
    body = {k: v for k, v in DRAFT.items() if k != missing}
    r = client.put(f"{BASE}/{CID}/draft", json=body, headers=_h())
    assert r.status_code == 422 and calls == []


def test_draft_put_accepts_an_empty_body_and_scope(client, calls):
    r = client.put(f"{BASE}/{CID}/draft", json={**DRAFT, "body_markdown": "", "applies_to": {}}, headers=_h())
    assert r.status_code == 200
    assert calls[0][2]["body_markdown"] == "" and calls[0][2]["applies_to"] == {}


def test_expected_stamp_reaches_the_store_as_utc(client, calls):
    client.post(f"{BASE}/{CID}/publish", json={"expected_updated_at": "2026-10-24T15:00:00.123456+07:00"},
                headers=_h())
    stamp = calls[0][2]["expected_updated_at"]
    assert stamp.isoformat() == "2026-10-24T08:00:00.123456+00:00"
    assert calls[0][1] == (uuid.UUID(CID),)


@pytest.mark.parametrize("exc, status, code", [
    (ContentInvalid("visa_needs_official_link", "body_markdown", "add the link"), 422, "visa_needs_official_link"),
    (ContentConflict("stale", "Someone else saved this item"), 409, "stale"),
    (ContentConflict("no_draft", "Nothing to publish"), 409, "no_draft"),
    (ContentNotFound("item"), 404, "not_found"),
])
def test_store_refusals_are_mapped(client, monkeypatch, exc, status, code):
    async def refuse(conn, *args, **kwargs):
        raise exc

    monkeypatch.setattr(store, "publish", refuse)
    r = client.post(f"{BASE}/{CID}/publish", json=EXPECTED, headers=_h())
    assert r.status_code == status and r.json()["code"] == code
    if status == 422:
        assert r.json()["field"] == "body_markdown"


@pytest.mark.parametrize("what, detail", [
    ("item", "No AA content item has this id"),
    ("version", "No such version"),
    ("draft", "This item has no draft"),
    ("SELECT secret FROM x -- arn:aws:iam::<account>:role/Fake", "Not found"),
])
def test_not_found_sends_a_fixed_message_never_the_exception_text(client, monkeypatch, what, detail):
    """AA-981 (CodeQL py/stack-trace-exposure): the 404 body is a fixed message per kind, not str(exc)."""
    async def refuse(conn, *args, **kwargs):
        raise ContentNotFound(what)

    monkeypatch.setattr(store, "publish", refuse)
    r = client.post(f"{BASE}/{CID}/publish", json=EXPECTED, headers=_h())
    assert r.status_code == 404 and r.json() == {"detail": detail, "code": "not_found"}
    assert "arn:" not in r.text and "SELECT" not in r.text


def test_only_version_refusal_is_409(client, monkeypatch):
    async def refuse(conn, *args, **kwargs):
        raise ContentConflict("only_version", "This draft is the item's only version")

    monkeypatch.setattr(store, "discard_draft", refuse)
    r = client.delete(f"{BASE}/{CID}/draft?expected_updated_at={STAMP}", headers=_h())
    assert r.status_code == 409 and r.json()["code"] == "only_version"


@pytest.mark.parametrize("method, path, body, op", [
    ("get", BASE, None, "list_items"),
    ("get", f"{BASE}/groups/prep-vietnam", None, "get_group"),
    ("get", f"{BASE}/{CID}", None, "get_item"),
    *WRITES,
])
def test_code_deployed_before_the_migration_is_503_not_500(client, monkeypatch, method, path, body, op):
    async def missing(conn, *args, **kwargs):
        raise asyncpg.UndefinedTableError('relation "gold_aa_internal.aa_content_item" does not exist')

    monkeypatch.setattr(store, op, missing)
    r = _call(client, method, path, body, _h())
    assert r.status_code == 503 and r.json()["code"] == "unavailable" and r.headers["retry-after"] == "5"


# ── validate.py ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "## Entry and visa\n\nApply for the e-visa on the official portal: https://evisa.gov.vn",
    "Visa: [official site](https://www.mofa.go.jp/j_info/visit/visa/index.html).",
    "Thị thực: nộp tại https://evisa.gov.vn",
    "No entry rules in this pack.\n\nMoney: cash is king.",
    "Money: pay by Visa card or cash.",
    "Cards: Visa/Mastercard are taken in most hotels.",
    "Apply for an eVisa at https://evisa.gov.vn before you fly.",
])
def test_visa_rule_accepts(body):
    validate.publishable("prep_pack", "Before you go", body)


@pytest.mark.parametrize("body", [
    "## Visa\n\nYou need a visa on arrival.",
    "Money: cash.\n\nVISA requirements differ by passport, ask your agent.",
    "Thị thực: hỏi đại lý của bạn.",
    "Visa: see http://not-https.example.com",
    "Apply for an eVisa before you fly.",
    "apply for an evisa before you fly.",
    "E-Visas take three days.",
    unicodedata.normalize("NFD", "Xin thị thực trước khi đi."),
    "Pay by Visa card.\n\nYou also need a visa to enter.",
])
def test_visa_rule_refuses_a_visa_section_without_an_https_link(body):
    with pytest.raises(ContentInvalid) as exc:
        validate.publishable("prep_pack", "Before you go", body)
    assert exc.value.code == "visa_needs_official_link"


def test_visa_rule_is_for_prep_packs_only_and_publish_needs_text():
    validate.publishable("campaign_block", "Hi", "We do not handle your visa.")
    with pytest.raises(ContentInvalid) as exc:
        validate.publishable("starter_template", "Hi", "   ")
    assert exc.value.code == "body_required"
    with pytest.raises(ContentInvalid):
        validate.publishable("starter_template", "", "text")


def test_applies_to_is_normalized():
    out = validate.applies_to({"country_codes": ["vn", "VN", " kh "],
                               "tours": [{"source_system": "cis", "source_id": " k1 "},
                                         {"source_system": "cis", "source_id": "k1"}]})
    assert out == {"country_codes": ["VN", "KH"], "tours": [{"source_system": "cis", "source_id": "k1"}]}
    assert validate.applies_to(None) == {} and validate.applies_to({}) == {}


@pytest.mark.parametrize("value", [
    [], {"regions": []}, {"country_codes": "VN"}, {"country_codes": ["FR"]}, {"country_codes": [1]},
    {"tours": [{"source_system": "booking", "source_id": "k"}]}, {"tours": [{"source_system": "cis"}]},
    {"tours": [{"source_system": "cis", "source_id": "x" * 129}]}, {"tours": [{"source_system": "cis",
                                                                              "source_id": "k", "x": 1}]},
])
def test_applies_to_refuses(value):
    with pytest.raises(ContentInvalid):
        validate.applies_to(value)


@pytest.mark.parametrize("fn, value", [
    (validate.kind, "newsletter"), (validate.locale, "fr"), (validate.group_key, "-bad"),
    (validate.group_key, "a"), (validate.group_key, "UPPER"), (validate.title, "x" * 301), (validate.title, " "),
    (validate.body, "x" * 20001), (validate.body, 5),
])
def test_field_rules_refuse(fn, value):
    with pytest.raises(ContentInvalid):
        fn(value)

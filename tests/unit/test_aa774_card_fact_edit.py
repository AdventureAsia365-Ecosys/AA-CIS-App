"""AA-774 (S235) — admins correct place-card facts: re-kind, re-word or drop, audited.

Pure edit rules (services/acp_shared/place_cards.apply_fact_edits), the endpoint (role check, one
transaction, audit row with before/after, shape preserved) and the effect on the T9 writer seed
(atom_seed.writer_facts drops a fact re-kinded to visiting). Mocked asyncpg; route wiring through a
TestClient on the router alone."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers import admin_content_quality as cq
from services.acp_shared import atom_seed, place_cards
from services.acp_shared.audit_log import PlatformAuditAction

SECRET = "s3cret"
CARD_ID = str(uuid.uuid4())
TSAATAN = "Access requires a multi-hour horse or reindeer ride from the nearest road."
FACTS = [
    {"text": "The Tsaatan are reindeer herders of the northern taiga.", "kind": "what_it_is"},
    {"text": TSAATAN, "kind": "setting", "source": "atom:a1"},
    {"text": "Camps move several times a year.", "kind": "history"},
]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


# ── pure edit rules ───────────────────────────────────────────────────────────────────────────

def test_rekind_preserves_other_keys_and_order():
    facts, changes = place_cards.apply_fact_edits(json.dumps(FACTS), [{"index": 1, "kind": "visiting"}])
    assert facts[1] == {"text": TSAATAN, "kind": "visiting", "source": "atom:a1"}
    assert facts[0] == FACTS[0] and facts[2] == FACTS[2] and len(facts) == 3
    assert changes == [{"index": 1, "action": "edit", "before": FACTS[1], "after": facts[1]}]


def test_text_is_whitespace_normalised_and_drop_applies_after_indexing():
    facts, changes = place_cards.apply_fact_edits(
        FACTS, [{"index": 0, "drop": True}, {"index": 2, "text": "  Camps move   with the herd. "}])
    assert [f["text"] for f in facts] == [TSAATAN, "Camps move with the herd."]
    assert [c["action"] for c in changes] == ["drop", "edit"] and changes[0]["after"] is None


@pytest.mark.parametrize("edits, msg", [
    ([], "at least one"),
    ([{"index": 3, "kind": "history"}], "does not exist"),
    ([{"index": -1, "kind": "history"}], "does not exist"),
    ([{"index": 0, "kind": "trivia"}], "kind must be one of"),
    ([{"index": 0, "text": "   "}], "cannot be empty"),
    ([{"index": 0, "text": "x" * 601}], "longer than"),
    ([{"index": 0, "drop": True, "kind": "history"}], "cannot be combined"),
    ([{"index": 0}], "give a kind"),
    ([{"index": 0, "kind": "history"}, {"index": 0, "drop": True}], "edited twice"),
    ([{"index": 0, "kind": "what_it_is"}], "nothing to change"),
])
def test_invalid_edits_are_rejected(edits, msg):
    with pytest.raises(place_cards.FactEditError, match=msg):
        place_cards.apply_fact_edits(FACTS, edits)


def test_stale_expected_text_is_a_conflict():
    with pytest.raises(place_cards.FactEditConflict):
        place_cards.apply_fact_edits(FACTS, [{"index": 1, "kind": "visiting", "expected_text": "Camps move."}])
    facts, _ = place_cards.apply_fact_edits(FACTS, [{"index": 1, "kind": "visiting", "expected_text": TSAATAN}])
    assert facts[1]["kind"] == "visiting"


def test_rekinded_visiting_fact_leaves_the_writer_seed():
    assert TSAATAN in atom_seed.writer_facts({"facts": FACTS})
    facts, _ = place_cards.apply_fact_edits(FACTS, [{"index": 1, "kind": "visiting"}])
    seed = atom_seed.writer_facts({"facts": json.dumps(facts)})
    assert TSAATAN not in seed and len(seed) == 2
    # still on the card (admin view / sibling-gate count), only filtered at read time
    assert TSAATAN in atom_seed._card_facts({"facts": facts})


# ── endpoint ──────────────────────────────────────────────────────────────────────────────────

def _card_row(facts):
    return {"card_id": uuid.UUID(CARD_ID), "display_name": "Tsaatan camp", "locality": None, "region": "Khövsgöl",
            "country": "Mongolia", "naming_note": None, "facts": json.dumps(facts), "status": "verified",
            "cost_usd": 0.01, "verified_by": "u0", "verified_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 10, 14, tzinfo=timezone.utc), "atoms": 2}


def _conn(facts=FACTS):
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    stored = {"facts": json.dumps(facts)}

    async def execute(sql, *args):
        if "UPDATE acp_shared.place_card" in sql:
            stored["facts"] = args[1]
        return "OK"

    async def fetchrow(sql, *args):
        if "FOR UPDATE" in sql:
            return {"card_id": uuid.UUID(CARD_ID), "display_name": "Tsaatan camp", "country": "Mongolia",
                    "facts": stored["facts"]}
        return _card_row(json.loads(stored["facts"]))

    conn.execute.side_effect = execute
    conn.fetchrow.side_effect = fetchrow
    return conn


def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


def _audits(conn):
    return [c.args for c in conn.execute.await_args_list if "acp_shared.audit_log" in c.args[0]]


async def _edit(conn, edits, role="admin", reason="Access line is a visiting fact"):
    body = cq.PlaceCardFactsEdit(edits=[cq.FactEdit(**e) for e in edits], reason=reason)
    return await cq.edit_place_card_facts(CARD_ID, body, _request(conn), x_admin_secret=SECRET,
                                          x_admin_user_id="u1", x_admin_role=role)


@pytest.mark.asyncio
async def test_edit_updates_facts_and_writes_one_audit_row():
    conn = _conn()
    out = await _edit(conn, [{"index": 1, "kind": "visiting", "expected_text": TSAATAN}])
    upd = [c.args for c in conn.execute.await_args_list if "UPDATE acp_shared.place_card" in c.args[0]]
    assert len(upd) == 1 and upd[0][1] == CARD_ID
    assert json.loads(upd[0][2])[1] == {"text": TSAATAN, "kind": "visiting", "source": "atom:a1"}
    (audit,) = _audits(conn)
    assert audit[1:6] == (None, "admin:u1", PlatformAuditAction.PLACE_CARD_FACTS_EDITED, "place_card", CARD_ID)
    details = json.loads(audit[6])
    assert details["reason"] == "Access line is a visiting fact"
    assert details["facts_before"] == details["facts_after"] == 3
    assert details["changes"] == [{"index": 1, "action": "edit", "before": FACTS[1],
                                   "after": {**FACTS[1], "kind": "visiting"}}]
    assert out["fact_rows"][1] == {"index": 1, "kind": "visiting", "text": TSAATAN, "in_writer_seed": False}
    assert out["status"] == "verified" and out["card_id"] == CARD_ID
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["content", "reviewer", None])
async def test_edit_requires_admin_role(role):
    conn = _conn()
    with pytest.raises(HTTPException) as e:
        await _edit(conn, [{"index": 1, "kind": "visiting"}], role=role)
    assert e.value.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_errors_map_to_http_and_write_nothing():
    conn = _conn()
    with pytest.raises(HTTPException) as e:
        await _edit(conn, [{"index": 7, "kind": "visiting"}])
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await _edit(conn, [{"index": 1, "kind": "visiting", "expected_text": "other"}])
    assert e.value.status_code == 409
    assert conn.execute.await_count == 0
    conn.fetchrow.side_effect = None
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException) as e:
        await _edit(conn, [{"index": 1, "kind": "visiting"}])
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_get_card_lists_facts_with_index_and_seed_flag():
    conn = _conn()
    out = await cq.get_place_card(CARD_ID, _request(conn), x_admin_secret=SECRET)
    assert [r["index"] for r in out["fact_rows"]] == [0, 1, 2]
    assert all(r["in_writer_seed"] for r in out["fact_rows"])
    assert out["fact_kinds"] == list(place_cards.FACT_KINDS)
    with pytest.raises(HTTPException) as e:
        await cq.get_place_card("not-a-uuid", _request(conn), x_admin_secret=SECRET)
    assert e.value.status_code == 422


# ── route wiring (TestClient on the router) ───────────────────────────────────────────────────

def _client(conn):
    app = FastAPI()
    app.include_router(cq.router)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    app.state.pool = MagicMock()
    app.state.pool.acquire.return_value = ctx
    return TestClient(app)


def test_patch_facts_route():
    conn = _conn()
    client = _client(conn)
    url = f"/admin/content-quality/place-cards/{CARD_ID}/facts"
    headers = {"x-admin-secret": SECRET, "x-admin-role": "admin", "x-admin-user-id": "u1"}
    body = {"edits": [{"index": 1, "kind": "visiting", "expected_text": TSAATAN}], "reason": "visiting fact"}
    r = client.patch(url, json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["fact_rows"][1]["kind"] == "visiting"
    # reason required, role required, unknown secret refused
    assert client.patch(url, json={"edits": body["edits"]}, headers=headers).status_code == 422
    assert client.patch(url, json=body, headers={**headers, "x-admin-role": "reviewer"}).status_code == 403
    assert client.patch(url, json=body, headers={**headers, "x-admin-secret": "nope"}).status_code in (401, 403)
    # GET detail resolves to the card route, not /duplicates
    g = client.get(f"/admin/content-quality/place-cards/{CARD_ID}", headers={"x-admin-secret": SECRET})
    assert g.status_code == 200 and g.json()["fact_rows"][1]["in_writer_seed"] is False


def test_bff_write_rule_is_admin_only():
    from pathlib import Path
    rbac = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "rbac.ts").read_text()
    assert '{ prefix: "content-quality/place-cards", roles: ["admin"] }' in rbac

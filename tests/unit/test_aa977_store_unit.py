"""AA-977 — the branches of services/aa_content/store.py on a scripted connection (no DB).

The SQL itself and the CIS 275 trigger run on real Postgres in tests/integration/test_aa977_aa_content_store.py; this
file covers the store's own decisions: optimistic concurrency (stale), retired / no_draft / only_version / duplicate /
group_kind_mismatch refusals, which statement each write runs, the group lock before the kind check, and the hash a
publish stores.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import pytest

from services.aa_content import reader, store
from services.aa_content.store import ContentConflict, ContentNotFound
from services.aa_content.validate import ContentInvalid

CID = uuid.UUID("20000000-0000-4000-8000-000000000001")
T0 = datetime(2026, 10, 24, 8, 0, 0, 123456, tzinfo=timezone.utc)
ACTOR = "admin:7"


def _item(**kw: Any) -> dict:
    row = {"content_id": CID, "kind": "prep_pack", "locale": "en", "group_key": "prep-vietnam", "retired_at": None,
           "retired_by": None, "created_at": T0, "created_by": ACTOR, "updated_at": T0}
    row.update(kw)
    return row


def _version(version: int = 1, status: str = "draft", **kw: Any) -> dict:
    row = {"content_id": CID, "version": version, "status": status, "title": "Before you go: Vietnam",
           "body_markdown": "Visa: apply on https://evisa.gov.vn only.", "applies_to": json.dumps({"country_codes":
                                                                                                   ["VN"]}),
           "content_hash": None, "published_at": None, "published_by": None, "created_at": T0, "created_by": ACTOR,
           "updated_at": T0}
    row.update(kw)
    return row


class ScriptedConn:
    """Answers the store's statements from a small state; records every statement in order."""

    def __init__(self, item: Optional[dict] = None, versions: Optional[list[dict]] = None,
                 group: Optional[list[dict]] = None, insert_fails: Optional[BaseException] = None) -> None:
        self.item = item
        self.versions = versions or []
        self.group = group or []
        self.insert_fails = insert_fails
        self.ran: list[tuple[str, tuple]] = []
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self, **kw: Any):
        self.transactions += 1
        yield

    def _draft(self) -> Optional[dict]:
        return next((v for v in self.versions if v["status"] == "draft"), None)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict]:
        self.ran.append((sql, args))
        if sql in (store.ITEM_SQL, store.LOCK_ITEM_SQL):
            return self.item
        if sql == store.DRAFT_SQL:
            return self._draft()
        if sql == store.ONE_VERSION_SQL:
            return next((v for v in self.versions if v["version"] == args[1]), None)
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.ran.append((sql, args))
        if sql == store.GROUP_SQL:
            return self.group
        if sql == store.VERSIONS_SQL:
            return sorted(self.versions, key=lambda v: -v["version"])
        if sql == store.LIST_SQL:
            return [{**_item(), "published_version": 1, "draft_version": None, "title": None, "applies_to": None}]
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.ran.append((sql, args))
        if sql == store.NEWEST_SQL:
            return max((v["version"] for v in self.versions), default=0)
        if sql == store.PUBLISHED_COUNT_SQL:
            return sum(v["status"] == "published" for v in self.versions)
        if sql == store.INSERT_ITEM_SQL:
            if self.insert_fails is not None:
                raise self.insert_fails
            self.item = _item(kind=args[0], locale=args[1], group_key=args[2])
            return CID
        if sql == store.COUNT_SQL:
            return 1
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        self.ran.append((sql, args))
        if sql == store.DISCARD_SQL:
            before = len(self.versions)
            self.versions = [v for v in self.versions if v["status"] != "draft"]
            return f"DELETE {before - len(self.versions)}"
        if sql == store.INSERT_DRAFT_SQL:
            self.versions.append(_version(args[1], title=args[2], body_markdown=args[3], applies_to=args[4]))
        return "UPDATE 1"

    def statements(self) -> list[str]:
        return [sql for sql, _ in self.ran]


def run(coro):
    return asyncio.run(coro)


# ── optimistic concurrency and state refusals ───────────────────────────────

@pytest.mark.parametrize("op, kwargs", [
    (store.save_draft, {"title": "t", "body_markdown": "b", "applies_to": {}, "actor": ACTOR}),
    (store.discard_draft, {}),
    (store.publish, {"actor": ACTOR}),
    (store.retire, {"actor": ACTOR}),
    (store.restore, {}),
])
def test_every_write_refuses_a_stale_token_and_an_unknown_item(op, kwargs):
    conn = ScriptedConn(_item(), [_version()])
    with pytest.raises(ContentConflict) as exc:
        run(op(conn, CID, expected_updated_at=T0 - timedelta(microseconds=1), **kwargs))
    assert exc.value.code == "stale"
    assert not any(sql.startswith(("UPDATE", "INSERT", "DELETE")) for sql in conn.statements())
    with pytest.raises(ContentNotFound):
        run(op(ScriptedConn(None), CID, expected_updated_at=T0, **kwargs))


@pytest.mark.parametrize("op, kwargs", [
    (store.save_draft, {"title": "t", "body_markdown": "b", "applies_to": {}, "actor": ACTOR}),
    (store.publish, {"actor": ACTOR}),
])
def test_a_retired_item_takes_no_draft_and_no_publish(op, kwargs):
    conn = ScriptedConn(_item(retired_at=T0), [_version()])
    with pytest.raises(ContentConflict) as exc:
        run(op(conn, CID, expected_updated_at=T0, **kwargs))
    assert exc.value.code == "retired"


def test_retire_and_restore_refuse_the_wrong_state():
    with pytest.raises(ContentConflict) as exc:
        run(store.retire(ScriptedConn(_item(retired_at=T0)), CID, expected_updated_at=T0, actor=ACTOR))
    assert exc.value.code == "already_retired"
    with pytest.raises(ContentConflict) as exc:
        run(store.restore(ScriptedConn(_item()), CID, expected_updated_at=T0))
    assert exc.value.code == "not_retired"


def test_retire_and_restore_run_their_statement():
    conn = ScriptedConn(_item(), [_version(1, "published")])
    run(store.retire(conn, CID, expected_updated_at=T0, actor=ACTOR))
    assert (store.RETIRE_SQL, (CID, ACTOR)) in conn.ran
    conn = ScriptedConn(_item(retired_at=T0), [_version(1, "published")])
    out = run(store.restore(conn, CID, expected_updated_at=T0))
    assert store.RESTORE_SQL in conn.statements() and out["content_id"] == str(CID)


# ── save / discard ────────────────────────────────────────────────────────────

def test_save_edits_the_draft_in_place():
    conn = ScriptedConn(_item(), [_version(1, "published"), _version(2)])
    run(store.save_draft(conn, CID, title=" New ", body_markdown="b", applies_to={"country_codes": ["vn"]},
                         expected_updated_at=T0, actor=ACTOR))
    update = next(args for sql, args in conn.ran if sql == store.UPDATE_DRAFT_SQL)
    assert update == (CID, "New", "b", json.dumps({"country_codes": ["VN"]}))
    assert store.INSERT_DRAFT_SQL not in conn.statements() and store.TOUCH_SQL in conn.statements()


def test_save_after_a_publish_starts_the_next_version():
    conn = ScriptedConn(_item(), [_version(1, "published"), _version(2, "published")])
    run(store.save_draft(conn, CID, title="t", body_markdown="", applies_to={}, expected_updated_at=T0,
                         actor=ACTOR))
    insert = next(args for sql, args in conn.ran if sql == store.INSERT_DRAFT_SQL)
    assert insert == (CID, 3, "t", "", "{}", ACTOR)


def test_save_validates_before_touching_the_database():
    conn = ScriptedConn(_item(), [_version()])
    with pytest.raises(ContentInvalid):
        run(store.save_draft(conn, CID, title=" ", body_markdown="b", applies_to={}, expected_updated_at=T0,
                             actor=ACTOR))
    assert conn.ran == []


def test_discard_refuses_the_only_version_of_a_never_published_item():
    conn = ScriptedConn(_item(), [_version(1)])
    with pytest.raises(ContentConflict) as exc:
        run(store.discard_draft(conn, CID, expected_updated_at=T0))
    assert exc.value.code == "only_version"
    assert store.DISCARD_SQL not in conn.statements() and len(conn.versions) == 1


def test_discard_without_a_draft_is_404():
    with pytest.raises(ContentNotFound):
        run(store.discard_draft(ScriptedConn(_item(), [_version(1, "published")]), CID, expected_updated_at=T0))


def test_discard_drops_the_draft_after_a_publish():
    conn = ScriptedConn(_item(), [_version(1, "published"), _version(2)])
    out = run(store.discard_draft(conn, CID, expected_updated_at=T0))
    assert [v["version"] for v in out["versions"]] == [1] and out["draft_version"] is None
    assert out["state"] == "published" and store.TOUCH_SQL in conn.statements()


# ── publish ───────────────────────────────────────────────────────────────────

def test_publish_without_a_draft_is_409():
    with pytest.raises(ContentConflict) as exc:
        run(store.publish(ScriptedConn(_item(), [_version(1, "published")]), CID, expected_updated_at=T0,
                          actor=ACTOR))
    assert exc.value.code == "no_draft"


def test_publish_runs_the_visa_rule_first():
    conn = ScriptedConn(_item(), [_version(body_markdown="Apply for an eVisa before you fly.")])
    with pytest.raises(ContentInvalid) as exc:
        run(store.publish(conn, CID, expected_updated_at=T0, actor=ACTOR))
    assert exc.value.code == "visa_needs_official_link"
    assert store.PUBLISH_SQL not in conn.statements()


def test_publish_stores_the_hash_of_the_served_body():
    conn = ScriptedConn(_item(), [_version(1, "published"), _version(2)])
    now = datetime(2026, 10, 24, 15, 0, 0, 654321, tzinfo=timezone(timedelta(hours=7)))
    out = run(store.publish(conn, CID, expected_updated_at=T0, actor=ACTOR, now=now))
    args = next(a for sql, a in conn.ran if sql == store.PUBLISH_SQL)
    served = out["published"]
    assert args[0] == CID and args[1] == 2 and args[4] == ACTOR
    assert args[3] == now.astimezone(timezone.utc) and served["published_at"] == "2026-10-24T08:00:00.654321Z"
    assert args[2] == served["content_hash"] == reader.hash_of({k: v for k, v in served.items()
                                                                 if k != "content_hash"})
    assert served["version"] == 2 and served["applies_to"] == {"country_codes": ["VN"]}


# ── create ────────────────────────────────────────────────────────────────────

def _create(conn, **kw):
    args = {"kind": "prep_pack", "locale": "vi", "group_key": "prep-vietnam", "title": "Trước khi đi",
            "body_markdown": None, "applies_to": None, "actor": ACTOR}
    args.update(kw)
    return run(store.create_item(conn, **args))


def test_create_takes_the_group_lock_before_reading_the_group():
    conn = ScriptedConn(group=[_item()])
    out = _create(conn)
    assert conn.statements()[:2] == [store.GROUP_LOCK_SQL, store.GROUP_SQL]
    assert conn.ran[0][1] == ("prep-vietnam",)
    insert = next(args for sql, args in conn.ran if sql == store.INSERT_DRAFT_SQL)
    assert insert == (CID, 1, "Trước khi đi", "", "{}", ACTOR)
    assert out["draft_version"] == 1 and out["state"] == "draft"


def test_create_refuses_a_second_copy_of_a_locale_and_another_kind():
    with pytest.raises(ContentConflict) as exc:
        _create(ScriptedConn(group=[_item(locale="vi")]))
    assert exc.value.code == "duplicate"
    with pytest.raises(ContentInvalid) as exc:
        _create(ScriptedConn(group=[_item(kind="campaign_block")]))
    assert exc.value.code == "group_kind_mismatch"


def test_create_maps_a_unique_violation_to_duplicate():
    conn = ScriptedConn(insert_fails=asyncpg.UniqueViolationError("duplicate key"))
    with pytest.raises(ContentConflict) as exc:
        _create(conn)
    assert exc.value.code == "duplicate"


# ── reads ─────────────────────────────────────────────────────────────────────

def test_reads_shape_and_404():
    conn = ScriptedConn(_item(), [_version(1, "published", content_hash="sha256:" + "0" * 64, published_at=T0,
                                           published_by=ACTOR), _version(2)])
    item = run(store.get_item(conn, CID))
    assert (item["state"], item["published_version"], item["draft_version"]) == ("published", 1, 2)
    assert item["versions"][1]["published_at"] == "2026-10-24T08:00:00.123456Z"
    assert run(store.get_version(conn, CID, 2))["status"] == "draft"
    with pytest.raises(ContentNotFound):
        run(store.get_version(conn, CID, 9))
    with pytest.raises(ContentNotFound):
        run(store.get_item(ScriptedConn(None), CID))
    assert run(store.get_group(ScriptedConn(group=[]), "prep-none")) == []


def test_list_row_tolerates_an_item_without_a_title():
    page = run(store.list_items(ScriptedConn(), kind=None, locale=None, status=None, country=None, limit=25,
                                offset=0))
    assert page["total"] == 1 and page["items"][0]["title"] is None and page["items"][0]["country_codes"] == []

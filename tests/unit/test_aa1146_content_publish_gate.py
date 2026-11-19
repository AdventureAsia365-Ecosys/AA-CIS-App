"""AA-1146 D1 — the two-person publish rule (api/services/content_publish_gate.py): the pure decision and the switch
read, which fails closed. No DB: a fake connection."""
import asyncio
import json

import pytest

from api.services import content_publish_gate as gate

LEAD = frozenset({"content.read", "content.write", "review.act", "content.publish"})
WRITER = frozenset({"content.read", "content.write", "review.act"})


@pytest.mark.parametrize("switch,perms,actor,author,expected", [
    (False, WRITER, "admin:zz-1", "admin:zz-1", None),
    (False, LEAD, "admin:zz-1", "admin:zz-1", None),
    (True, WRITER, "admin:zz-1", "admin:zz-2", gate.TWO_PERSON_REQUIRED),
    (True, LEAD, "admin:zz-1", "admin:zz-1", gate.SAME_PERSON),
    (True, LEAD, "admin:zz-1", "admin:zz-2", None),
    (True, LEAD, "admin:zz-1", None, None),
])
def test_refusal(switch, perms, actor, author, expected):
    assert gate.refusal(switch=switch, perms=perms, actor=actor, author=author) == expected


def test_every_refusal_has_a_message():
    assert set(gate.MESSAGES) == {gate.TWO_PERSON_REQUIRED, gate.SAME_PERSON}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, row=None, exc=None):
        self.row, self.exc = row, exc

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, key):
        assert key == gate.PUBLISH_TWO_PERSON_KEY
        if self.exc is not None:
            raise self.exc
        return self.row


@pytest.mark.parametrize("row,exc,expected", [
    ({"value_text": json.dumps(True)}, None, True),
    ({"value_text": json.dumps(False)}, None, False),
    ({"value_text": "not json"}, None, True),
    ({"value_text": json.dumps("yes")}, None, True),
    (None, RuntimeError("zz connection lost"), True),
])
def test_switch_on_reads_the_row_and_fails_closed(row, exc, expected):
    assert asyncio.run(gate.switch_on(_Conn(row, exc))) is expected


def test_switch_on_without_a_value_row_is_the_default(monkeypatch):
    monkeypatch.setattr(gate, "_default", lambda: False)
    assert asyncio.run(gate.switch_on(_Conn(None))) is False

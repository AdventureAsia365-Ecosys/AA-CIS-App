"""AA-1096 l23 — Platform › Settings explains each setting: the API item carries the operator's guide (from the
compiled defaults, CIS 415) and the rows of the last change / history carry the staff member's name next to the actor
id (the rows keep admin:<uuid>, CIS 407). No DB: a fake connection answers the staff-name read."""
import pytest

from api.services import settings_registry as reg
from shared import settings

ID_A = "00000000-0000-4000-8000-0000000000aa"
ID_B = "00000000-0000-4000-8000-0000000000bb"


class _Conn:
    def __init__(self, names):
        self.names, self.calls = names, []

    async def fetch(self, sql, ids):
        self.calls.append(list(ids))
        return [{"id": i, "name": self.names[i]} for i in ids if i in self.names]


def test_the_item_carries_the_guide_of_its_key(monkeypatch):
    guide = {"en": {"what": "w"}, "vi": {"what": "v"}}
    monkeypatch.setitem(settings.GUIDES, "cis.publish.daily_limit_default", guide)
    out = reg._definition_out(settings.definition("cis.publish.daily_limit_default"))
    assert out["guide"] == guide
    monkeypatch.setitem(settings.GUIDES, "cis.publish.daily_limit_default", None)
    assert reg._definition_out(settings.definition("cis.publish.daily_limit_default"))["guide"] is None


def test_every_compiled_key_has_a_guide_entry():
    assert set(settings.GUIDES) == set(settings.READER.keys())
    assert settings.guide("cis.zz.unknown") is None


@pytest.mark.asyncio
async def test_actor_names_read_once_and_keep_unknown_actors():
    conn = _Conn({ID_A: "Zz Admin A"})
    rows = [{"proposed_by": f"admin:{ID_A}", "decided_by": f"admin:{ID_B.upper()}"},
            {"proposed_by": "admin:zz-script", "decided_by": None},
            {"proposed_by": f"admin:{ID_A}", "decided_by": f"admin:{ID_A}"}]
    await reg._actor_names(conn, rows)
    assert conn.calls == [sorted([ID_A, ID_B])]
    assert [(r["proposed_by_name"], r["decided_by_name"]) for r in rows] == [
        ("Zz Admin A", None), (None, None), ("Zz Admin A", "Zz Admin A")]


@pytest.mark.asyncio
async def test_actor_names_skip_the_read_without_staff_actors():
    conn = _Conn({})
    rows = [{"proposed_by": "seed:zz", "decided_by": None}]
    await reg._actor_names(conn, rows)
    assert conn.calls == [] and rows[0]["proposed_by_name"] is None and rows[0]["decided_by_name"] is None

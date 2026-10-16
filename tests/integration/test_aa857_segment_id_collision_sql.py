"""AA-857 — the collision guard's SQL against real Postgres (no FK bypass needed: only
atom_segment / atom_segment_alias rows, inside a transaction that is rolled back).

Covers: the label upsert (`_SEGMENT_UPSERT_SQL`) keeps an unclaimed id's label (`WHERE $4::boolean`),
the alias chain resolver, and that the id-facts / mixed-country queries run and answer for ids without members.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_contract import segment_matching as sm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def tx():
    c = await asyncpg.connect(DSN)
    t = c.transaction()
    await t.start()
    yield c
    await t.rollback()
    await c.close()


@pytest.mark.asyncio
async def test_unclaimed_id_keeps_its_label(tx):
    sql = sm._SEGMENT_UPSERT_SQL
    await tx.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                     "VALUES ('zz857_keep', 'Delhi', 'explore'), ('zz857_mine', 'Old', 'visit')")
    await tx.executemany(sql, [("zz857_keep", "hot spring hotel", "eat", False),
                               ("zz857_mine", "New", "visit", True),
                               ("zz857_fresh", "Fresh", "visit", False)])
    rows = await tx.fetch("SELECT segment_id, canonical_place FROM acp_contract.atom_segment "
                          "WHERE segment_id LIKE 'zz857_%' ORDER BY 1")
    assert [(r["segment_id"], r["canonical_place"]) for r in rows] == [
        ("zz857_fresh", "Fresh"), ("zz857_keep", "Delhi"), ("zz857_mine", "New")]


@pytest.mark.asyncio
async def test_alias_chain_and_id_facts(tx):
    await tx.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                     "VALUES ('zz857_a', 'A', 'v'), ('zz857_b', 'B', 'v'), ('zz857_c', 'C', 'v')")
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical) "
                     "VALUES ('zz857_a', 'zz857_b'), ('zz857_b', 'zz857_c')")
    assert await sm.resolve_segment_aliases(tx, ["zz857_a", "zz857_c", "zz857_none"]) == {
        "zz857_a": "zz857_c", "zz857_c": "zz857_c", "zz857_none": "zz857_none"}

    facts = {r["segment_id"]: r for r in await tx.fetch(sm._ID_FACTS_SQL, ["zz857_a", "zz857_c", "zz857_none"])}
    assert (facts["zz857_a"]["taken"], facts["zz857_a"]["alias_old"]) == (True, True)
    assert (facts["zz857_c"]["taken"], facts["zz857_c"]["alias_old"]) == (True, False)
    assert (facts["zz857_none"]["taken"], facts["zz857_none"]["alias_old"]) == (False, False)
    assert all(list(r["countries"]) == [] for r in facts.values())

    assert all(r["has_members"] is False for r in facts.values())

    assert await tx.fetch(sm._MIXED_WRITTEN_SQL, ["zz857_a", "zz857_c"]) == []


@pytest.mark.asyncio
async def test_alias_chain_stops_at_a_revived_old_id(tx):
    """Before AA-857 a fresh mint could write members into an alias old id and leave the alias
    row; such an id is live and must not resolve to the alias target. Member rows reference
    tour_atoms through an FK; FK triggers are off for this rolled-back transaction (replica mode,
    test superuser — the S218 repoint test does the same)."""
    await tx.execute("SET LOCAL session_replication_role = replica")
    await tx.execute("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action) "
                     "VALUES ('zz857_x', 'X', 'v'), ('zz857_y', 'Y', 'v'), ('zz857_p', 'P', 'v'), "
                     "('zz857_q', 'Q', 'v')")
    await tx.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical) "
                     "VALUES ('zz857_x', 'zz857_y'), ('zz857_p', 'zz857_q')")
    await tx.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) "
                     "VALUES ('zz857_x', 'zz857_atom_1')")
    assert await sm.resolve_segment_aliases(tx, ["zz857_x", "zz857_p"]) == {
        "zz857_x": "zz857_x", "zz857_p": "zz857_q"}
    assert await sm.live_segment_labels(tx, ["zz857_x", "zz857_p"]) == {"zz857_p": ("zz857_q", "Q", "v")}
    facts = {r["segment_id"]: r for r in await tx.fetch(sm._ID_FACTS_SQL, ["zz857_x", "zz857_y"])}
    assert (facts["zz857_x"]["has_members"], facts["zz857_y"]["has_members"]) == (True, False)

"""S218 — Segment merge re-points members without hitting atom_segment_member_pkey (real Postgres).

From 05/10/2026 every tour's Segment step failed: re-pointing a losing Segment's members with a
bare UPDATE collided when an atom was already a member of the surviving Segment, the whole
transaction rolled back, and the same merge was derived (and failed) again for every later tour.
"""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_contract.segment_matching import _repoint_members  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SEGS = ("s218_old_a", "s218_old_b", "s218_target")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    # Members reference tour_atoms/atom_segment through FKs; this test is about the member rows
    # only, so FK triggers are off for the session (test superuser).
    await c.execute("SET session_replication_role = replica")
    await c.execute("DELETE FROM acp_contract.atom_segment_member WHERE segment_id = ANY($1::text[])", list(SEGS))
    yield c
    await c.execute("DELETE FROM acp_contract.atom_segment_member WHERE segment_id = ANY($1::text[])", list(SEGS))
    await c.close()


async def _members(c):
    rows = await c.fetch("SELECT segment_id, atom_id FROM acp_contract.atom_segment_member "
                         "WHERE segment_id = ANY($1::text[]) ORDER BY 1, 2", list(SEGS))
    return [(r["segment_id"], r["atom_id"]) for r in rows]


@pytest.mark.asyncio
async def test_atom_already_in_target_does_not_abort_the_merge(conn):
    await conn.executemany("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)", [
        ("s218_old_a", "atom1"), ("s218_old_a", "atom2"),
        ("s218_old_b", "atom2"), ("s218_old_b", "atom3"),
        ("s218_target", "atom1"),
    ])
    async with conn.transaction():
        await _repoint_members(conn, [("s218_old_a", "s218_target"), ("s218_old_b", "s218_target")])
    assert await _members(conn) == [("s218_target", "atom1"), ("s218_target", "atom2"), ("s218_target", "atom3")]


@pytest.mark.asyncio
async def test_old_bare_update_would_have_collided(conn):
    """Guards the premise: the pre-S218 statement fails on the same data."""
    await conn.executemany("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                           [("s218_old_a", "atom1"), ("s218_target", "atom1")])
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("UPDATE acp_contract.atom_segment_member SET segment_id = 's218_target' "
                           "WHERE segment_id = 's218_old_a'")

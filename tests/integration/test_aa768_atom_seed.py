"""AA-768 — the atom seed queries (atom fields + Segment-sibling evidence) run on the real schema."""
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_shared.atom_seed import atom_seed_text  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest.mark.asyncio
async def test_atom_seed_queries_run_on_the_real_tables():
    conn = await asyncpg.connect(DSN)
    try:
        # Raises on a bad column/type in either query; an unknown atom is simply not visible.
        assert await atom_seed_text(conn, "no-such-atom", "00000000-0000-0000-0000-000000000000") is None
        from services.acp_shared.atom_seed import _SIBLING_EVIDENCE_QUERY
        assert await conn.fetch(_SIBLING_EVIDENCE_QUERY, "no-such-atom", "platform") == []
    finally:
        await conn.close()

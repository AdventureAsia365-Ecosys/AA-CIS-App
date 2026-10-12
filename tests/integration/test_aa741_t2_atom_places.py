"""AA-741 (S231) — the atom-places query T2's market SEO now uses runs on the real schema
(view acp_contract.v_active_tour_atoms from the migrations), not just against a mock."""
import os
import sys
import uuid

import asyncpg
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.seo_intelligence.s1_prefetch import ATOM_PLACES_SQL, atom_places  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest.mark.asyncio
async def test_atom_places_sql_runs_on_the_real_view():
    conn = await asyncpg.connect(DSN)
    try:
        assert await conn.fetch(ATOM_PLACES_SQL, str(uuid.uuid4())) == []   # raises on a bad query
        assert await atom_places(conn, str(uuid.uuid4())) == []
    finally:
        await conn.close()

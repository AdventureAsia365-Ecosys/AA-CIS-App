"""S291 (C4.3 review, AA-838) on PostgreSQL (the test login; the role does not change savepoints): the depth-named savepoints of the CIS pools
(``shared/pg_connection.py``) roll back exactly what asyncpg's unique names did (an outer block that fails after a
caught inner rollback undoes its own work too; a failed statement two levels down leaves the outer block usable), and
only ``cis_sp_<depth>`` texts reach the server. Temporary table only; nothing in a CIS schema is written."""

import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.pg_connection import StableSavepointConnection  # noqa: E402

pytestmark = pytest.mark.asyncio

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ALLOWED = {
    "SAVEPOINT cis_sp_1;", "SAVEPOINT cis_sp_2;", "RELEASE SAVEPOINT cis_sp_1;", "RELEASE SAVEPOINT cis_sp_2;",
    "ROLLBACK TO cis_sp_1;", "ROLLBACK TO cis_sp_2;",
}


async def test_depth_named_savepoints_roll_back_what_asyncpg_did():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1, connection_class=StableSavepointConnection)
    try:
        async with pool.acquire() as c:
            assert isinstance(c, StableSavepointConnection)
            sent = []
            c.add_query_logger(lambda record: sent.append(str(record.query)))
            async with c.transaction():
                await c.execute("CREATE TEMP TABLE zz_sp (v int) ON COMMIT DROP")
                await c.execute("INSERT INTO zz_sp VALUES (1)")
                with pytest.raises(KeyError):
                    async with c.transaction():
                        await c.execute("INSERT INTO zz_sp VALUES (2)")
                        with pytest.raises(ValueError):
                            async with c.transaction():
                                await c.execute("INSERT INTO zz_sp VALUES (3)")
                                raise ValueError
                        await c.execute("INSERT INTO zz_sp VALUES (4)")
                        async with c.transaction():
                            await c.execute("INSERT INTO zz_sp VALUES (5)")
                        raise KeyError
                async with c.transaction():
                    await c.execute("INSERT INTO zz_sp VALUES (6)")
                rows = [r["v"] for r in await c.fetch("SELECT v AS v FROM zz_sp ORDER BY v")]
                # A failed statement inside a depth-2 block leaves the outer block usable.
                with pytest.raises(asyncpg.DivisionByZeroError):
                    async with c.transaction():
                        async with c.transaction():
                            await c.execute("SELECT 1 / 0")
                assert await c.fetchval("SELECT count(*) AS n FROM zz_sp") == 2
        assert rows == [1, 6]
        savepoints = {q for q in sent if "SAVEPOINT" in q or q.startswith("ROLLBACK TO")}
        assert savepoints and savepoints <= ALLOWED
    finally:
        await pool.close()

"""S291 (C4.3 review, AA-838): the CIS api, tenant and job worker pools name savepoints by depth (``cis_sp_1``,
``cis_sp_2``), so ``pg_stat_statements`` (it keys a utility statement by its text on PostgreSQL 15) sees a handful of
SAVEPOINT texts, not one per nested transaction. No server here: the statement texts the connection class sends. The
PostgreSQL side (rollback semantics) is in tests/integration/test_s291_pg_connection_pg.py."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from asyncpg.transaction import TransactionState

from shared.pg_connection import SAVEPOINT_PREFIX, StableSavepointConnection, StableSavepointTransaction, savepoint_name


class _Protocol:
    def is_in_transaction(self) -> bool:
        return False

    def is_connected(self) -> bool:
        return True


class FakeConnection(StableSavepointConnection):
    """The connection class without a server: ``execute`` records the statement text."""

    def __init__(self) -> None:  # asyncpg's own __init__ needs a live protocol
        self._top_xact = None
        self._pool_release_ctr = 0
        self._aborted = False
        self._protocol = _Protocol()
        self._cis_savepoints = []
        self.sent: list[str] = []
        self.fail_on: str | None = None

    def __del__(self) -> None:  # asyncpg warns about an unclosed live connection; this one never opened
        pass

    async def execute(self, query: str, *args: Any, timeout: float | None = None) -> str:
        self.sent.append(query)
        if self.fail_on is not None and query.startswith(self.fail_on):
            raise RuntimeError("refused")
        return "OK"


@pytest.mark.asyncio
async def test_savepoints_are_named_by_their_depth_and_the_names_repeat() -> None:
    con = FakeConnection()
    for _ in range(3):  # three requests on one pooled connection: the same texts every time
        async with con.transaction():
            async with con.transaction():
                async with con.transaction():
                    pass
            async with con.transaction():  # a sibling block reuses depth 1
                pass
    assert isinstance(con.transaction(), StableSavepointTransaction)
    assert set(con.sent) == {
        "BEGIN;", "SAVEPOINT cis_sp_1;", "SAVEPOINT cis_sp_2;", "RELEASE SAVEPOINT cis_sp_2;",
        "RELEASE SAVEPOINT cis_sp_1;", "COMMIT;",
    }
    assert con.sent[:5] == ["BEGIN;", "SAVEPOINT cis_sp_1;", "SAVEPOINT cis_sp_2;", "RELEASE SAVEPOINT cis_sp_2;",
                            "RELEASE SAVEPOINT cis_sp_1;"]
    assert savepoint_name(3) == f"{SAVEPOINT_PREFIX}3" == "cis_sp_3"


@pytest.mark.asyncio
async def test_a_rolled_back_block_frees_its_depth_and_rolls_back_to_its_own_name() -> None:
    con = FakeConnection()
    async with con.transaction():
        with pytest.raises(ValueError):
            async with con.transaction():
                raise ValueError
        async with con.transaction():  # depth 1 again: the newest cis_sp_1 is this block's
            with pytest.raises(KeyError):
                async with con.transaction():
                    raise KeyError
    assert con.sent == [
        "BEGIN;", "SAVEPOINT cis_sp_1;", "ROLLBACK TO cis_sp_1;", "SAVEPOINT cis_sp_1;", "SAVEPOINT cis_sp_2;",
        "ROLLBACK TO cis_sp_2;", "RELEASE SAVEPOINT cis_sp_1;", "COMMIT;",
    ]


@pytest.mark.asyncio
async def test_a_savepoint_that_failed_to_start_takes_no_depth_and_a_new_transaction_starts_at_one() -> None:
    con = FakeConnection()
    tx = con.transaction()
    await tx.start()
    con.fail_on = "SAVEPOINT"
    with pytest.raises(RuntimeError):
        async with con.transaction():
            pass
    con.fail_on = None
    async with con.transaction():
        pass
    await tx.commit()
    # A savepoint left open by a broken caller does not leak into the next top-level transaction.
    con._cis_savepoints = [type("Stale", (), {"_state": TransactionState.STARTED})()]
    async with con.transaction():
        async with con.transaction():
            pass
    assert con.sent[-4:] == ["BEGIN;", "SAVEPOINT cis_sp_1;", "RELEASE SAVEPOINT cis_sp_1;", "COMMIT;"]
    assert con.sent.count("SAVEPOINT cis_sp_1;") == 3  # the failed start, the retry, the new transaction


def test_other_unique_ids_keep_asyncpg_s_counter() -> None:
    con = FakeConnection()
    first, second = con._get_unique_id("stmt"), con._get_unique_id("stmt")
    assert first != second and first.startswith("__asyncpg_stmt_")


def test_the_api_tenant_and_worker_pools_use_the_connection_class() -> None:
    from api import main
    from api.core import aa544_tenant_pool
    from shared.jobs import worker

    for source in (inspect.getsource(main), inspect.getsource(aa544_tenant_pool.create_tenant_pool),
                   inspect.getsource(worker)):
        assert "connection_class=StableSavepointConnection" in source

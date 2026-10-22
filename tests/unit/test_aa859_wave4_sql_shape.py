"""AA-859 wave 4 — the shape of the SQL the moved routes run (the constants the integration tests mutate), the
base-table atom reads that replace v_active_tour_atoms on the pick path (C9), the LATERAL trip page (C12) and the
alias-card savepoint inside a transaction (C10)."""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from services.acp_angle_gate import service as ag_service
from services.acp_planning import quarter, tenant_pool
from services.acp_shared import route_guard, slate, subject_seed

def test_every_subject_statement_keeps_its_tenant_filter():
    for sql in (slate._SUBJECT_LOCK_SQL, slate._SUBJECT_PICKED_SQL, slate._SUBJECT_CUT_SQL, slate._SUBJECT_STATE_SQL):
        assert "tenant_id = $2::uuid" in sql
    assert slate._SUBJECT_LOCK_SQL.rstrip().endswith("FOR UPDATE")
    for sql in (slate._SUBJECT_PICKED_SQL, slate._SUBJECT_CUT_SQL):
        assert "AND state = 'proposed'" in sql
    # 265 grants the tenant role UPDATE (state) only on subject
    for sql in (slate._SUBJECT_PICKED_SQL, slate._SUBJECT_CUT_SQL):
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        assert set_clause.strip().startswith("state = ") and "," not in set_clause
    assert "WHERE s.tenant_id = $1::uuid" in slate._SLATE_SUBJECTS_SQL


def test_angle_gate_reads_keep_their_tenant_filter():
    assert "WHERE request_id = $1 AND tenant_id = $2" in ag_service._REQUEST_ROW_SQL
    assert "s.subject_id = $1 AND s.tenant_id = $2" in ag_service._SUBJECT_HEADER_SQL
    assert "shared.job" in ag_service._LAST_GENERATION_SQL


def test_pick_reads_atoms_on_the_base_table_with_the_203_rule():
    for sql in (subject_seed._MEMBERS_SQL, route_guard._TRIP_MEMBER_SQL):
        assert "v_active_tour_atoms" not in sql
        assert "JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id" in sql
        assert "pt.master_status = 'active' AND pt.deleted_at IS NULL" in sql
        assert "NOT ta.deleted AND NOT ta.is_empty_marker" in sql
        assert "ta.owner_scope IN ('platform', $" in sql


def test_tenant_trip_query_takes_one_trip_page_per_tour():
    sql = tenant_pool._TENANT_TRIP_QUERY
    assert "LEFT JOIN LATERAL" in sql and "LIMIT 1" in sql
    assert "p.tenant_id IN ($2::text, '00000000-0000-0000-0000-000000000001')" in sql
    assert "ORDER BY (p.tenant_id = $2::text) DESC" in sql
    assert "ttv.tenant_id = $1::uuid" in sql


def test_trip_query_binds_the_tenant_twice():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    tid = uuid.uuid4()
    asyncio.run(tenant_pool.fetch_tenant_trips_on(conn, tid))
    assert conn.fetch.await_args.args[1:] == (tid, str(tid))


def test_planning_writes_touch_only_granted_columns():
    """265's column grants: year_plan INSERT (tenant_id, year); quarter_plan INSERT (tenant_id, year, quarter,
    year_plan_id) / UPDATE (year_plan_id, current_version_id); versions INSERT (plan_id, version_no, payload, source,
    approval_status) / UPDATE (approval_status, approved_by, approved_at)."""
    assert "(tenant_id, year)" in quarter._YEAR_PLAN_UPSERT_SQL
    assert "(tenant_id, year, quarter, year_plan_id)" in quarter._QUARTER_PLAN_UPSERT_SQL
    assert "SET year_plan_id = " in quarter._QUARTER_PLAN_UPSERT_SQL
    assert "(plan_id, version_no, payload, source, approval_status)" in quarter._VERSION_INSERT_SQL
    assert "SET approval_status = 'approved', approved_by = $2, approved_at = now()" in quarter._VERSION_APPROVE_SQL
    assert "SET current_version_id = $2" in quarter._CURRENT_VERSION_SQL


class _TxConn:
    """An asyncpg-like connection inside a transaction (is_in_transaction on the class, as on the pool proxy)."""

    def __init__(self, error=None):
        self.error, self.savepoints = error, 0

    def is_in_transaction(self):
        return True

    def transaction(self):
        conn = self

        class _Sp:
            async def __aenter__(self):
                conn.savepoints += 1

            async def __aexit__(self, *exc):
                return False
        return _Sp()

    async def fetch(self, sql, *args):
        if self.error:
            raise self.error
        return [{"alias_norm": "hoan kiem lake", "country_norm": "vietnam", "card_id": "c1"}]


def test_alias_cards_run_in_a_savepoint_inside_a_transaction():
    conn = _TxConn()
    assert asyncio.run(subject_seed.load_alias_cards(conn, ["Hoan Kiem Lake"])) == {
        ("hoan kiem lake", "vietnam"): "c1"}
    assert conn.savepoints == 1
    failing = _TxConn(error=RuntimeError("permission denied"))
    assert asyncio.run(subject_seed.load_alias_cards(failing, ["Hoan Kiem Lake"])) == {}
    assert failing.savepoints == 1, "the failed read was inside its savepoint (the caller's transaction survives)"


def test_alias_cards_without_a_transaction_take_no_savepoint():
    class _Plain:
        async def fetch(self, sql, *args):
            return []
    assert asyncio.run(subject_seed.load_alias_cards(_Plain(), ["Hoan Kiem Lake"])) == {}
    mock = MagicMock()                      # a mock answers anything: still no savepoint
    mock.fetch = AsyncMock(return_value=[])
    assert asyncio.run(subject_seed.load_alias_cards(mock, ["Hoan Kiem Lake"])) == {}
    mock.transaction.assert_not_called()

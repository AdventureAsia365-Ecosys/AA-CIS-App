"""AA-1093 (S281 lane l2-erase-crm): schema learn (CIS 420) is part of the tenant erasure (no DB). The DB behaviour is
in tests/integration/test_aa1093_learn_erasure_db.py.
"""
from __future__ import annotations

import pytest

from services.erasure import inventory as inv
from services.erasure import plan as pl
from tests.unit.test_aa839_erasure import TID, E, _catalog, _registry


LEARN_TENANT_TABLES = {"learn.learner", "learn.enrollment", "learn.statement", "learn.lesson_progress",
                       "learn.quiz_attempt", "learn.review_card", "learn.lab_run", "learn.certificate"}


def test_learn_is_a_cis_schema_and_every_tenant_table_of_it_is_deleted():
    assert "learn" in inv.CIS_SCHEMAS
    by_name = inv.TENANT_TABLE_BY_NAME
    assert LEARN_TENANT_TABLES <= set(by_name)
    assert {by_name[t].action for t in LEARN_TENANT_TABLES} == {"delete"}
    # every delete setting names a deleted inventory table (a renamed table cannot leave a stale setting)
    assert all(by_name[t].action == "delete" for t in inv.DELETE_SETTINGS)
    assert inv.DELETE_SETTINGS["learn.statement"] == ("learn.retention_purge", "on")


class _RecordingConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "DELETE 0"

    async def fetchval(self, sql, *args):
        return 0


@pytest.mark.asyncio
async def test_apply_sets_the_statement_purge_flag_before_its_deletes():
    reg = _registry(inv.TenantTable("learn.learner", "delete", "learners"),
                    inv.TenantTable("learn.statement", "delete", "statements"))
    cat = _catalog(["learn.learner", "learn.statement"], {"learn.learner": "uuid", "learn.statement": "uuid"},
                   [E("statement_learner_fk", "learn.statement", "learn.learner", ("learner_id",), ("learner_id",),
                      deltype="c")])
    plan = pl.plan_tenant(reg, cat)
    conn = _RecordingConn()
    await pl.apply_tenant(conn, plan, TID, {}, {})
    sqls = [sql for sql, _ in conn.calls]
    flag = sqls.index("SELECT set_config($1, $2, true)")
    assert conn.calls[flag][1] == ("learn.retention_purge", "on")
    assert flag < min(i for i, s in enumerate(sqls) if s.startswith("DELETE FROM"))
    # a plan without learn.statement sets nothing
    other = pl.plan_tenant(_registry(inv.TenantTable("learn.learner", "delete", "l")),
                           _catalog(["learn.learner"], {"learn.learner": "uuid"}, []))
    conn = _RecordingConn()
    await pl.apply_tenant(conn, other, TID, {}, {})
    assert not [s for s, _ in conn.calls if "set_config" in s]

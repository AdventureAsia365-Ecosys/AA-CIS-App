"""AA-1036 R3 slice 3 — CIS 369 (destructive): shared.tenants.api_key_hash is dropped (tenant API keys are gone).

conftest replays 369 like every file, after 368. Each test runs the file body (errors not swallowed) inside one
transaction that is rolled back, so the shared test DB keeps the replay state (the column absent):

  * the replay dropped the column and registered 369 once;
  * with the column back (a stand-in, the pre-369 Dev state: 368 registered, every value NULL) the file drops it,
    says so and registers 369;
  * a value written after 368 stops the file (the writer must be found first), and so does a missing 368;
  * a view that grew a dependency on the column stops the drop (RESTRICT) instead of taking the view with it;
  * on a DB without the column the file only registers 369, and a second run is a no-op.
"""
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import KEY_HASH_PRESENT_SQL, KEY_HASH_STANDIN_SQL, migration_body  # noqa: E402

BODY = migration_body(369)
TAG = uuid.uuid4().hex[:8]


@pytest.fixture
def db():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    k = c.cursor()
    try:
        yield c, k
    finally:
        c.rollback()
        c.close()


def _present(k) -> bool:
    k.execute(KEY_HASH_PRESENT_SQL)
    return k.fetchone()[0]


def _registered(k, version: str) -> int:
    k.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = %s", (version,))
    return k.fetchone()[0]


def _pre_369(k, key_hash=None):
    """The Dev state the DRY RUN checked: the column present, 368 registered, 369 not; one invented tenant row."""
    k.execute(KEY_HASH_STANDIN_SQL)
    k.execute("DELETE FROM shared.schema_versions WHERE version = '369'")
    k.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, api_key_hash, "
              "rate_limit_rpm, is_active) VALUES (%s, %s, %s, 'starter', 3, %s, 60, true)",
              (str(uuid.uuid4()), f"Fernhollow Rambles {TAG}", f"aa1036-369-{TAG}", key_hash))


def _refused(c, k, message: str, error=psycopg2.errors.RaiseException):
    """The body fails with `message`; the savepoint shows nothing was dropped or registered."""
    k.execute("SAVEPOINT before_369")
    with pytest.raises(error) as info:
        k.execute(BODY)
    k.execute("ROLLBACK TO SAVEPOINT before_369")
    assert message in str(info.value), info.value
    assert _present(k) and _registered(k, "369") == 0


def test_the_replay_dropped_the_column_and_registered_369(db):
    _, k = db
    assert not _present(k), "conftest swallowed an error in 369 (see its log)"
    assert _registered(k, "368") == 1 and _registered(k, "369") == 1


def test_drops_the_column_when_present_and_null(db):
    c, k = db
    _pre_369(k)
    assert _present(k)
    c.notices.clear()
    k.execute(BODY)
    assert not _present(k)
    assert _registered(k, "369") == 1
    assert any("migration 369: dropped shared.tenants.api_key_hash" in n for n in c.notices), c.notices
    k.execute("SELECT count(*) AS n FROM shared.tenants WHERE slug = %s", (f"aa1036-369-{TAG}",))
    assert k.fetchone()[0] == 1                                   # the row stays, only the column goes


def test_refuses_when_a_value_was_written_after_368(db):
    c, k = db
    _pre_369(k, key_hash="written-after-368")
    _refused(c, k, "holds a value written after 368")


def test_refuses_without_368(db):
    c, k = db
    _pre_369(k)
    k.execute("DELETE FROM shared.schema_versions WHERE version = '368'")
    _refused(c, k, "needs 368")


def test_a_dependent_view_stops_the_drop(db):
    c, k = db
    _pre_369(k)
    k.execute("CREATE VIEW shared.zz_aa1036_key_view AS SELECT tenant_id, api_key_hash FROM shared.tenants")
    _refused(c, k, "zz_aa1036_key_view", error=psycopg2.errors.DependentObjectsStillExist)


def test_registers_only_when_the_column_is_absent_and_reruns_are_noops(db):
    c, k = db
    k.execute("DELETE FROM shared.schema_versions WHERE version = '369'")
    c.notices.clear()
    k.execute(BODY)
    k.execute(BODY)
    assert not _present(k)
    assert _registered(k, "369") == 1
    absent = [n for n in c.notices if "migration 369: shared.tenants.api_key_hash is already absent" in n]
    assert len(absent) == 2, c.notices

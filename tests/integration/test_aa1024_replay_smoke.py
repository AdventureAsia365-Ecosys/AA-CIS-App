"""AA-1024 l22 (G6 follow-up): the integration smoke file of scripts/pre-push.sh (SMOKE_INTEGRATION).

A change to tests/integration/conftest.py, or to a helper it imports (_replay_guard.py, _constants.py), changes every
integration session but no test file names it, so the pre-push stamp used to be only a WARN. Now the stamp covers this
file: one cheap test whose session runs the whole schema replay (and its replay guard) and the db_conn fixture
(seed + truncate). Keep it cheap: no service import, no data of its own.
"""


def test_the_replay_ran_and_db_conn_works(db_conn):
    cur = db_conn.cursor()
    cur.execute("SELECT 1 AS ok")
    assert cur.fetchone()[0] == 1
    # Every migration registers itself in shared.schema_versions: an empty table means the replay did not run.
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions")
    assert cur.fetchone()[0] > 0
    cur.execute("SELECT current_setting('app.tenant_id', true) AS tenant")
    assert cur.fetchone()[0]
    cur.close()

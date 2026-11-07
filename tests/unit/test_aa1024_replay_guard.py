"""AA-1024 l22 (S271 lessons guard G16): a migration that fails during the integration test replay is an error.

tests/integration/conftest.py used to print "Warning <file>: <error>" and go on, so migration 203 and about 50 GRANTs
never reached the test DB while the tests stayed green (lesson S231, AA-741). The rule lives in
tests/integration/_replay_guard.py (pure, read here without psycopg2); this test checks it and that conftest uses it.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INTEGRATION = REPO / "tests" / "integration"
MIGRATIONS = REPO / "api" / "migrations"

_spec = importlib.util.spec_from_file_location("aa1024_replay_guard", INTEGRATION / "_replay_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def test_new_failure_is_a_problem():
    got = guard.replay_problems({"019_social_content.sql": "x", "384_widget.sql": "relation does not exist"},
                                {"019_social_content.sql": "x"})
    assert got == ["384_widget.sql failed during the test replay: relation does not exist"]


def test_stale_expected_entry_is_a_problem_unless_pgvector_is_installed():
    got = guard.replay_problems({}, {"041_pgvector_embeddings.sql": 'extension "vector" is not available'})
    assert len(got) == 1 and "now applies: delete the entry" in got[0]
    assert guard.replay_problems({}, {"041_pgvector_embeddings.sql": "x"}, vector_available=True) == []


def test_an_expected_file_failing_with_another_error_is_a_problem():
    expected = {"019_social_content.sql": 'schema "acp_silver_s4" does not exist'}
    # The recorded error is a substring of the first line: expected (psycopg2's LINE / HINT lines are ignored).
    assert guard.replay_problems({"019_social_content.sql": 'schema "acp_silver_s4" does not exist\nLINE 3: x'},
                                 expected) == []
    got = guard.replay_problems({"019_social_content.sql": 'syntax error at or near "CREAT"\nLINE 1: CREAT'}, expected)
    assert len(got) == 1
    assert got[0].startswith("019_social_content.sql failed during the test replay with another error")
    assert 'syntax error at or near "CREAT"' in got[0] and "acp_silver_s4" in got[0]
    # The expected text must sit on the first line, not in the detail lines below it.
    assert len(guard.replay_problems({"019_social_content.sql": 'permission denied\nschema "acp_silver_s4" does not '
                                      "exist"}, expected)) == 1
    # A local Postgres with pgvector replays another chain: no mismatch check there (as for stale entries).
    assert guard.replay_problems({"019_social_content.sql": "other"}, expected, vector_available=True) == []


def test_the_expected_set_matches_the_ci_replay_shape():
    assert guard.replay_problems(dict(guard.EXPECTED_REPLAY_FAILURES)) == []
    for name in guard.EXPECTED_REPLAY_FAILURES:
        assert (MIGRATIONS / name).is_file(), f"{name} is expected to fail but does not exist (renumbered?)"


def test_first_line_drops_psycopg2_detail():
    assert guard.first_line(Exception('relation "x" does not exist\nLINE 1: SELECT')) == 'relation "x" does not exist'
    assert guard.first_line(ValueError()) == "ValueError"


def test_conftest_records_every_failure_and_exits_on_problems():
    src = (INTEGRATION / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_apply_schema")
    body = ast.unparse(fn)
    assert "replay_failed[mf] = first_line(e)" in body, "the migration loop must record each failure"
    assert "replay_problems(replay_failed" in body and "pytest.exit(exit_message(problems)" in body
    assert "rolsuper" in body and "pytest.exit(f'integration conftest: {SUPERUSER_HINT}'" in body
    # The only bare 'except Exception' in the replay loop is the recorded one: no new silent swallow.
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and isinstance(h.type, ast.Name)
                and h.type.id == "Exception"]
    assert len(handlers) == 1

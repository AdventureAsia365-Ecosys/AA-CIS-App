"""
AA-751 guard: no runtime code may read or write the dropped ACP v1 run table.

`acp_shared.acp_runs` was dropped by migration 121. The look-alike `shared.acp_runs` (migration
009, never dropped) has no reader either and is a Dead Table Registry candidate, so the guard
covers both names. AA-751 deleted the last readers (`GET /admin/acp/stuck-runs`,
`POST /admin/acp/runs/{run_id}/force-fail`, `services/acp_shared/cost_utils.py`,
`services/acp_canary/`). Stuck work is a `shared.job` row now (reaper + the admin Jobs page),
so a new SQL statement against `acp_runs` is a bug: it fails at runtime with
"relation does not exist".

Comments and docstrings that mention the table as history are allowed: only a SQL keyword
(FROM / JOIN / UPDATE / INTO) directly followed by the table name counts. The regex runs over the
whole file text, so a keyword and table name split across lines of a triple-quoted SQL block are
still caught; the bare `acp_runs` form is caught too, since `search_path` could resolve it.
Migrations are `.sql` and are not scanned; neither is `tests/` (the integration conftest keeps an
FK stub).
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("api", "services", "shared", "worker")
ACP_RUNS_SQL = re.compile(
    r"\b(FROM|JOIN|UPDATE|INTO)\s+(?:(?:acp_shared|shared)\.)?acp_runs\b", re.I
)


def _scan_text(text: str) -> list[tuple[int, str]]:
    """Return (1-based line, matched SQL) for every hit; matches may span lines."""
    return [
        (text.count("\n", 0, m.start()) + 1, " ".join(m.group(0).split()))
        for m in ACP_RUNS_SQL.finditer(text)
    ]


def _offenders() -> list[str]:
    hits = []
    for top in SCANNED_DIRS:
        base = REPO_ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, snippet in _scan_text(text):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {snippet}")
    return hits


def test_no_runtime_sql_against_acp_runs():
    hits = _offenders()
    assert not hits, (
        "acp_shared.acp_runs was dropped (migration 121); use shared.job instead. "
        "Offending lines:\n" + "\n".join(hits)
    )


def test_scanned_dirs_exist():
    # A rename of api/ or services/ would silently turn the guard into a no-op.
    for top in ("api", "services", "shared"):
        assert (REPO_ROOT / top).is_dir(), top


def test_pattern_catches_sql_and_ignores_prose():
    assert ACP_RUNS_SQL.search("SELECT run_id FROM acp_shared.acp_runs WHERE x")
    assert ACP_RUNS_SQL.search("update shared.acp_runs set status='failed'")
    assert ACP_RUNS_SQL.search("INSERT INTO acp_shared.acp_runs (run_id) VALUES ($1)")
    assert ACP_RUNS_SQL.search("JOIN acp_shared.acp_runs r ON r.run_id = p.run_id")
    assert not ACP_RUNS_SQL.search("# that table is FK'd to acp_shared.acp_runs(run_id)")
    assert not ACP_RUNS_SQL.search("FROM acp_shared.acp_v2_runs")
    assert not ACP_RUNS_SQL.search("0 rows ever durably written to shared.acp_runs either")
    assert ACP_RUNS_SQL.search("DELETE FROM acp_runs WHERE run_id = $1")
    assert not ACP_RUNS_SQL.search("FROM public_acp_runs")


def test_scan_catches_multiline_sql():
    # Triple-quoted SQL often breaks the line between the keyword and the table name.
    text = (
        "x = 1\n"
        'QUERY = """\n'
        "    SELECT run_id\n"
        "    FROM\n"
        "        acp_shared.acp_runs\n"
        "    WHERE status = 'running'\n"
        '"""\n'
    )
    assert _scan_text(text) == [(4, "FROM acp_shared.acp_runs")]
    assert _scan_text("# FK'd to acp_shared.acp_runs(run_id)\nFROM acp_shared.acp_v2_runs\n") == []

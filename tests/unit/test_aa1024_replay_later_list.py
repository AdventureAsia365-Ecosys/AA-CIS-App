"""AA-1024 l22 (S271 lessons guard G8): the old-migration replay tests know every later migration that builds on
what they drop.

tests/integration/test_aa1002_migration_304.py drops 304's tables (``_undo``), re-runs 304 and then re-runs the later
files in ``LATER``, so the modules after it see the live state. A new migration with an FK, CHECK, grant, column or row
on one of those tables that is not in ``LATER`` breaks that module, or the modules after it, in CI only (six red CI
rounds, lessons "Thêm FK trỏ vào một bảng có sẵn ..." S259-S270).

For each replay test in REPLAY_TESTS this test reads (AST, no import: the module needs psycopg2) the tables its undo
function drops, its LATER list and its NOT_LATER dict (file -> why it need not be re-run), and fails when a migration
numbered above the replayed one names a dropped table (or a watched column) and is in neither list. The author then
decides: add it to LATER (with the undo step it needs) or to NOT_LATER with the reason.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"

# replayed migration -> (module, undo function, extra watched patterns: regexes a later file may match instead of a
# dropped table name)
REPLAY_TESTS = {
    304: ("tests/integration/test_aa1002_migration_304.py", "_undo",
          (r"\bllm_model_catalog\b[\s\S]*\bcapabilities\b",)),   # _undo drops the catalog's capabilities column
}
DROP_RE = re.compile(r"\bDROP\s+TABLE\s+IF\s+EXISTS\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", re.I)
FILE_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def read_replay_module(path: Path, undo_name: str) -> tuple[set, list, dict]:
    """(tables the undo function drops, LATER file names, NOT_LATER {file: reason}) of one replay test module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dropped, later, not_later = set(), [], {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == undo_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    dropped |= {t.lower() for t in DROP_RE.findall(sub.value)}
        targets = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
        if "LATER" in targets:
            later = [c.value for c in ast.walk(node.value)
                     if isinstance(c, ast.Constant) and isinstance(c.value, str) and FILE_RE.match(c.value)]
        if "NOT_LATER" in targets and isinstance(node.value, ast.Dict):
            not_later = {k.value: v.value for k, v in zip(node.value.keys, node.value.values)
                         if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
    return dropped, later, not_later


def names_watched(sql: str, dropped: set, patterns: tuple) -> list[str]:
    low = sql.lower()
    hits = sorted(t for t in dropped if re.search(r"\b" + re.escape(t) + r"\b", low))
    hits += [p for p in patterns if re.search(p, low)]
    return hits


def later_problems(replayed: int, dropped: set, later: list, not_later: dict, migrations: dict,
                   patterns: tuple = ()) -> list[str]:
    """Problems for one replay test. ``migrations`` = {file name: SQL} of the owner's migrations."""
    out = []
    if not dropped:
        out.append(f"no DROP TABLE found in the undo function of the {replayed} replay test (renamed?)")
    for name in later + list(not_later):
        if name not in migrations:
            out.append(f"{name} is listed for the {replayed} replay but no such migration exists (renumbered?)")
    for name in sorted(set(later) & set(not_later)):
        out.append(f"{name} is in both LATER and NOT_LATER of the {replayed} replay")
    for name, reason in sorted(not_later.items()):
        if not str(reason).strip():
            out.append(f"{name}: NOT_LATER needs a reason")
    for name, sql in sorted(migrations.items()):
        m = FILE_RE.match(name)
        if not m or int(m.group(1)) <= replayed or name in later or name in not_later:
            continue
        hits = names_watched(sql, dropped, patterns)
        if hits:
            out.append(f"{name} names {', '.join(hits)}, which the {replayed} replay test drops and rebuilds: add it "
                       f"to LATER (re-run after the replay, with any undo step it needs) or to NOT_LATER with the "
                       f"reason it need not be re-run")
    return out


def _owner_migrations() -> dict:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql"))}


def test_replay_tests_list_every_later_migration_on_their_tables():
    migrations = _owner_migrations()
    problems = []
    for replayed, (module, undo, patterns) in REPLAY_TESTS.items():
        dropped, later, not_later = read_replay_module(REPO / module, undo)
        problems += [f"{module}: {p}" for p in later_problems(replayed, dropped, later, not_later, migrations,
                                                              patterns)]
    assert not problems, "\n".join(problems)


def test_the_304_module_is_read():
    dropped, later, not_later = read_replay_module(REPO / REPLAY_TESTS[304][0], "_undo")
    assert {"shared.llm_model_deployment", "shared.llm_provider_connection"} <= dropped
    assert "322_provider_connection_v2.sql" in later and "376_llm_anthropic_api_catalog_fallbacks.sql" in later
    assert "323_llm_contract_views.sql" in not_later


# ── the rule on fixtures (red without the guard) ──────────────────────────────────────────────────────────────────

FIXTURE = {
    "300_base.sql": "CREATE TABLE shared.widget (id int);",
    "304_widgets.sql": "CREATE TABLE shared.widget_part (id int REFERENCES shared.widget);",
    "305_widget_fk.sql": "ALTER TABLE shared.gizmo ADD CONSTRAINT g_fk FOREIGN KEY (p) REFERENCES shared.widget_part;",
    "306_unrelated.sql": "CREATE TABLE shared.sprocket (id int);",
    "307_widget_rows.sql": "INSERT INTO shared.widget_part VALUES (1);",
    "308_catalog_col.sql": "UPDATE shared.llm_model_catalog SET capabilities = '{}';",
}


def test_a_new_file_on_a_dropped_table_is_refused():
    got = later_problems(304, {"shared.widget_part"}, ["305_widget_fk.sql"], {}, FIXTURE)
    assert len(got) == 1 and got[0].startswith("307_widget_rows.sql names shared.widget_part")


def test_listed_files_pass_and_a_reason_is_required():
    assert later_problems(304, {"shared.widget_part"}, ["305_widget_fk.sql"],
                          {"307_widget_rows.sql": "rows only, the test deletes them"}, FIXTURE) == []
    got = later_problems(304, {"shared.widget_part"}, ["305_widget_fk.sql"], {"307_widget_rows.sql": " "}, FIXTURE)
    assert got == ["307_widget_rows.sql: NOT_LATER needs a reason"]


def test_stale_and_double_entries_are_refused():
    got = later_problems(304, {"shared.widget_part"}, ["305_widget_fk.sql", "309_gone.sql"],
                         {"305_widget_fk.sql": "x", "307_widget_rows.sql": "x"}, FIXTURE)
    assert any("309_gone.sql is listed" in g for g in got)
    assert any("305_widget_fk.sql is in both" in g for g in got)


def test_watched_column_pattern():
    got = later_problems(304, {"shared.widget_part"}, ["305_widget_fk.sql"], {"307_widget_rows.sql": "x"}, FIXTURE,
                         (r"\bllm_model_catalog\b[\s\S]*\bcapabilities\b",))
    assert len(got) == 1 and got[0].startswith("308_catalog_col.sql names")


def test_files_at_or_below_the_replayed_number_are_ignored():
    assert later_problems(304, {"shared.widget"}, [], {}, {"300_base.sql": FIXTURE["300_base.sql"],
                                                           "304_widgets.sql": FIXTURE["304_widgets.sql"]}) == []

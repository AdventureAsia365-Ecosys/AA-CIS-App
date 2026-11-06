"""AA-1024 l22 (S271 lessons guard G7): DB tests never order rows by a random key.

A test that reads rows ``ORDER BY id`` where ``id`` is a uuid gets them in random order: green locally, red in one CI
shard (S271 #1350; lessons "Script chọn bản mới nhất phải có ORDER BY khoá ổn định", S252). The same holds for
``ORDER BY created_at`` with nothing after it: rows written in one transaction share ``now()``.

The lint reads every SQL string literal in tests/integration/**/*.py (adjacent literals and f-string text
included) and fails on:

  uuid-order     the first ORDER BY key is ``id`` / ``<alias>.id`` of a table whose ``id`` is a uuid (the type comes
                 from this repo's migrations, or a CREATE TABLE in the test file itself);
  ts-only-order  ``ORDER BY [<alias>.]created_at [ASC|DESC]`` with no further key.

Fix: pick each row by a real key (a dict by name), compare ``sorted()`` lists, or order by a monotonic column with a
tiebreak (``ORDER BY created_at, seq``). A line that really means it carries ``order-ok: <reason>`` next to the string.
An ``ORDER BY id`` whose table cannot be resolved (several FROM tables, no alias) is listed, never failed.
Existing hits are in ALLOWED, which may only shrink.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"
TESTS = REPO / "tests" / "integration"     # DB tests; unit tests only assert on SQL text sent to mocks

# (test file relative to the repo root, rule) -> hits allowed today. Shrink only: fix a hit, lower its number.
ALLOWED: dict[tuple[str, str], int] = {
    ("tests/integration/test_aa1016_tenant_ai_budget_sql.py", "ts-only-order"): 1,
    ("tests/integration/test_aa1025_d3_h1a_db.py", "ts-only-order"): 1,
    ("tests/integration/test_aa1025_h2_capacity_db.py", "ts-only-order"): 1,
    ("tests/integration/test_aa1028_slice3.py", "ts-only-order"): 2,
    ("tests/integration/test_aa1035_s2_view_as_pg.py", "ts-only-order"): 1,
    ("tests/integration/test_aa1035_tenant_scope_pg.py", "ts-only-order"): 1,
    ("tests/integration/test_aa1042_route_trace_db.py", "ts-only-order"): 1,
    ("tests/integration/test_aa764_t7_t8_jobs.py", "ts-only-order"): 1,
    ("tests/integration/test_aa804_ai_gateway_sql.py", "ts-only-order"): 1,
    ("tests/integration/test_aa816_staff_directory_writes.py", "ts-only-order"): 3,
    ("tests/integration/test_aa819_daily_publish_limit.py", "ts-only-order"): 1,
    ("tests/integration/test_aa839_erasure_job.py", "ts-only-order"): 1,
    ("tests/integration/test_aa859_wave4_pool_routes.py", "ts-only-order"): 1,
    ("tests/integration/test_aa900_cassette_db.py", "ts-only-order"): 1,
    ("tests/integration/test_aa900_w0_config_fp_db.py", "ts-only-order"): 1,
    ("tests/integration/test_aa978_membership_service.py", "ts-only-order"): 1,
    ("tests/integration/test_aa979_membership_session.py", "ts-only-order"): 1,
    ("tests/integration/test_aa997_p15_hide_unhide_pg.py", "ts-only-order"): 1,
}

IDENT = r"[a-z_][a-z0-9_]*"
QNAME = rf"{IDENT}(?:\.{IDENT})?"
CREATE_RE = re.compile(rf"\bCREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({QNAME})\s*\(",
                       re.I)
ALTER_TYPE_RE = re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({QNAME})\s+ALTER\s+COLUMN\s+id\s+"
                           rf"(?:SET\s+DATA\s+)?TYPE\s+({IDENT})", re.I)
RENAME_RE = re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({QNAME})\s+RENAME\s+TO\s+({IDENT})", re.I)
ID_COL_RE = re.compile(rf"(?:^|,)\s*id\s+({IDENT})", re.I)
FROM_RE = re.compile(rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+({QNAME})(?:\s+(?:AS\s+)?({IDENT}))?", re.I)
ORDER_RE = re.compile(rf"\bORDER\s+BY\s+(?:({IDENT})\.)?({IDENT})(\s+(?:ASC|DESC))?(\s+NULLS\s+(?:FIRST|LAST))?(\s*,)?",
                      re.I)
KEYWORDS = {"where", "join", "on", "left", "right", "inner", "outer", "full", "cross", "order", "group", "limit",
            "using", "set", "values", "select", "returning", "natural", "lateral", "union", "having", "offset",
            "for", "default", "window", "except", "intersect", "fetch", "as", "and", "or"}
MARKER = "order-ok:"


def _paren_body(sql: str, start: int) -> str:
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[start + 1:i]
    return sql[start + 1:]


def _top_level(body: str) -> str:
    """The body with nested parentheses blanked, so ``id`` inside a CHECK or a type modifier is not a column."""
    out, depth = [], 0
    for ch in body:
        if ch == "(":
            depth += 1
        if depth == 0:
            out.append(ch)
        if ch == ")":
            depth -= 1
    return "".join(out)


def _norm(t: str) -> str:
    t = t.lower()
    if t == "uuid":
        return "uuid"
    if t in ("bigserial", "serial", "smallserial", "bigint", "integer", "int", "int4", "int8", "smallint"):
        return "int"
    return t


def id_types(sql_texts: list[str], types: dict | None = None) -> dict[str, str]:
    """{table (qualified as written, lower case): normalised id type} from CREATE TABLE / ALTER COLUMN id TYPE /
    RENAME TO, applied in order (a later file wins)."""
    types = dict(types or {})
    for sql in sql_texts:
        events = []
        for m in CREATE_RE.finditer(sql):
            col = ID_COL_RE.search(_top_level(_paren_body(sql, m.end() - 1)))
            events.append((m.start(), "create", m.group(1).lower(), _norm(col.group(1)) if col else None))
        for m in ALTER_TYPE_RE.finditer(sql):
            events.append((m.start(), "alter", m.group(1).lower(), _norm(m.group(2))))
        for m in RENAME_RE.finditer(sql):
            events.append((m.start(), "rename", m.group(1).lower(), m.group(2).lower()))
        for _, kind, table, val in sorted(events):
            if kind == "rename":
                if table in types:
                    schema = table.rsplit(".", 1)[0] + "." if "." in table else ""
                    types[schema + val] = types.pop(table)
            elif val is None:
                types.pop(table, None)
            else:
                types[table] = val
    return types


def _lookup(types: dict, table: str) -> str | None:
    table = table.lower()
    if table in types:
        return types[table]
    bare = [v for k, v in types.items() if k.rsplit(".", 1)[-1] == table.rsplit(".", 1)[-1]]
    return bare[0] if len(set(bare)) == 1 else None


def order_findings(sql: str, types: dict) -> list[tuple[str, str]]:
    """[(rule | 'unresolved', detail)] for one SQL string."""
    out = []
    for m in ORDER_RE.finditer(sql):
        qual, col, tiebreak = (m.group(1) or "").lower(), m.group(2).lower(), bool(m.group(5))
        stmt = sql[:m.start()]
        stmt = stmt[max(stmt.lower().rfind("select"), 0):] if "select" in stmt.lower() else stmt
        if col == "created_at" and not tiebreak:
            out.append(("ts-only-order", m.group(0).strip()))
            continue
        if col != "id":
            continue
        aliases, tables = {}, []
        for f in FROM_RE.finditer(stmt):
            t, alias = f.group(1).lower(), (f.group(2) or "").lower()
            tables.append(t)
            if stmt[f.end():].lstrip().startswith(","):
                tables.append("<comma join>")       # FROM a, b: the second table is not read here
            aliases[t.rsplit(".", 1)[-1]] = t
            if alias and alias not in KEYWORDS:
                aliases[alias] = t
        if qual:
            table = aliases.get(qual, qual)
        elif len(set(tables)) == 1:
            table = tables[0]
        else:
            out.append(("unresolved", m.group(0).strip()))
            continue
        if _lookup(types, table) == "uuid":
            out.append(("uuid-order", f"{m.group(0).strip()} on {table} (id is a uuid)"))
    return out


def _strings(tree: ast.AST):
    """(first line, last line, text) of each string literal; an f-string's text parts joined with a placeholder."""
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{x}"
                           for v in node.values)
            yield node.lineno, node.end_lineno, text
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.end_lineno, node.value


def scan_file(path: Path, base_types: dict) -> list[tuple[str, int, str]]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    literals = [(a, b, t) for a, b, t in _strings(tree) if "order by" in t.lower() or "create table" in t.lower()]
    types = id_types([t for _, _, t in literals if "create table" in t.lower()], base_types)
    out = []
    for first, last, text in literals:
        window = "\n".join(lines[first - 1:last + 1])
        if MARKER in window:
            continue
        for rule, detail in order_findings(text, types):
            out.append((rule, first, detail))
    return out


def migration_types() -> dict:
    return id_types([p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql"))])


def scan_tree() -> list[tuple[str, str, int, str]]:
    base = migration_types()
    out = []
    for path in sorted(TESTS.rglob("*.py")):
        for rule, line, detail in scan_file(path, base):
            out.append((str(path.relative_to(REPO)), rule, line, detail))
    return out


# ── the lint on this repo ──────────────────────────────────────────────────────────────────────────────────────────


def test_no_new_random_order_in_db_tests():
    hits = scan_tree()
    counts = Counter((f, r) for f, r, _, _ in hits if r != "unresolved")
    grown = {k: v for k, v in counts.items() if v > ALLOWED.get(k, 0)}
    detail = "\n".join(f"  {f}:{line}: [{r}] {d}" for f, r, line, d in hits if (f, r) in grown)
    assert not grown, ("ORDER BY on a random key in a DB test (pick rows by key, compare sorted(), or add a "
                       f"monotonic tiebreak; 'order-ok: <reason>' if intended):\n{detail}")


def test_allowlist_only_shrinks():
    counts = Counter((f, r) for f, r, _, _ in scan_tree() if r != "unresolved")
    stale = {k: (v, counts.get(k, 0)) for k, v in ALLOWED.items() if counts.get(k, 0) < v}
    assert not stale, f"ALLOWED is above the current count, lower it (allowed, now): {stale}"


def test_migration_types_are_read():
    types = migration_types()
    assert types.get("shared.decision_log") == "int"          # BIGSERIAL (181)
    assert types.get("gold_aa_internal.tour_lifecycle_event") == "int"
    assert sum(1 for v in types.values() if v == "uuid") > 20
    # The S271 #1350 flake, as it was written before the fix: shared.llm_call_log.id is a uuid.
    assert [r for r, _ in order_findings(
        "SELECT model, quality_signal FROM shared.llm_call_log WHERE stage = $1 ORDER BY id", types)] == ["uuid-order"]


# ── the rules on fixtures (red without the lint) ───────────────────────────────────────────────────────────────────

TYPES = id_types([
    "CREATE TABLE IF NOT EXISTS shared.widget_log (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text);",
    "CREATE TABLE shared.gadget_log (\n  id bigserial PRIMARY KEY,\n  note text CHECK (note <> 'id uuid')\n);",
    "CREATE TABLE shared.sprocket (id integer PRIMARY KEY); ALTER TABLE shared.sprocket ALTER COLUMN id TYPE uuid;",
    "CREATE TABLE shared.old_name (id uuid PRIMARY KEY); ALTER TABLE shared.old_name RENAME TO new_name;",
])


def test_fixture_types():
    assert TYPES == {"shared.widget_log": "uuid", "shared.gadget_log": "int", "shared.sprocket": "uuid",
                     "shared.new_name": "uuid"}


def test_uuid_id_order_is_found():
    assert [r for r, _ in order_findings("SELECT name FROM shared.widget_log WHERE x = $1 ORDER BY id", TYPES)] == \
        ["uuid-order"]
    assert [r for r, _ in order_findings("SELECT w.name FROM shared.widget_log w ORDER BY w.id DESC LIMIT 1",
                                         TYPES)] == ["uuid-order"]
    assert [r for r, _ in order_findings("SELECT 1 FROM shared.sprocket ORDER BY id, name", TYPES)] == ["uuid-order"]
    assert [r for r, _ in order_findings("SELECT 1 FROM shared.new_name ORDER BY id", TYPES)] == ["uuid-order"]


def test_int_ids_and_real_keys_pass():
    assert order_findings("SELECT note FROM shared.gadget_log ORDER BY id", TYPES) == []
    assert order_findings("SELECT name FROM shared.widget_log ORDER BY name", TYPES) == []
    assert order_findings("SELECT name FROM shared.widget_log ORDER BY created_at, name", TYPES) == []
    assert order_findings("SELECT g.note FROM shared.gadget_log g JOIN shared.widget_log w ON w.name = g.note "
                          "ORDER BY g.id", TYPES) == []


def test_created_at_alone_is_found():
    assert [r for r, _ in order_findings("SELECT 1 FROM shared.gadget_log ORDER BY created_at DESC", TYPES)] == \
        ["ts-only-order"]
    assert [r for r, _ in order_findings("SELECT 1 FROM shared.gadget_log g ORDER BY g.created_at", TYPES)] == \
        ["ts-only-order"]


def test_unresolved_is_listed_not_failed():
    assert [r for r, _ in order_findings("SELECT 1 FROM shared.widget_log, shared.gadget_log ORDER BY id", TYPES)] \
        == ["unresolved"]


def test_scan_reads_fstrings_adjacent_literals_and_the_marker(tmp_path: Path):
    f = tmp_path / "test_fixture_db.py"
    f.write_text(
        'Q1 = ("SELECT name FROM shared.widget_log "\n'
        '      "WHERE x = $1 ORDER BY id")\n'
        'T = "widget_log"\n'
        'Q2 = f"SELECT name FROM shared.{T} w ORDER BY w.id"\n'
        'Q3 = "SELECT name FROM shared.widget_log ORDER BY id"  # order-ok: a set compare follows\n'
        'Q4 = "CREATE TABLE shared.local_tbl (id uuid PRIMARY KEY)"\n'
        'Q5 = "SELECT 1 FROM shared.local_tbl ORDER BY id"\n'
    )
    got = [(rule, line) for rule, line, _ in scan_file(f, TYPES)]
    assert got == [("uuid-order", 1), ("uuid-order", 7)], got

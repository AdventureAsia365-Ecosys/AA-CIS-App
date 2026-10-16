"""Job table shape: render the DDL template and compare an owner's <schema>.job with it (AA-751, X6).

ADR 0003a decision 2: every owner keeps its own job table (CIS shared.job, Booking booking.job, a future
tripplanner.job) in the shape fixed by job_table_template.sql. The template is never applied across owners;
each owner's CI renders it into a scratch schema and compares its live table with the result through
pg_catalog:

    tmpl = await read_shape(conn, "x6_tmpl")       # after executing render(...) in that schema
    diffs = compare(tmpl, await read_shape(conn, "shared"))
    assert diffs == [], "\\n".join(diffs)

Compared: columns (type, NOT NULL, default), CHECK constraints (the status set parsed as a set; every
constraint validated), primary key, foreign keys, indexes by definition (a multiset), the BEFORE UPDATE
trigger (timing / level / event, normalised function body, function search_path) and reloptions (the
owner holds at least the template's). Not compared: names of constraints / indexes / the trigger and its
function, column order, grants, comments, ownership.

Stdlib only, no app imports: AA-Booking keeps a byte copy (backend/tests/job_template/), drift-checked
against this file by its CI. `conn` is an asyncpg connection (anything with fetch/fetchval/transaction).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("job_table_template.sql")

# Owner kind patterns (ADR 0003a decision 3). CIS kinds stay unprefixed and may not use another owner's prefix.
CIS_KIND_PATTERN = r"^(?!bk_|tp_)[a-z][a-z0-9_]*$"
BOOKING_KIND_PATTERN = r"^bk_[a-z0-9_]+$"
TRIPPLANNER_KIND_PATTERN = r"^tp_[a-z0-9_]+$"

PLACEHOLDERS = ("schema", "kind_pattern", "extra_statuses", "max_attempts_default")

# pg_trigger.tgtype bits (src/include/catalog/pg_trigger.h).
TRIGGER_TYPE_ROW = 1
TRIGGER_TYPE_BEFORE = 2
TRIGGER_TYPE_INSERT = 4
TRIGGER_TYPE_DELETE = 8
TRIGGER_TYPE_UPDATE = 16
TRIGGER_TYPE_TRUNCATE = 32
TRIGGER_TYPE_INSTEAD = 64
# pg_trigger.tgenabled values whose trigger fires in a normal (origin) session: O (origin/local), A (always).
TRIGGER_FIRES_IN_ORIGIN = ("O", "A")

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_EXTRA_STATUSES_RE = re.compile(r"^(\s*,\s*'[a-z_]+')*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_STATUS_CHECK_RE = re.compile(r"\bstatus\s*=\s*ANY\s*\(\s*ARRAY\[(?P<items>[^\]]*)\]")
_STATUS_ITEM_RE = re.compile(r"'((?:[^']|'')*)'")
_INDEX_RE = re.compile(r"^CREATE (?P<unique>UNIQUE )?INDEX \S+ ON (?:ONLY )?(?P<table>\S+) (?P<rest>USING .*)$", re.S)


# ── template ─────────────────────────────────────────────────────────────────────────────────────


def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render(
    template_text: str, *, schema: str, kind_pattern: str, extra_statuses: str, max_attempts_default: int
) -> str:
    """The template's statements for one owner: full-line comments dropped, every placeholder filled.

    Raises ValueError on an unsafe value (the result is executed as SQL) or a placeholder left unfilled.
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"schema must match {_SCHEMA_RE.pattern}: {schema!r}")
    if not kind_pattern or "'" in kind_pattern or "\x00" in kind_pattern:
        # A quote would end the SQL literal (standard_conforming_strings: a backslash is literal there).
        raise ValueError(f"kind_pattern must be non-empty and contain no quote: {kind_pattern!r}")
    if not _EXTRA_STATUSES_RE.match(extra_statuses):
        raise ValueError(f"extra_statuses must look like \", 'a', 'b'\" or be empty: {extra_statuses!r}")
    if isinstance(max_attempts_default, bool) or not isinstance(max_attempts_default, int) or max_attempts_default < 1:
        raise ValueError(f"max_attempts_default must be an int >= 1: {max_attempts_default!r}")
    values = {
        "schema": schema,
        "kind_pattern": kind_pattern,
        "extra_statuses": extra_statuses,
        "max_attempts_default": str(max_attempts_default),
    }

    body = "\n".join(line for line in template_text.splitlines() if not line.lstrip().startswith("--"))

    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            raise ValueError(f"unknown template placeholder {{{{{name}}}}}")
        return values[name]

    out = _PLACEHOLDER_RE.sub(fill, body)
    if "{{" in out or "}}" in out:
        raise ValueError("template placeholder left unfilled")
    return out.strip() + "\n"


def split_statements(sql: str) -> list[str]:
    """Top-level statements of SQL without comments: splits on ';' outside quotes and $$ bodies."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_quote = in_dollar = False
    while i < n:
        ch = sql[i]
        if in_dollar:
            if sql.startswith("$$", i):
                in_dollar = False
                buf.append("$$")
                i += 2
                continue
        elif in_quote:
            if ch == "'":
                in_quote = False
        elif ch == "'":
            in_quote = True
        elif sql.startswith("$$", i):
            in_dollar = True
            buf.append("$$")
            i += 2
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


# ── catalog read ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Trigger:
    tgtype: int
    enabled: str  # pg_trigger.tgenabled: O (origin), D (disabled), R (replica), A (always)
    conditional: bool  # has a WHEN clause
    column_list: bool  # UPDATE OF <columns>
    body: str  # pg_proc.prosrc
    config: tuple[str, ...]  # pg_proc.proconfig


@dataclass
class Shape:
    schema: str
    columns: dict[str, tuple[str, bool, str | None]]  # name -> (type, not null, default expression)
    constraints: list[tuple[str, bool, str]]  # (contype, validated, definition)
    indexes: list[str]  # pg_get_indexdef
    triggers: list[Trigger]
    reloptions: list[str] = field(default_factory=list)


_COLUMNS_SQL = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid) AS dflt
  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
"""
_CONSTRAINTS_SQL = """
SELECT contype::text, convalidated, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = $1::regclass
"""
_INDEXES_SQL = "SELECT pg_get_indexdef(indexrelid) AS def FROM pg_index WHERE indrelid = $1::regclass"
_TRIGGERS_SQL = """
SELECT t.tgtype::int AS tgtype, t.tgenabled::text AS enabled, t.tgqual IS NOT NULL AS conditional,
       coalesce(array_length(t.tgattr, 1), 0) > 0 AS column_list, p.prosrc, p.proconfig
  FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE t.tgrelid = $1::regclass AND NOT t.tgisinternal
"""
_RELOPTIONS_SQL = "SELECT coalesce(reloptions, '{}') FROM pg_class WHERE oid = $1::regclass"


async def read_shape(conn, schema: str) -> Shape:
    """<schema>.job as pg_catalog describes it; an empty Shape when the table does not exist (compare() then
    reports "table not found").

    Runs in a transaction (a savepoint when the caller already has one) with search_path pinned to
    pg_catalog, so every pg_get_*def qualifies the table whatever the role's own path (a role named like its
    schema has "$user" on it). On success the caller's search_path is restored before returning; on an error
    the savepoint rollback undoes the pin, and the original error propagates unmasked.
    """
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"schema must match {_SCHEMA_RE.pattern}: {schema!r}")
    table = f"{schema}.job"
    async with conn.transaction():
        old_path = await conn.fetchval("SELECT current_setting('search_path')")
        await conn.fetchval("SELECT set_config('search_path', 'pg_catalog', true)")
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            columns = {
                r["attname"]: (r["type"], r["attnotnull"], r["dflt"]) for r in await conn.fetch(_COLUMNS_SQL, table)
            }
            constraints = [
                (r["contype"], r["convalidated"], r["def"]) for r in await conn.fetch(_CONSTRAINTS_SQL, table)
            ]
            indexes = [r["def"] for r in await conn.fetch(_INDEXES_SQL, table)]
            triggers = [
                Trigger(
                    r["tgtype"],
                    r["enabled"],
                    r["conditional"],
                    r["column_list"],
                    r["prosrc"],
                    tuple(r["proconfig"] or ()),
                )
                for r in await conn.fetch(_TRIGGERS_SQL, table)
            ]
            reloptions = list(await conn.fetchval(_RELOPTIONS_SQL, table) or [])
        else:
            columns, constraints, indexes, triggers, reloptions = {}, [], [], [], []
        # Not in a `finally`: after a failed query the transaction is aborted and this would raise
        # InFailedSQLTransactionError over the real error. A rollback undoes the pin anyway.
        await conn.fetchval("SELECT set_config('search_path', $1, true)", old_path)
    return Shape(schema, columns, constraints, indexes, triggers, reloptions)


# ── normalisation ────────────────────────────────────────────────────────────────────────────────


def placeholder_table(text: str, schema: str) -> str:
    """Replaces the qualified <schema>.job in a deparsed definition with a fixed placeholder."""
    return re.sub(rf'(?<![\w."]){re.escape(schema)}\.job\b', "<schema>.job", text)


def normalise_body(body: str) -> str:
    """A plpgsql body with whitespace collapsed and the trailing ';' dropped: 'END $$;' (bk-001) and
    'END;\\n$$' (the template) compare equal."""
    return re.sub(r"\s+", " ", body or "").strip().rstrip("; \t\n")


def parse_status_set(definition: str) -> frozenset[str] | None:
    """The status values of a `status IN (...)` CHECK as deparsed (`status = ANY (ARRAY['a'::text, ...])`);
    None for any other CHECK."""
    m = _STATUS_CHECK_RE.search(definition)
    if not m:
        return None
    return frozenset(v.replace("''", "'") for v in _STATUS_ITEM_RE.findall(m.group("items")))


def normalise_index(definition: str, schema: str) -> str:
    """`CREATE [UNIQUE] INDEX <name> ON <schema>.job USING ...` -> `[UNIQUE ]USING ...` (name dropped)."""
    m = _INDEX_RE.match(definition.strip())
    if not m:
        return placeholder_table(definition, schema)
    return (m.group("unique") or "") + placeholder_table(m.group("rest"), schema)


def _trigger_key(t: Trigger) -> tuple:
    return (t.tgtype, t.conditional, t.column_list, normalise_body(t.body))


def describe_tgtype(tgtype: int) -> str:
    timing = "INSTEAD OF" if tgtype & TRIGGER_TYPE_INSTEAD else ("BEFORE" if tgtype & TRIGGER_TYPE_BEFORE else "AFTER")
    events = [
        name
        for bit, name in (
            (TRIGGER_TYPE_INSERT, "INSERT"),
            (TRIGGER_TYPE_UPDATE, "UPDATE"),
            (TRIGGER_TYPE_DELETE, "DELETE"),
            (TRIGGER_TYPE_TRUNCATE, "TRUNCATE"),
        )
        if tgtype & bit
    ]
    level = "ROW" if tgtype & TRIGGER_TYPE_ROW else "STATEMENT"
    return f"{timing} {' OR '.join(events) or '?'} FOR EACH {level}"


# ── comparison ───────────────────────────────────────────────────────────────────────────────────


def _multiset_diff(label: str, want: list[str], have: list[str], allowed_extra: tuple[str, ...] = ()) -> list[str]:
    diffs = []
    missing = Counter(want) - Counter(have)
    extra = Counter(have) - Counter(want)
    for item in sorted(missing.elements()):
        diffs.append(f"{label} missing: {item}")
    allowed = Counter(allowed_extra)
    for item in sorted(extra.elements()):
        if allowed[item] > 0:
            allowed[item] -= 1
            continue
        diffs.append(f"{label} not in template: {item}")
    return diffs


def compare(
    template: Shape,
    owner: Shape,
    *,
    allow_extra_indexes: tuple[str, ...] = (),
    allow_extra_columns: tuple[str, ...] = (),
) -> list[str]:
    """Human-readable differences between an owner's job table and the rendered template; [] = conforms.

    allow_extra_indexes: normalised definitions (`[UNIQUE ]USING btree (...) WHERE ...`, table written
    `<schema>.job`) of owner-only indexes, e.g. Booking's singleton index on bk_inquiry_pull.
    allow_extra_columns: owner-only column names.
    """
    t, o = template, owner
    tname, oname = f"{t.schema}.job", f"{o.schema}.job"
    diffs: list[str] = []
    if not o.columns:
        return [f"{oname}: table not found or has no columns"]

    # columns
    for name, (typ, notnull, dflt) in sorted(t.columns.items()):
        if name not in o.columns:
            diffs.append(f"column missing: {name} {typ}")
            continue
        otyp, onotnull, odflt = o.columns[name]
        if otyp != typ:
            diffs.append(f"column {name}: type {otyp}, template {typ}")
        if onotnull != notnull:
            diffs.append(
                f"column {name}: {'NOT NULL' if onotnull else 'nullable'}, "
                f"template {'NOT NULL' if notnull else 'nullable'}"
            )
        if (odflt or None) != (dflt or None):
            diffs.append(f"column {name}: default {odflt!r}, template {dflt!r}")
    for name in sorted(set(o.columns) - set(t.columns) - set(allow_extra_columns)):
        diffs.append(f"column not in template: {name} {o.columns[name][0]}")

    # constraints: every one validated; the status CHECK compared as a set, the rest by definition
    for _contype, validated, definition in o.constraints:
        if not validated:
            diffs.append(f"constraint not validated (NOT VALID): {placeholder_table(definition, o.schema)}")

    def split(shape: Shape) -> tuple[dict[str, list[str]], list[frozenset[str]]]:
        by_type: dict[str, list[str]] = {}
        statuses: list[frozenset[str]] = []
        for contype, _validated, definition in shape.constraints:
            if contype == "c":
                status_set = parse_status_set(definition)
                if status_set is not None:
                    statuses.append(status_set)
                    continue
            by_type.setdefault(contype, []).append(placeholder_table(definition, shape.schema))
        return by_type, statuses

    t_by, t_status = split(t)
    o_by, o_status = split(o)
    labels = {
        "c": "CHECK",
        "f": "FOREIGN KEY",
        "p": "PRIMARY KEY",
        "u": "UNIQUE",
        "x": "EXCLUDE",
        "t": "CONSTRAINT TRIGGER",
        "n": "NOT NULL",
    }
    for contype in sorted(set(t_by) | set(o_by)):
        diffs += _multiset_diff(
            labels.get(contype, f"constraint {contype}"), t_by.get(contype, []), o_by.get(contype, [])
        )
    if len(o_status) != 1:
        diffs.append(f"status CHECK: expected exactly one, found {len(o_status)}")
    elif t_status and o_status[0] != t_status[0]:
        missing = sorted(t_status[0] - o_status[0])
        extra = sorted(o_status[0] - t_status[0])
        diffs.append(f"status CHECK: missing {missing}, not in template {extra}")

    # indexes, by definition
    diffs += _multiset_diff(
        "index",
        [normalise_index(d, t.schema) for d in t.indexes],
        [normalise_index(d, o.schema) for d in o.indexes],
        tuple(allow_extra_indexes),
    )

    # trigger(s)
    t_keys = Counter(_trigger_key(x) for x in t.triggers)
    o_keys = Counter(_trigger_key(x) for x in o.triggers)
    for key in sorted((t_keys - o_keys).elements()):
        diffs.append(f"trigger missing: {describe_tgtype(key[0])} with body {key[3]!r}")
    for key in sorted((o_keys - t_keys).elements()):
        diffs.append(f"trigger not in template: {describe_tgtype(key[0])} with body {key[3]!r}")
    for ot in o.triggers:
        if ot.enabled not in TRIGGER_FIRES_IN_ORIGIN:
            # D (disabled) or R (ENABLE REPLICA: fires only under session_replication_role = replica).
            diffs.append(
                f"trigger not firing in origin sessions (tgenabled={ot.enabled}): {describe_tgtype(ot.tgtype)}"
            )
        for tt in t.triggers:
            if _trigger_key(tt) == _trigger_key(ot):
                missing_cfg = sorted(set(tt.config) - set(ot.config))
                if missing_cfg:
                    diffs.append(f"trigger function config missing {missing_cfg} (has {list(ot.config)})")

    # reloptions: the owner holds at least the template's
    for opt in sorted(set(t.reloptions) - set(o.reloptions)):
        diffs.append(f"reloption missing: {opt} (has {sorted(o.reloptions)})")

    return [f"{oname} vs {tname} template: {d}" for d in diffs]

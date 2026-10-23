"""AA-839 — erasure planner: which rows an erasure touches, in what order, and what blocks it.

Pure part (unit-tested): `plan_tenant(registry, catalog)` turns the inventory plus the live catalog (tenant_id
columns, foreign keys) into a TenantPlan — one Node per table with its action and WHERE predicate, the blocker
checks, the delete order (children first) and the FK columns to null first when two tables point at each other.
DB part: `load_catalog`, `count_*` (DRY RUN; SELECT only) and `apply_*` (one transaction; every statement's row
count must equal the approved DRY RUN, then a recount must find 0 rows, or it raises and nothing is committed).

Predicates are SQL templates over a table alias `{t}`; `$1` is always the subject (tenant id as text, or the
traveller's sha256 hex). Identifiers come from the catalog or the reviewed inventory and are always quoted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.erasure.inventory import (
    CIS_SCHEMAS, EMAIL_TOKEN_RE, TRAVELLER_EXACT, TRAVELLER_TEXT, Registry, TenantTable, TravellerScan,
)

MAX_DEPTH = 8          # FK hops from a tenant table; deeper is a blocker (never seen: raw_tours -> atoms -> members)
MAX_IDS = 200          # traveller matches: ids printed per scan
TEXT_TYPES = ("text", "character varying", "character")


class PlanError(Exception):
    """The catalog has a shape the planner does not handle (reported as a blocker, never guessed)."""


def qname(table: str) -> str:
    schema, _, name = table.partition(".")
    return f'"{schema.replace(chr(34), chr(34) * 2)}"."{name.replace(chr(34), chr(34) * 2)}"'


def qcol(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


# ── catalog ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Edge:
    conname: str
    child: str
    parent: str
    child_cols: tuple[str, ...]
    parent_cols: tuple[str, ...]
    child_nullable: bool          # every child column is nullable
    deltype: str                  # pg_constraint.confdeltype: a no action, r restrict, c cascade, n set null, d default


@dataclass
class Catalog:
    tables: set[str]                                  # every base table in the CIS schemas
    tenant_columns: dict[str, str]                    # table -> format_type of its tenant_id column
    edges: list[Edge] = field(default_factory=list)   # FKs whose parent is a CIS table
    # table -> its tenant-key columns (_TENANT_KEYS_SQL: tenant_id, tenant, owner_scope, or a "tenant" soft key)
    tenant_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def key_tables(self) -> set[str]:
        """Tables that hold a tenant key: each needs an inventory entry."""
        return set(self.tenant_columns) | set(self.tenant_keys)


_TABLES_SQL = """
    SELECT n.nspname || '.' || c.relname AS tbl
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY($1::text[])
"""
_TENANT_COLUMNS_SQL = """
    SELECT n.nspname || '.' || c.relname AS tbl, format_type(a.atttypid, a.atttypmod) AS typ
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE a.attname = 'tenant_id' AND a.attnum > 0 AND NOT a.attisdropped
       AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY($1::text[])
"""
# AA-839 review: tenant data is not always keyed by a column named tenant_id (tour_atoms.owner_scope,
# pipeline_jobs.tenant, atomize_day_fingerprint.tenant_tour_version_id). A tenant key is tenant_id / tenant /
# owner_scope, or any column whose name contains "tenant" that no foreign key covers (an FK-covered one is found
# by the FK walk instead).
_TENANT_KEYS_SQL = """
    SELECT n.nspname || '.' || c.relname AS tbl, array_agg(a.attname::text ORDER BY a.attnum) AS cols
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE a.attnum > 0 AND NOT a.attisdropped
       AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY($1::text[])
       AND (a.attname IN ('tenant_id', 'tenant', 'owner_scope')
            OR (a.attname LIKE '%tenant%' AND NOT EXISTS (
                    SELECT 1 FROM pg_constraint k
                     WHERE k.contype = 'f' AND k.conrelid = c.oid AND a.attnum = ANY(k.conkey))))
     GROUP BY 1
"""
_FK_SQL = """
    SELECT c.conname, c.confdeltype::text AS deltype,
           cn.nspname || '.' || cl.relname AS child, pn.nspname || '.' || pl.relname AS parent,
           array_agg(ca.attname::text ORDER BY k.ord) AS child_cols,
           array_agg(pa.attname::text ORDER BY k.ord) AS parent_cols,
           bool_and(NOT ca.attnotnull) AS child_nullable
      FROM pg_constraint c
      JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_namespace cn ON cn.oid = cl.relnamespace
      JOIN pg_class pl ON pl.oid = c.confrelid JOIN pg_namespace pn ON pn.oid = pl.relnamespace
      CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(ck, pk, ord)
      JOIN pg_attribute ca ON ca.attrelid = c.conrelid AND ca.attnum = k.ck
      JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = k.pk
     WHERE c.contype = 'f' AND pn.nspname = ANY($1::text[]) AND NOT cl.relispartition
     GROUP BY c.conname, c.confdeltype, cn.nspname, cl.relname, pn.nspname, pl.relname
     ORDER BY 1, 3
"""


async def load_catalog(conn, schemas: Iterable[str] = CIS_SCHEMAS) -> Catalog:
    schemas = list(schemas)
    tables = {r["tbl"] for r in await conn.fetch(_TABLES_SQL, schemas)}
    cols = {r["tbl"]: r["typ"] for r in await conn.fetch(_TENANT_COLUMNS_SQL, schemas)}
    edges = [Edge(r["conname"], r["child"], r["parent"], tuple(r["child_cols"]), tuple(r["parent_cols"]),
                  bool(r["child_nullable"]), r["deltype"]) for r in await conn.fetch(_FK_SQL, schemas)]
    keys = {r["tbl"]: tuple(r["cols"]) for r in await conn.fetch(_TENANT_KEYS_SQL, schemas)}
    return Catalog(tables, cols, edges, keys)


# ── tenant plan (pure) ──────────────────────────────────────────────────────────────────────────

@dataclass
class Node:
    table: str
    action: str                       # delete | detach | merge | keep | cascade
    reason: str
    template: Optional[str] = None    # registry predicate over {t}; None for a cascade node
    via: list[Edge] = field(default_factory=list)   # cascade: the FKs that reach it
    null_also: tuple[str, ...] = ()
    soft_parents: tuple[str, ...] = ()  # tables the predicate reads: deleted after this one (where_any needs)

    @property
    def deletes(self) -> bool:
        return self.action in ("delete", "cascade")


@dataclass
class Check:
    """A count that must be 0 before an apply (blocker), or is reported for information."""
    kind: str                         # cross_tenant | kept_row_reference | foreign_owner | live_jobs | ...
    table: str
    detail: str
    sql: Optional[str]                # SELECT count(*) ...; None = a static finding (always blocks)


@dataclass
class NullFirst:
    edge: Edge                        # child cols set to NULL for rows in the erasure, before the deletes


@dataclass
class TenantPlan:
    nodes: dict[str, Node]
    checks: list[Check]
    order: list[str]                  # delete order: children first
    null_first: list[NullFirst]
    unregistered: list[str]           # CIS tables with tenant_id and no registry entry
    missing: list[str]                # registry tables absent from this database (skipped)


def _default_template(typ: str) -> str:
    return "{t}.tenant_id = $1::uuid" if typ == "uuid" else "{t}.tenant_id::text = $1::text"


def predicate(plan_nodes: dict[str, Node], table: str, alias: str, depth: int = 0,
              _seen: Optional[frozenset] = None) -> str:
    """WHERE clause selecting `table`'s rows in the erasure, over `alias`."""
    node = plan_nodes[table]
    if node.template is not None:
        return "(" + node.template.format(t=alias) + ")"
    seen = (_seen or frozenset()) | {table}
    parts = []
    for i, e in enumerate(node.via):
        if e.parent in seen:
            continue          # a cycle among cascade tables; the other incoming path covers it
        pa = f"p{depth}_{i}"
        lhs = ", ".join(f"{alias}.{qcol(c)}" for c in e.child_cols)
        rhs = ", ".join(f"{pa}.{qcol(c)}" for c in e.parent_cols)
        inner = predicate(plan_nodes, e.parent, pa, depth + 1, seen)
        parts.append(f"(({lhs}) IN (SELECT {rhs} FROM {qname(e.parent)} {pa} WHERE {inner}))")
    if not parts:
        return "(false)"
    return "(" + " OR ".join(parts) + ")"


def _same_tenant_link(e: Edge, child: Node, parent: Node) -> bool:
    """A tenant_id -> tenant_id FK between two default-predicate tables selects the same tenant on both sides."""
    return (e.child_cols == ("tenant_id",) and e.parent_cols == ("tenant_id",)
            and child.template is not None and parent.template is not None
            and "tenant_id" in child.template and "tenant_id" in parent.template
            and child.template.startswith("{t}.tenant_id") and parent.template.startswith("{t}.tenant_id"))


def _registry_template(t: TenantTable, catalog: Catalog) -> tuple[Optional[str], tuple[str, ...]]:
    """(predicate template, soft parents) of a registry entry on this database; (None, ()) = cannot plan it."""
    if t.where_any:
        used = [(tpl, needs) for tpl, needs in t.where_any if all(n in catalog.tables for n in needs)]
        if not used:
            return None, ()
        template = " OR ".join(f"({tpl})" for tpl, _ in used) if len(used) > 1 else used[0][0]
        soft = tuple(sorted({n for _, needs in used for n in needs if n != t.table}))
        return template, soft
    if t.where is not None:
        return t.where, ()
    typ = catalog.tenant_columns.get(t.table)
    if typ is None:
        return None, ()               # registry says tenant_id, the DB has none: report, do not guess
    return _default_template(typ), ()


def plan_tenant(registry: Registry, catalog: Catalog) -> TenantPlan:
    by_name = registry.by_name()
    nodes: dict[str, Node] = {}
    missing: list[str] = []
    for t in registry.tenant_tables:
        if t.table not in catalog.tables:
            missing.append(t.table)
            continue
        template, soft = _registry_template(t, catalog)
        if template is None:
            missing.append(t.table)
            continue
        nodes[t.table] = Node(t.table, t.action, t.reason, template, null_also=t.null_also, soft_parents=soft)
    key_tables = catalog.key_tables()
    unregistered = sorted(tb for tb in key_tables if tb not in by_name)
    checks: list[Check] = []

    # BFS over FKs from every deleted table: children with no tenant_id become cascade nodes.
    by_parent: dict[str, list[Edge]] = {}
    for e in catalog.edges:
        by_parent.setdefault(e.parent, []).append(e)
    frontier = [(tb, 0) for tb, n in nodes.items() if n.deletes]
    visited_edges: set[str] = set()
    pending_checks: list[tuple[str, Edge]] = []
    while frontier:
        parent, depth = frontier.pop(0)
        for e in by_parent.get(parent, []):
            key = f"{e.conname}@{e.child}"
            if key in visited_edges or e.child == e.parent:
                continue
            visited_edges.add(key)
            schema = e.child.split(".", 1)[0]
            if schema not in CIS_SCHEMAS:
                pending_checks.append(("foreign_owner", e))
                continue
            child = nodes.get(e.child)
            if child is not None:
                if child.action == "cascade":
                    child.via.append(e)
                    continue
                pending_checks.append(("registered_child", e))
                continue
            if e.child in key_tables:
                continue                  # unregistered tenant table: already a blocker via `unregistered`
            if depth + 1 > MAX_DEPTH:
                checks.append(Check("too_deep", e.child, f"{e.conname}: more than {MAX_DEPTH} FK hops", None))
                continue
            nodes[e.child] = Node(e.child, "cascade", f"reached through {e.conname} from {e.parent}", via=[e])
            frontier.append((e.child, depth + 1))

    for kind, e in pending_checks:
        parent_pred = predicate(nodes, e.parent, "p")
        cols = ", ".join(f"c.{qcol(c)}" for c in e.child_cols)
        pcols = ", ".join(f"p.{qcol(c)}" for c in e.parent_cols)
        refs = f"({cols}) IN (SELECT {pcols} FROM {qname(e.parent)} p WHERE {parent_pred})"
        if kind == "foreign_owner":
            checks.append(Check("foreign_owner", e.child, f"{e.conname}: another owner's rows point at erased rows",
                                f"SELECT count(*) FROM {qname(e.child)} c WHERE {refs}"))
            continue
        child = nodes[e.child]
        parent = nodes[e.parent]
        if child.action == "delete":
            if _same_tenant_link(e, child, parent):
                continue
            child_pred = predicate(nodes, e.child, "c")
            checks.append(Check("cross_tenant", e.child,
                                f"{e.conname}: rows outside the erasure point at erased {e.parent} rows",
                                f"SELECT count(*) FROM {qname(e.child)} c WHERE {refs} "
                                f"AND NOT coalesce({child_pred}, false)"))
        elif child.action in ("detach", "merge") and e.child_cols == ("tenant_id",):
            continue                      # the tenant link is nulled / merged away before the deletes
        elif e.deltype == "n":
            continue                      # ON DELETE SET NULL: the kept row loses the link, as a detach would
        else:
            checks.append(Check("kept_row_reference", e.child,
                                f"{e.conname}: a {child.action} row points at erased {e.parent} rows",
                                f"SELECT count(*) FROM {qname(e.child)} c WHERE {refs}"))

    # Self-referencing FKs (shared.job.parent_job_id, tenant_tour_versions.parent_version_id): a row outside the
    # erasure that points at an erased row of the same table would make the apply fail (NO ACTION / RESTRICT) or
    # lose / delete it silently (SET NULL / CASCADE). Counted like a cross-tenant reference, whatever the action.
    # coalesce: a row whose predicate is NULL (e.g. a child job without payload tenant_id) is outside the erasure.
    for e in catalog.edges:
        if e.child != e.parent or e.child not in nodes or not nodes[e.child].deletes:
            continue
        cols = ", ".join(f"c.{qcol(c)}" for c in e.child_cols)
        pcols = ", ".join(f"p.{qcol(c)}" for c in e.parent_cols)
        checks.append(Check(
            "self_reference", e.child, f"{e.conname}: rows outside the erasure point at erased rows of the same table",
            f"SELECT count(*) FROM {qname(e.child)} c WHERE ({cols}) IN (SELECT {pcols} FROM {qname(e.parent)} p "
            f"WHERE {predicate(nodes, e.parent, 'p')}) AND NOT coalesce({predicate(nodes, e.child, 'c')}, false)"))

    order, null_first, cycle_error = _delete_order(nodes, catalog.edges)
    if cycle_error:
        checks.append(Check("unsupported_cycle", cycle_error, "an FK cycle with no nullable column", None))
    for tb in unregistered:
        checks.append(Check("unregistered", tb, "tenant_id column without an inventory entry", None))
    return TenantPlan(nodes, checks, order, null_first, unregistered, sorted(missing))


def _delete_order(nodes: dict[str, Node], edges: list[Edge]) -> tuple[list[str], list[NullFirst], Optional[str]]:
    """Children before parents among the deleted tables; a cycle is broken by nulling a nullable FK first."""
    deleted = {tb for tb, n in nodes.items() if n.deletes}
    # a soft parent is read by the child's predicate, so it must outlive the child's DELETE (never nulled)
    soft = {Edge(f"soft:{tb}->{sp}", tb, sp, (), (), False, "a")
            for tb, n in nodes.items() if n.deletes for sp in n.soft_parents if sp in deleted}
    live = sorted({e for e in edges if e.child in deleted and e.parent in deleted and e.child != e.parent} | soft,
                  key=lambda e: (e.conname, e.child))
    remaining = set(deleted)
    order: list[str] = []
    null_first: list[NullFirst] = []
    while remaining:
        # a table can go when no remaining table still points at it
        ready = sorted(tb for tb in remaining
                       if not any(e.parent == tb and e.child in remaining and e.child != tb for e in live))
        if ready:
            order.extend(ready)
            remaining -= set(ready)
            continue
        stuck = [e for e in live if e.child in remaining and e.parent in remaining and e.child_nullable]
        if not stuck:
            return order + sorted(remaining), null_first, ",".join(sorted(remaining))
        e = stuck[0]
        null_first.append(NullFirst(e))
        live.remove(e)
    return order, null_first, None


# ── DRY RUN counts / apply (DB) ─────────────────────────────────────────────────────────────────

def fingerprint(subject: str, rows: list[dict]) -> str:
    """sha256 of the subject kind and the (table, action, rows) list: what an approval names."""
    key = sorted((r["table"], r["action"], int(r["rows"])) for r in rows)
    return hashlib.sha256(json.dumps({"subject": subject, "rows": key}).encode()).hexdigest()


_PRIV = {"delete": ("DELETE",), "cascade": ("DELETE",), "detach": ("UPDATE",),
         "merge": ("INSERT", "UPDATE", "DELETE"), "keep": ()}


async def count_tenant(conn, plan: TenantPlan, tenant_id: str) -> dict:
    """SELECT only. Rows per node, blocker counts, missing privileges."""
    rows = []
    for tb in sorted(plan.nodes):
        node = plan.nodes[tb]
        n = await conn.fetchval(f"SELECT count(*) FROM {qname(tb)} t WHERE {predicate(plan.nodes, tb, 't')}",
                                tenant_id)
        rows.append({"table": tb, "action": node.action, "rows": int(n), "reason": node.reason,
                     **({"via": sorted({e.conname for e in node.via})} if node.via else {})})
    for nf in plan.null_first:
        e = nf.edge
        n = await conn.fetchval(_null_first_count_sql(plan, e), tenant_id)
        rows.append({"table": e.child, "action": f"null_first:{','.join(e.child_cols)}", "rows": int(n),
                     "reason": f"breaks the FK cycle {e.conname} before the deletes"})
    blockers, info = [], []
    for c in plan.checks:
        if c.sql is None:
            blockers.append({"kind": c.kind, "table": c.table, "detail": c.detail, "rows": None})
            continue
        try:
            # a savepoint: a failed statement would otherwise abort the whole (repeatable-read) transaction and
            # every later check would fail as "not readable" too (AA-839 review)
            async with conn.transaction():
                n = int(await conn.fetchval(c.sql, tenant_id))
        except Exception as e:  # e.g. no privilege on another owner's table
            blockers.append({"kind": c.kind, "table": c.table, "detail": f"{c.detail} (not readable: "
                             f"{type(e).__name__})", "rows": None})
            continue
        (blockers if n else info).append({"kind": c.kind, "table": c.table, "detail": c.detail, "rows": n})
    live_jobs = int(await conn.fetchval(
        "SELECT count(*) FROM shared.job WHERE payload ->> 'tenant_id' = $1 AND status IN ('queued', 'running')",
        tenant_id))
    if live_jobs:
        blockers.append({"kind": "live_jobs", "table": "shared.job", "rows": live_jobs,
                         "detail": "queued or running jobs of the tenant: wait for them or cancel them"})
    for r in rows:
        need = _PRIV.get(r["action"], ("UPDATE",))
        if r["rows"] and need:
            ok = await conn.fetchval("SELECT bool_and(has_table_privilege(current_user, $1, p)) "
                                     "FROM unnest($2::text[]) p", qname(r["table"]), list(need))
            if not ok:
                blockers.append({"kind": "privilege", "table": r["table"], "rows": r["rows"],
                                 "detail": f"{'/'.join(need)} not granted to the runtime role"})
    return {"rows": rows, "blockers": blockers, "checks_passed": info}


def _null_first_count_sql(plan: TenantPlan, e: Edge) -> str:
    return f"SELECT count(*) FROM {qname(e.child)} t WHERE {_null_first_where(plan, e)}"


def _null_first_where(plan: TenantPlan, e: Edge) -> str:
    cols = ", ".join(f"t.{qcol(c)}" for c in e.child_cols)
    pcols = ", ".join(f"p.{qcol(c)}" for c in e.parent_cols)
    return (f"{predicate(plan.nodes, e.child, 't')} AND ({cols}) IN "
            f"(SELECT {pcols} FROM {qname(e.parent)} p WHERE {predicate(plan.nodes, e.parent, 'p')})")


class CountMismatch(Exception):
    """A statement of the apply touched a different number of rows than the approved DRY RUN."""


def _affected(status: str) -> int:
    """asyncpg execute() status ('DELETE 3', 'UPDATE 2', 'INSERT 0 4') -> row count."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return -1


async def apply_tenant(conn, plan: TenantPlan, tenant_id: str, expected: dict[tuple[str, str], int],
                       merge_sql: dict[str, str]) -> list[dict]:
    """Inside the caller's transaction: null-first, detach, merge, then deletes children-first. Raises
    CountMismatch on any difference with `expected` ((table, action) -> rows); the caller rolls back."""
    done: list[dict] = []

    def check(table: str, action: str, got: int):
        want = expected.get((table, action), 0)
        if got != want:
            raise CountMismatch(f"{action} {table}: {got} rows, the approved DRY RUN had {want}")
        done.append({"table": table, "action": action, "rows": got})

    for nf in plan.null_first:
        e = nf.edge
        sets = ", ".join(f"{qcol(c)} = NULL" for c in e.child_cols)
        st = await conn.execute(f"UPDATE {qname(e.child)} t SET {sets} WHERE {_null_first_where(plan, e)}",
                                tenant_id)
        check(e.child, f"null_first:{','.join(e.child_cols)}", _affected(st))
    for tb in sorted(plan.nodes):
        node = plan.nodes[tb]
        if node.action == "detach":
            sets = ", ".join(f"{qcol(c)} = NULL" for c in ("tenant_id",) + node.null_also)
            st = await conn.execute(f"UPDATE {qname(tb)} t SET {sets} WHERE {predicate(plan.nodes, tb, 't')}",
                                    tenant_id)
            check(tb, "detach", _affected(st))
        elif node.action == "merge":
            await conn.execute(merge_sql[tb], tenant_id)
            st = await conn.execute(f"DELETE FROM {qname(tb)} t WHERE {predicate(plan.nodes, tb, 't')}", tenant_id)
            check(tb, "merge", _affected(st))
    for tb in plan.order:
        node = plan.nodes[tb]
        st = await conn.execute(f"DELETE FROM {qname(tb)} t WHERE {predicate(plan.nodes, tb, 't')}", tenant_id)
        check(tb, node.action, _affected(st))
    # verification inside the transaction (the caller re-verifies from a new connection after commit)
    for tb, node in plan.nodes.items():
        if node.action in ("delete", "detach", "merge"):
            left = await conn.fetchval(f"SELECT count(*) FROM {qname(tb)} t WHERE {predicate(plan.nodes, tb, 't')}",
                                       tenant_id)
            if left:
                raise CountMismatch(f"{tb}: {left} rows of the tenant left after the {node.action}")
    return done


async def verify_tenant(conn, plan: TenantPlan, tenant_id: str) -> dict[str, int]:
    """Rows still selected by each registry node's own predicate (cascade nodes need their deleted parents)."""
    out = {}
    for tb, node in plan.nodes.items():
        if node.action in ("delete", "detach", "merge"):
            out[tb] = int(await conn.fetchval(
                f"SELECT count(*) FROM {qname(tb)} t WHERE {predicate(plan.nodes, tb, 't')}", tenant_id))
    return out


# ── traveller (DSAR) ────────────────────────────────────────────────────────────────────────────

def traveller_where(scan: TravellerScan) -> str:
    """Rows whose column holds the traveller's address ($1 = sha256 hex of the trimmed, lowercased address)."""
    if scan.exact:
        return (f"{scan.column} IS NOT NULL AND "
                f"encode(sha256(convert_to(lower(btrim({scan.column})), 'UTF8')), 'hex') = $1")
    return (f"{scan.column} LIKE '%@%' AND EXISTS (SELECT 1 FROM regexp_matches({scan.column}, "
            f"'{EMAIL_TOKEN_RE}', 'g') AS m(tok) "
            f"WHERE encode(sha256(convert_to(lower((m.tok)[1]), 'UTF8')), 'hex') = $1)")


def traveller_scans() -> tuple[TravellerScan, ...]:
    return TRAVELLER_EXACT + TRAVELLER_TEXT


def scan_key(scan: TravellerScan) -> str:
    return f"{scan.table}:{scan.column.removeprefix('t.').split('::')[0]}"


async def count_traveller(conn, email_sha256: str, tables: set[str]) -> dict:
    rows, skipped = [], []
    for scan in traveller_scans():
        if scan.table not in tables:
            skipped.append(scan.table)
            continue
        ids = await conn.fetch(f"SELECT t.{qcol(scan.id_col)}::text AS id FROM {qname(scan.table)} t "
                               f"WHERE {traveller_where(scan)} ORDER BY 1 LIMIT {MAX_IDS + 1}", email_sha256)
        n = len(ids) if len(ids) <= MAX_IDS else int(await conn.fetchval(
            f"SELECT count(*) FROM {qname(scan.table)} t WHERE {traveller_where(scan)}", email_sha256))
        rows.append({"table": scan_key(scan), "action": scan.action, "rows": n, "reason": scan.reason,
                     "ids": [r["id"] for r in ids[:MAX_IDS]]})
    return {"rows": rows, "skipped": sorted(set(skipped))}


async def apply_traveller(conn, email_sha256: str, expected: dict[tuple[str, str], int],
                          protect_job_ids: Iterable[str] = ()) -> list[dict]:
    """Deletes / redacts the matched rows (manual ones are left for a person). Raises CountMismatch."""
    done = []
    protect = list(protect_job_ids)
    for scan in TRAVELLER_TEXT:
        if scan.action not in ("delete", "redact"):
            continue
        key = scan_key(scan)
        guard = " AND NOT (t.id::text = ANY($2::text[]))" if scan.table == "shared.job" else ""
        args = (email_sha256, protect) if guard else (email_sha256,)
        if scan.action == "delete":
            st = await conn.execute(f"DELETE FROM {qname(scan.table)} t WHERE {traveller_where(scan)}{guard}", *args)
        else:
            st = await conn.execute(f"UPDATE {qname(scan.table)} t SET {scan.redact_set} "
                                    f"WHERE {traveller_where(scan)}{guard}", *args)
        got, want = _affected(st), expected.get((key, scan.action), 0)
        if got != want:
            raise CountMismatch(f"{scan.action} {key}: {got} rows, the approved DRY RUN had {want}")
        done.append({"table": key, "action": scan.action, "rows": got})
    return done

"""AA-1036 CIS 369 DRY RUN (read-only): is shared.tenants.api_key_hash safe to drop?

Runs in the CIS api container (DATABASE_URL = cis_app) via tools/ecs/ecsx.sh. Reads the catalog and one count only.
Prints `verdict ready=true` when 368 is registered, no row holds a value, and nothing (view, index, constraint,
policy, trigger, column grant) depends on the column.
"""

import asyncio
import os

import asyncpg

COL = "api_key_hash"


async def main() -> None:
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    problems = []

    attnum = await c.fetchval(
        "SELECT attnum FROM pg_attribute WHERE attrelid = 'shared.tenants'::regclass AND attname = $1 AND NOT attisdropped",
        COL,
    )
    print("column present:", attnum is not None, "attnum", attnum)
    if attnum is None:
        print("verdict ready=true (already absent, 369 registers only)")
        await c.close()
        return

    reg = await c.fetch("SELECT version, applied_at FROM shared.schema_versions WHERE version IN ('368','369') ORDER BY 1")
    print("schema_versions:", [(r["version"], r["applied_at"].isoformat()) for r in reg])
    if not any(r["version"] == "368" for r in reg):
        problems.append("368 not registered")

    total = await c.fetchval("SELECT count(*) AS n FROM shared.tenants")
    nonnull = await c.fetchval(f"SELECT count(*) AS n FROM shared.tenants WHERE {COL} IS NOT NULL")
    print("tenants rows:", total, "non-null api_key_hash:", nonnull)
    if nonnull:
        problems.append(f"{nonnull} rows still hold a value")

    deps = await c.fetch(
        """
        SELECT d.classid::regclass::text AS catalog, d.objid, d.deptype,
               COALESCE(v.oid::regclass::text, '') AS rel
          FROM pg_depend d
          LEFT JOIN pg_rewrite rw ON d.classid = 'pg_rewrite'::regclass AND rw.oid = d.objid
          LEFT JOIN pg_class v ON v.oid = rw.ev_class
         WHERE d.refclassid = 'pg_class'::regclass
           AND d.refobjid = 'shared.tenants'::regclass
           AND d.refobjsubid = $1
        """,
        attnum,
    )
    for r in deps:
        print("dependency:", r["catalog"], r["objid"], r["deptype"], r["rel"])
    if deps:
        problems.append(f"{len(deps)} catalog objects depend on the column")

    idx = await c.fetch(
        "SELECT indexrelid::regclass::text AS name FROM pg_index WHERE indrelid = 'shared.tenants'::regclass AND $1 = ANY(indkey)",
        attnum,
    )
    print("indexes on column:", [r["name"] for r in idx])
    if idx:
        problems.append("index on column")

    con = await c.fetch(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'shared.tenants'::regclass AND $1 = ANY(conkey)", attnum
    )
    print("constraints on column:", [r["conname"] for r in con])
    if con:
        problems.append("constraint on column")

    pol = await c.fetch(
        "SELECT policyname FROM pg_policies WHERE schemaname = 'shared' AND tablename = 'tenants' "
        "AND (coalesce(qual,'') LIKE '%' || $1 || '%' OR coalesce(with_check,'') LIKE '%' || $1 || '%')",
        COL,
    )
    print("policies naming column:", [r["policyname"] for r in pol])
    if pol:
        problems.append("policy names column")

    trg = await c.fetch(
        """
        SELECT t.tgname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
         WHERE NOT t.tgisinternal AND p.prosrc LIKE '%' || $1 || '%'
        """,
        COL,
    )
    print("triggers whose function names column:", [r["tgname"] for r in trg])
    if trg:
        problems.append("trigger function names column")

    fns = await c.fetch(
        """
        SELECT n.nspname || '.' || p.proname AS fn FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosrc LIKE '%' || $1 || '%'
        """,
        COL,
    )
    print("functions naming column (info):", [r["fn"] for r in fns])

    grants = await c.fetch(
        "SELECT grantee, privilege_type FROM information_schema.column_privileges "
        "WHERE table_schema='shared' AND table_name='tenants' AND column_name=$1",
        COL,
    )
    print("column grants (dropped with the column):", sorted({(r["grantee"], r["privilege_type"]) for r in grants}))

    print("problems:", problems or "none")
    print("verdict ready=" + ("true" if not problems else "false"))
    await c.close()


asyncio.run(main())

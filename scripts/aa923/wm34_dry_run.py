"""AA-923 WM-34 DRY RUN (read-only): the two retired learning tables before their quarantine.

Skill aa-cis-schema references/destructive-ops.md step 1 and data standards DM-3.9 (candidate -> quarantine -> drop).
Nothing is written: the session is set read-only and the script runs only SELECTs (counts, catalog views,
pg_stat_user_tables). Output is counts, names, timestamps and roles, never row text.

Run it through the S3-mediated ECS exec in the CIS api container (the container's DATABASE_URL = cis_app, which reads
both tables and the catalog; ssl="require"):  python3 /tmp/s.py
Paste the output into the AA-923 Linear comment; the quarantine migration (CIS 316) needs `verdict ready=true`.

Output lines (prefix SCRIPT):
  SCRIPT now=<ts> login=<role>
  SCRIPT table=<schema.name> exists=<t|f> rows=<n> active=<n> last_created=<ts|-> seq=<n> idx=<n> ins=<n> upd=<n>
         del=<n> live=<n> stats_reset=<ts|->
  SCRIPT pipeline_lessons stage=<stage> active=<t|f> rows=<n>
  SCRIPT acp_output_rules tenant=<platform|null|other> source=<type> active=<t|f> rows=<n>
  SCRIPT dep object=<o> kind=<view|fk_in|fk_out> name=<n>
  SCRIPT grant object=<o> grantee=<role> privileges=<p,...>
  SCRIPT verdict ready=<true|false> reasons=<...>
ready: both tables exist, no view and no inbound FK depends on either, and acp_output_rules holds no row of a
tenant other than the platform (a tenant row would leave the erasure's reach once quarantined).
"""
from __future__ import annotations

import asyncio
import os

PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
TABLES = ("shared.pipeline_lessons", "acp_shared.acp_output_rules")

STAT_SQL = """
SELECT s.seq_scan, coalesce(s.idx_scan, 0) AS idx_scan, s.n_tup_ins, s.n_tup_upd, s.n_tup_del, s.n_live_tup,
       (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()) AS stats_reset
  FROM pg_stat_user_tables s WHERE s.relid = $1::regclass
"""
DEPS_SQL = """
SELECT 'view' AS kind, v.oid::regclass::text AS name
  FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
  JOIN pg_class v ON v.oid = r.ev_class
 WHERE d.refobjid = $1::regclass AND v.oid <> $1::regclass
UNION
SELECT 'fk_in', c.conrelid::regclass::text || '.' || c.conname FROM pg_constraint c
 WHERE c.contype = 'f' AND c.confrelid = $1::regclass AND c.conrelid <> $1::regclass
UNION
SELECT 'fk_out', c.conname || '->' || c.confrelid::regclass::text FROM pg_constraint c
 WHERE c.contype = 'f' AND c.conrelid = $1::regclass
ORDER BY 1, 2
"""
GRANTS_SQL = """
SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges
  FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE c.oid = $1::regclass AND a.grantee <> c.relowner
 GROUP BY 1 ORDER BY 1
"""


def _ts(v) -> str:
    return v.isoformat() if v is not None else "-"


async def main() -> None:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    reasons: list[str] = []
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        now, login = await conn.fetchval("SELECT now()"), await conn.fetchval("SELECT current_user")
        print(f"SCRIPT now={_ts(now)} login={login}")
        for table in TABLES:
            if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
                print(f"SCRIPT table={table} exists=f")
                reasons.append(f"{table}:missing")
                continue
            rows = await conn.fetchval(f"SELECT count(*) FROM {table}")
            active = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE is_active IS TRUE")
            last = await conn.fetchval(f"SELECT max(created_at) FROM {table}")
            st = await conn.fetchrow(STAT_SQL, table)
            print(f"SCRIPT table={table} exists=t rows={rows} active={active} last_created={_ts(last)} "
                  f"seq={st['seq_scan']} idx={st['idx_scan']} ins={st['n_tup_ins']} upd={st['n_tup_upd']} "
                  f"del={st['n_tup_del']} live={st['n_live_tup']} stats_reset={_ts(st['stats_reset'])}")
            for d in await conn.fetch(DEPS_SQL, table):
                print(f"SCRIPT dep object={table} kind={d['kind']} name={d['name']}")
                if d["kind"] in ("view", "fk_in"):
                    reasons.append(f"{table}:{d['kind']}:{d['name']}")
            for g in await conn.fetch(GRANTS_SQL, table):
                print(f"SCRIPT grant object={table} grantee={g['grantee']} privileges={g['privileges']}")
        if await conn.fetchval("SELECT to_regclass('shared.pipeline_lessons') IS NOT NULL"):
            for r in await conn.fetch("SELECT stage, coalesce(is_active, false) AS active, count(*) AS n "
                                      "FROM shared.pipeline_lessons GROUP BY 1, 2 ORDER BY 1, 2"):
                print(f"SCRIPT pipeline_lessons stage={r['stage']} active={'t' if r['active'] else 'f'} rows={r['n']}")
        if await conn.fetchval("SELECT to_regclass('acp_shared.acp_output_rules') IS NOT NULL"):
            for r in await conn.fetch(
                    "SELECT CASE WHEN tenant_id IS NULL THEN 'null' WHEN tenant_id = $1 THEN 'platform' "
                    "ELSE 'other' END AS tenant, source_type, coalesce(is_active, false) AS active, count(*) AS n "
                    "FROM acp_shared.acp_output_rules GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", PLATFORM_TENANT_ID):
                print(f"SCRIPT acp_output_rules tenant={r['tenant']} source={r['source_type']} "
                      f"active={'t' if r['active'] else 'f'} rows={r['n']}")
                if r["tenant"] == "other":
                    reasons.append(f"acp_output_rules:tenant_rows:{r['n']}")
    finally:
        await conn.close()
    print(f"SCRIPT verdict ready={'false' if reasons else 'true'} reasons={','.join(reasons) or '-'}")


if __name__ == "__main__":
    asyncio.run(main())

"""AA-1058 — how much of the ledger names its object, and what a tour cost. READ ONLY.

Runs in the CIS api (or worker) container (python3 + asyncpg, DATABASE_URL = cis_app, ssl=require), through
tools/ecs/ecsx.sh in the root repo:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/aa1058/ledger_attribution.py HOURS=24

For the last HOURS hours (default 24, at most 2160) it prints:
  * per stage of shared.llm_call_log: rows, cost, the share of rows carrying tour_id / content_piece_id / job_id /
    prompt_version, the key the AA-1058 rule asks for (shared/llm_client/attribution.py when the image has it) and the
    share carrying it; rows with no app or an unattributed stage;
  * cost per tour: tours that ran, their summed cost, cost per tour, per stage prefix, and the top tours (8-character
    id prefixes);
  * cost per content piece for t9 / t10;
  * shared.dfs_call_log rows per stage and app (an empty stage counted);
  * the share of new S1 versions (silver_aa_internal.generated_content) with a prompt_version.
Only counts, sums, shares, stage names and 8-character id prefixes are printed: no texts. Every query is isolated: a
failure prints `ERR <section> <ExceptionName>` and the script goes on.

Tables / columns: AA-Ecosys docs/architecture/db-schema-reference.md (shared.llm_call_log, shared.dfs_call_log,
silver_aa_internal.generated_content).
"""
import asyncio
import os

import asyncpg

MAX_HOURS = 2160
TOP_TOURS = 10

# The AA-1058 rules, used when the image predates shared/llm_client/attribution.py (same order and meaning).
FALLBACK_RULES = (
    ("a3_demand", "job_id"), ("a3_question_scope", "job_id"), ("a3_question_landing", "job_id"),
    ("a3_segment_type", "job_id"), ("a3_segment_match", "job_id"),
    ("s1_", "tour_id"), ("a1_", "tour_id"), ("t5_", "tour_id"), ("a3_atomize", "tour_id"),
    ("t9_", "content_piece_id"), ("t10_", "content_piece_id"),
)

STAGE_SQL = """
    SELECT stage, count(*) AS n, coalesce(sum(cost_usd), 0)::float AS cost,
           count(*) FILTER (WHERE tour_id IS NOT NULL) AS with_tour,
           count(*) FILTER (WHERE content_piece_id IS NOT NULL) AS with_piece,
           count(*) FILTER (WHERE job_id IS NOT NULL) AS with_job,
           count(*) FILTER (WHERE prompt_version IS NOT NULL) AS with_prompt,
           count(*) FILTER (WHERE app IS NULL) AS no_app
    FROM shared.llm_call_log
    WHERE created_at >= now() - make_interval(hours => $1)
    GROUP BY stage ORDER BY cost DESC, stage
"""

TOUR_TOTAL_SQL = """
    SELECT count(DISTINCT tour_id) AS tours, coalesce(sum(cost_usd), 0)::float AS cost
    FROM shared.llm_call_log
    WHERE created_at >= now() - make_interval(hours => $1) AND tour_id IS NOT NULL
"""

TOUR_PREFIX_SQL = """
    SELECT split_part(stage, '_', 1) AS prefix, count(DISTINCT tour_id) AS tours,
           coalesce(sum(cost_usd), 0)::float AS cost
    FROM shared.llm_call_log
    WHERE created_at >= now() - make_interval(hours => $1) AND tour_id IS NOT NULL
    GROUP BY 1 ORDER BY cost DESC, prefix
"""

TOUR_TOP_SQL = """
    SELECT tour_id::text AS tour, count(*) AS n, coalesce(sum(cost_usd), 0)::float AS cost
    FROM shared.llm_call_log
    WHERE created_at >= now() - make_interval(hours => $1) AND tour_id IS NOT NULL
    GROUP BY tour_id ORDER BY cost DESC LIMIT $2
"""

PIECE_SQL = """
    SELECT split_part(stage, '_', 1) AS prefix, count(DISTINCT content_piece_id) AS pieces,
           coalesce(sum(cost_usd), 0)::float AS cost
    FROM shared.llm_call_log
    WHERE created_at >= now() - make_interval(hours => $1) AND content_piece_id IS NOT NULL
      AND (stage LIKE 't9\\_%' OR stage LIKE 't10\\_%')
    GROUP BY 1 ORDER BY prefix
"""

DFS_SQL = """
    SELECT coalesce(nullif(stage, ''), '(none)') AS stage, coalesce(app, '(none)') AS app, count(*) AS n,
           coalesce(sum(cost_usd), 0)::float AS cost
    FROM shared.dfs_call_log
    WHERE created_at >= now() - make_interval(hours => $1)
    GROUP BY 1, 2 ORDER BY cost DESC, 1, 2
"""

GC_SQL = """
    SELECT count(*) AS versions, count(*) FILTER (WHERE prompt_version IS NOT NULL) AS with_prompt
    FROM silver_aa_internal.generated_content
    WHERE created_at >= now() - make_interval(hours => $1)
"""


def out(metric, value):
    print(f"ATTRIB {metric}={value}")


def share(part, whole):
    return "null" if not whole else f"{part / whole:.3f}"


def money(v):
    return f"{float(v or 0.0):.4f}"


def rules():
    """The AA-1058 rules: the image's when it has them, else FALLBACK_RULES."""
    try:
        from shared.llm_client import attribution
        return tuple(attribution.RULES), "image"
    except Exception:  # noqa: BLE001 — an image from before AA-1058
        return FALLBACK_RULES, "fallback"


def required_key(stage, rule_list):
    for name, key in rule_list:
        if (name.endswith("_") and stage.startswith(name)) or stage == name:
            return key
    return None


KEY_COLUMN = {"tour_id": "with_tour", "content_piece_id": "with_piece", "job_id": "with_job"}


def stage_lines(rows, rule_list):
    """The per-stage lines and the totals of the ruled stages: (lines, ruled_rows, ruled_with_key)."""
    lines, ruled, carried = [], 0, 0
    for r in rows:
        stage, n = r["stage"], r["n"]
        key = required_key(stage, rule_list)
        has = r[KEY_COLUMN[key]] if key else None
        if key:
            ruled += n
            carried += has
        lines.append(
            f"stage.{stage} rows={n} cost={money(r['cost'])} tour={share(r['with_tour'], n)} "
            f"piece={share(r['with_piece'], n)} job={share(r['with_job'], n)} "
            f"prompt_version={share(r['with_prompt'], n)} no_app={r['no_app']} "
            f"rule={key or '-'} rule_ok={share(has, n) if key else '-'}")
    return lines, ruled, carried


async def section(name, coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — every section is isolated
        print(f"ERR {name} {type(e).__name__}")
        return None


async def report(conn, hours: int) -> None:
    out("hours", hours)
    rule_list, source = rules()
    out("rules", source)

    rows = await section("stages", conn.fetch(STAGE_SQL, hours))
    if rows is not None:
        lines, ruled, carried = stage_lines(rows, rule_list)
        for line in lines:
            print(f"ATTRIB {line}")
        total = sum(r["n"] for r in rows)
        out("ledger.rows", total)
        out("ledger.cost", money(sum(r["cost"] for r in rows)))
        out("ledger.no_app", sum(r["no_app"] for r in rows))
        out("ledger.unattributed_stage", sum(r["n"] for r in rows if r["stage"] in ("", "unattributed")))
        out("ledger.ruled_rows", ruled)
        out("ledger.ruled_with_key_share", share(carried, ruled))

    tot = await section("tours", conn.fetchrow(TOUR_TOTAL_SQL, hours))
    if tot is not None:
        out("tours.count", tot["tours"])
        out("tours.cost", money(tot["cost"]))
        out("tours.cost_per_tour", money(tot["cost"] / tot["tours"]) if tot["tours"] else "null")
    for r in await section("tours_by_prefix", conn.fetch(TOUR_PREFIX_SQL, hours)) or []:
        per_tour = money(r["cost"] / r["tours"]) if r["tours"] else "null"
        out(f"tours.prefix.{r['prefix']}", f"tours={r['tours']} cost={money(r['cost'])} per_tour={per_tour}")
    for r in await section("tours_top", conn.fetch(TOUR_TOP_SQL, hours, TOP_TOURS)) or []:
        out(f"tours.top.{str(r['tour'])[:8]}", f"rows={r['n']} cost={money(r['cost'])}")

    for r in await section("pieces", conn.fetch(PIECE_SQL, hours)) or []:
        out(f"pieces.prefix.{r['prefix']}",
            f"pieces={r['pieces']} cost={money(r['cost'])} "
            f"per_piece={money(r['cost'] / r['pieces']) if r['pieces'] else 'null'}")

    dfs = await section("dfs", conn.fetch(DFS_SQL, hours))
    if dfs is not None:
        for r in dfs:
            out(f"dfs.{r['stage']}.{r['app']}", f"rows={r['n']} cost={money(r['cost'])}")
        out("dfs.no_stage", sum(r["n"] for r in dfs if r["stage"] in ("(none)", "unattributed")))

    gc = await section("s1_versions", conn.fetchrow(GC_SQL, hours))
    if gc is not None:
        out("s1.versions", gc["versions"])
        out("s1.prompt_version_share", share(gc["with_prompt"], gc["versions"]))


def hours_from_env() -> int:
    try:
        h = int(os.environ.get("HOURS", "24"))
    except ValueError:
        h = 24
    return max(1, min(h, MAX_HOURS))


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await report(conn, hours_from_env())
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

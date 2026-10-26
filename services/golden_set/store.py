"""AA-919 (WM-17) — SQL of the golden set (CIS 284): read the current state, check the tenants, write a set, its
items and the baseline_import runs, all inside the caller's transaction. asyncpg, as cis_app (DATABASE_URL).

Every read of a tenant table names the tenant or the set explicitly; cis_app's RLS policy is USING (true) (X1
parity), so the code is what keeps tenants apart (ADR 0010 decision 10).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from services.golden_set.derive import GoldenSet

ADVISORY_KEY = 91917                  # pg_advisory_xact_lock(hashtext(name), ADVISORY_KEY): one import per set name

SET_BY_VERSION_SQL = """
    SELECT set_id, status, source_sha256, item_count FROM shared.eval_golden_set WHERE name = $1 AND version = $2
"""
ACTIVE_SET_SQL = "SELECT set_id, version FROM shared.eval_golden_set WHERE name = $1 AND status = 'active'"
TENANTS_SQL = """
    SELECT tenant_id::text AS tenant_id, cancelled_at, is_active
      FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])
"""
SUPERSEDE_SQL = """
    UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now()
     WHERE name = $1 AND status = 'active'
    RETURNING set_id, version
"""
INSERT_SET_SQL = """
    INSERT INTO shared.eval_golden_set (name, version, status, source_uri, source_sha256, seed, holdout_pct,
                                        tier_rules, shared_inputs, meta, label_history, item_count, imported_by)
    VALUES ($1, $2, 'active', $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, $11, $12)
    RETURNING set_id
"""
INSERT_ITEM_SQL = """
    INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind, tiers,
                                         country, days, input, control_output, post_edit, pipeline_verdict, label,
                                         label_verdict, label_source, injection_spec, refs, input_sha256, pick_reason)
    VALUES ($1, $2, $3, $4, $5, $6::uuid, $7, $8, $9, $10::text[], $11, $12, $13::jsonb, $14::jsonb, $15, $16::jsonb,
            $17::jsonb, $18, $19, $20::jsonb, $21::jsonb, $22, $23)
    RETURNING item_id
"""
ITEM_IDS_SQL = "SELECT sample_id, item_id FROM shared.eval_golden_item WHERE set_id = $1"
BASELINE_RUN_SQL = """
    SELECT id FROM shared.prompt_eval_runs
     WHERE pipeline = 'writing_memory' AND run_kind = 'baseline_import' AND golden_set_id = $1 AND writer = $2
"""
INSERT_RUN_SQL = """
    INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, regression_detected, details,
                                         triggered_by, run_kind, writer, golden_set_id, tiers, stop_reason, verdict,
                                         fingerprint, finished_at)
    VALUES ('writing_memory', $1, $2, $3, false, $4::jsonb, $5, $6, $7, $8, '{}'::text[], $9, $10, $11::jsonb, now())
    RETURNING id
"""
INSERT_RESULT_SQL = """
    INSERT INTO shared.eval_item_result (run_id, item_id, writer, scope, tenant_id, arm, mode, phase, output,
                                         output_sha256, verdict, score, failure_codes, judge_model, judge_version,
                                         cost_usd, details)
    VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8, $9::jsonb, $10, $11, $12, $13::text[], $14, $15, $16, $17::jsonb)
"""
COUNTS_SQL = """
    SELECT writer, split, count(*)::int AS n FROM shared.eval_golden_item WHERE set_id = $1 GROUP BY 1, 2 ORDER BY 1, 2
"""
TIER_COUNTS_SQL = """
    SELECT writer, t AS tier, count(*)::int AS n
      FROM shared.eval_golden_item, unnest(tiers) t WHERE set_id = $1 GROUP BY 1, 2 ORDER BY 1, 2
"""
RESULT_COUNTS_SQL = """
    SELECT r.writer, r.mode, r.verdict, count(*)::int AS n
      FROM shared.eval_item_result r JOIN shared.prompt_eval_runs p ON p.id = r.run_id
     WHERE p.golden_set_id = $1 AND p.run_kind = 'baseline_import'
     GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""


class ImportRefused(RuntimeError):
    """The database state forbids the import (another sha for the version, a missing or offboarded tenant)."""


def _j(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


async def tenant_problems(conn, tenant_ids: list[str]) -> list[str]:
    """Every tenant of the items must exist and not be offboarded (critic 11 / 37): an offboarded tenant's text must
    not come back after the AA-839 erasure."""
    if not tenant_ids:
        return []
    rows = {r["tenant_id"]: r for r in await conn.fetch(TENANTS_SQL, tenant_ids)}
    problems = []
    for t in tenant_ids:
        r = rows.get(t)
        if r is None:
            problems.append(f"tenant {t} is not in shared.tenants")
        elif r["cancelled_at"] is not None:
            problems.append(f"tenant {t} is offboarded (cancelled_at set)")
    return problems


def _semver(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


async def plan(conn, gs: GoldenSet, *, allow_downgrade: bool = False) -> dict:
    """Read-only: what an apply would do (or why it is refused). A new version must be semver-greater than the active
    one: importing an older fixture would otherwise supersede the newer active set (allow_downgrade: on purpose)."""
    name, version = gs.set_row["name"], gs.set_row["version"]
    existing = await conn.fetchrow(SET_BY_VERSION_SQL, name, version)
    active = await conn.fetchrow(ACTIVE_SET_SQL, name)
    problems = await tenant_problems(conn, gs.tenant_ids)
    action, supersedes = "insert", None
    if existing is not None:
        if existing["source_sha256"] != gs.set_row["source_sha256"]:
            problems.append(f"{name}@{version} was imported from other bytes (sha256 {existing['source_sha256'][:12]}…"
                            f" vs {gs.set_row['source_sha256'][:12]}…): a changed fixture needs a new version")
        action = "exists"                      # apply writes only the missing baseline runs; it supersedes nothing
    elif active is not None:
        if _semver(version) <= _semver(active["version"]) and not allow_downgrade:
            problems.append(f"{version} is not newer than the active {active['version']} (--allow-downgrade to make "
                            f"an older version active on purpose)")
        supersedes = {"set_id": str(active["set_id"]), "version": active["version"]}
    return {"action": action, "existing_set_id": None if existing is None else str(existing["set_id"]),
            "supersedes": supersedes, "problems": problems}


async def apply(conn, gs: GoldenSet, baseline: dict[str, dict], *, allow_downgrade: bool = False) -> dict:
    """Write the set, its items and the missing baseline_import runs, in the caller's transaction. Idempotent on
    (name, version) + source_sha256; refuses (ImportRefused) on any problem of plan()."""
    name = gs.set_row["name"]
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1), $2)", name, ADVISORY_KEY)
    p = await plan(conn, gs, allow_downgrade=allow_downgrade)
    if p["problems"]:
        raise ImportRefused("; ".join(p["problems"]))
    out: dict[str, Any] = {"action": p["action"], "superseded": None, "baseline_runs": {}}
    if p["action"] == "exists":
        set_id = p["existing_set_id"]
        ids = {r["sample_id"]: r["item_id"] for r in await conn.fetch(ITEM_IDS_SQL, set_id)}
    else:
        s = gs.set_row
        # the new version becomes the only active one (unique index eval_golden_set_one_active). A release whose
        # fingerprint names the old version is flagged by WM-18's reverify scan (the release table comes with it).
        superseded = await conn.fetch(SUPERSEDE_SQL, name)
        out["superseded"] = [{"set_id": str(r["set_id"]), "version": r["version"]} for r in superseded] or None
        set_id = await conn.fetchval(INSERT_SET_SQL, name, s["version"], s["source_uri"], s["source_sha256"],
                                     s["seed"], s["holdout_pct"], _j(s["tier_rules"]), _j(s["shared_inputs"]),
                                     _j(s["meta"]), _j(s["label_history"]), s["item_count"], s["imported_by"])
        ids = {}
        for i in gs.items:
            ids[i["sample_id"]] = await conn.fetchval(
                INSERT_ITEM_SQL, set_id, i["sample_id"], i["writer"], i["channel"], i["scope"], i["tenant_id"],
                i["split"], i["trap"], i["kind"], i["tiers"], i["country"], i["days"], _j(i["input"]),
                _j(i["control_output"]), i["post_edit"], _j(i["pipeline_verdict"]), _j(i["label"]),
                i["label_verdict"], i["label_source"], _j(i["injection_spec"]), _j(i["refs"]), i["input_sha256"],
                i["pick_reason"])
    out["set_id"] = str(set_id)
    items = {i["sample_id"]: i for i in gs.items}
    for writer, b in baseline.items():
        found = await conn.fetchval(BASELINE_RUN_SQL, set_id, writer)
        if found is not None:
            out["baseline_runs"][writer] = {"run_id": str(found), "action": "exists"}
            continue
        missing = [r["sample_id"] for r in b["results"] if r["sample_id"] not in ids]
        if missing:
            raise ImportRefused(f"baseline rows for items not in the set: {missing[:5]}")
        run = b["run"]
        run_id = await conn.fetchval(INSERT_RUN_SQL, run["prompt_version"], run["tour_count"], run["cost_usd"],
                                     _j(run["details"]), gs.set_row["imported_by"], run["run_kind"], writer, set_id,
                                     run["stop_reason"], run["verdict"], _j(run["fingerprint"]))
        for r in b["results"]:
            it = items[r["sample_id"]]
            await conn.execute(INSERT_RESULT_SQL, run_id, ids[r["sample_id"]], it["writer"], it["scope"],
                               it["tenant_id"], r["arm"], r["mode"], r["phase"], _j(r["output"]), r["output_sha256"],
                               r["verdict"], r["score"], r["failure_codes"], r["judge_model"], r["judge_version"],
                               r["cost_usd"], _j(r["details"]))
        out["baseline_runs"][writer] = {"run_id": str(run_id), "action": "insert", "results": len(b["results"])}
    out["counts"] = await counts(conn, set_id)
    return out


async def counts(conn, set_id) -> dict:
    """Counts only, read back from the DB (the evidence for the Linear comment)."""
    by_split: dict[str, dict] = {}
    for r in await conn.fetch(COUNTS_SQL, set_id):
        by_split.setdefault(r["writer"], {})[r["split"]] = r["n"]
    by_tier: dict[str, dict] = {}
    for r in await conn.fetch(TIER_COUNTS_SQL, set_id):
        by_tier.setdefault(r["writer"], {})[r["tier"]] = r["n"]
    results: dict[str, dict] = {}
    for r in await conn.fetch(RESULT_COUNTS_SQL, set_id):
        results.setdefault(r["writer"], {})[f"{r['mode']}:{r['verdict']}"] = r["n"]
    return {"by_writer_split": by_split, "by_writer_tier": by_tier, "baseline_results": results}

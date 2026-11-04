"""AA-1026 (S267, family fallback chains) — $0 check of every stage route on the live registry: which model would
serve each stage right now, and whether the routes keep the chain rules. Calls NO model.

Runs inside the CIS api / worker container (the S3-mediated ECS exec runner puts /app on sys.path), as the
container's DATABASE_URL (cis_app). It reads shared.llm_role_config, shared.llm_model_catalog, the connection /
deployment registry and the circuit state (shared.llm_provider_health) through the gateway's own readers, writes
nothing, and needs the code of ``api/services/llm_route_chains.py`` in the image (deployed with this change):

    python3 route_chain_check.py              # every stage, human-readable, then one JSON object
    python3 route_chain_check.py --json       # the JSON object only

For each active stage: its chain, the attempts in the gateway's order (``client.route_attempts``: model first by
default, tech lead S267, each attempt listing the deployments in the order they would be tried, e.g. acc3 then the
Anthropic API; the AWS-first passes only with ``LLM_ROUTE_AWS_FIRST=on``), each attempt's status (``call`` = a
deployment would serve now, with its connection; ``skip`` = why not: circuit open with the class that opened it,
budget reached, role not served, no enabled deployment in that scope; ``refused`` = every planned deployment of it is
refused on record), and FIRST = the first model@connection that would SERVE the stage. AA-1042 (S268): a deployment
the account refuses on record (``stage_coverage.refusal``: the CIS 367 probe state, the breaker's refusals, the
connection's access state, the rule of the Stage coverage card) does not count as first: it is listed under the
attempt's ``refused`` and the stage's ``skipped`` with its reason, and ``called_first`` keeps the first
model@connection the gateway would call (it still tries a refused deployment once per breaker period). Then the
chain-rule violations (``llm_route_chains.violations``) of the live rows. Each stage also carries its ``product``
and ledger ``app`` (``llm_route_chains.product_of`` / ``ledger_app``, S267: one Anthropic API key, per-product
spend read from the ledger by stage); a stage no rule maps is listed under ``unmapped_stages``. Exit code 0 (a report).

Output markers: ``--- ROUTE CHAIN CHECK ---`` / ``--- END ---`` around the JSON object
``{"stages": [...], "violations": [[stage, rule, detail], ...], "first_by_stage": {stage: "model@connection"},
"unmapped_stages": [stage, ...]}``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _load() -> tuple[list[dict], list, object]:
    """The stage rows, the catalog and the refusals on record (``stage_coverage.RefusalEvidence``, None before the
    registry migrations)."""
    import asyncpg

    from shared.llm_client import stage_coverage
    from shared.llm_client.catalog import list_models
    from shared.llm_client.role_config import list_stage_configs
    from shared.secrets import get_database_url
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        evidence = await stage_coverage.load_evidence(conn)
    finally:
        await conn.close()
    return await list_stage_configs(), await list_models(), evidence


def _config(row: dict):
    from shared.llm_client.role_config import StageConfig
    return StageConfig(stage=row["stage"], role=row["role"], provider=row["provider"], model_id=row["model_id"],
                       account_route=row.get("account_route"),
                       fallback_model_ids=tuple(row.get("fallback_model_ids") or ()),
                       shadow_model_id=row.get("shadow_model_id"),
                       shadow_sample_pct=row.get("shadow_sample_pct") or 0,
                       allowed_scopes=tuple(row["allowed_scopes"]) if row.get("allowed_scopes") else None,
                       enabled=row.get("is_active") is not False)


def _reach(models: list) -> tuple[dict, set, dict]:
    """Model key -> data scopes of every deployment (enabled or not, the admin's policy view); the chat models served
    only by connection acc2; model key -> the data scope of each deployment row (the attempt cap)."""
    from shared.llm_client import connections, deployments
    conns = connections.registry_rows_sync()
    reach: dict = {}
    acc2_only: set = set()
    per_row: dict = {}
    for m in models:
        rows = deployments.deployments_for_sync(m.model_key, enabled_only=False)
        row_scopes = [conns[d.connection_key].data_scope for d in rows if d.connection_key in conns]
        if row_scopes:
            reach[m.model_key] = frozenset(row_scopes)
            per_row[m.model_key] = row_scopes
        if rows and {d.connection_key for d in rows} == {"acc2"} and m.api_style != "embed":
            acc2_only.add(m.model_key)
    return reach, acc2_only, per_row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the JSON object only")
    args = parser.parse_args()

    from api.services import llm_route_chains as route_chains

    rows, models, evidence = asyncio.run(_load())
    stages = []
    for row in rows:
        if row.get("is_active") is False:
            stages.append({"stage": row["stage"], "role": row["role"], "switched_off": True,
                           "reason": row.get("disabled_reason")})
            continue
        stages.append(route_chains.resolve_stage(_config(row), evidence))
    for s in stages:
        s.update(product=route_chains.product_of(s["stage"]), app=route_chains.ledger_app(s["stage"]))
    reach, acc2_only, per_row = _reach(models)
    found = route_chains.violations(rows, models, reach, acc2_only, per_row)
    report = {"stages": stages, "violations": [list(v) for v in found],
              "first_by_stage": {s["stage"]: s.get("first") for s in stages if not s.get("switched_off")},
              "unmapped_stages": sorted(s["stage"] for s in stages if s.get("product") is None)}

    if not args.json:
        for s in stages:
            if s.get("switched_off"):
                print(f"{s['stage']:26} switched off ({s.get('reason') or 'no reason'})")
                continue
            print(f"{s['stage']:26} {s['role']:8} {','.join(s.get('scopes') or ['-']):22} "
                  f"{s.get('product') or 'UNMAPPED'}/{s.get('app') or '-'} FIRST {s.get('first')}")
            for a in s["attempts"]:
                where = a.get("connection") or a.get("reason")
                print(f"    {a['model']:22} [{a['scope']}] {a['status']}: {where}")
                for r in a.get("refused") or []:
                    print(f"    {'':22}   refused on {r['connection']}: {r['reason']}")
            if s.get("error"):
                print(f"    ERROR {s['error']}")
        print(f"violations: {len(found)}; stages with no product: {', '.join(report['unmapped_stages']) or 'none'}")
        for v in found:
            print("   ", " | ".join(v))
    print("--- ROUTE CHAIN CHECK ---")
    print(json.dumps(report, default=str, sort_keys=True))
    print("--- END ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())

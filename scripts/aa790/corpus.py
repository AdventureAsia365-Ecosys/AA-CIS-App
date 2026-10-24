"""AA-790 — the stored T9 pieces the census and the voice replay read, as one row shape.

Row: {"id", "piece_id", "tenant", "channel", "status", "text", "cta", "brand_pack", "custom_validators",
"label", "source"}. ``label`` is a verdict on the panel / label scale (as_is | light | heavy | reject) or None.

Sources (all read-only, $0):

* ``golden``  — the AA-729 fixture (``docs/eval/aa729/fixture-*.json`` in the root repo): the 30 T9 samples, with
  their brand pack from the stored writer input. Label: the fixture's pass/fail mapped to light/heavy.
* ``pack``    — the S234 label pack (``S234-label-40.xlsx``) plus the panel key (``agreement/panel_key.json``):
  40 pieces with the 4-lens panel majority as the label. The brand pack is the golden pack of the same tenant
  (the pack's BRAND column is a summary).
* ``db``      — every stored ``acp_shared.content_piece`` row (latest attempt per request), with the tenant's
  active ``shared.tenant_brand_rules`` row and the latest ``acp_shared.content_piece_label`` label. Read with
  the container's ``DATABASE_URL`` (role cis_app), ssl=require. SELECT only.
* ``rows``    — a JSON file of rows this module exported (``--rows``), so the census and the replay run locally
  with the branch's detectors on DB rows read by the deployed image.

The image has no ``scripts/`` folder and the deployed ``services/`` predate AA-790, so the DB census runs in two
steps (S3-mediated ECS exec, root runbook ``docs/ops/runbooks/ecs-exec-s3-mediated.md``): upload THIS file alone
and run it in the api container as an exporter (it imports only ``services.acp_shared.brand_pack``)::

  python3 /tmp/s.py --db --export /tmp/rows.json --s3 s3://<scripts bucket>/scripts/aa790-rows.json

then fetch the object into the private root ``.tmp-session/`` and run ``integrity_census --rows <file>`` (and
``voice_rubric_replay --rows <file>``) locally. The rows quote tenant piece text: never commit them to this repo.
"""
from __future__ import annotations

import json
import os
from typing import Optional

PIECES_SQL = """
    SELECT DISTINCT ON (cp.angle_gate_request_id)
           cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, cp.channel, cp.status,
           cp.content_text, t.name AS tenant
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    ORDER BY cp.angle_gate_request_id, cp.attempt_number DESC
"""
BRAND_SQL = """
    SELECT DISTINCT ON (tenant_id) tenant_id::text AS tenant_key, *
    FROM shared.tenant_brand_rules
    WHERE is_active = true
    ORDER BY tenant_id, (brand_name = 'default') DESC, version DESC
"""
LABEL_SQL = """
    SELECT DISTINCT ON (piece_id) piece_id::text AS piece_id, label
    FROM acp_shared.content_piece_label
    ORDER BY piece_id, labelled_at DESC
"""


def _validators(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def load_golden(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        fixture = json.load(fh)
    rows = []
    for s in fixture.get("samples") or []:
        if s.get("kind") != "t9":
            continue
        ctx = (s.get("input") or {}).get("context") or {}
        verdict = (s.get("label") or {}).get("verdict")
        rows.append({
            "id": s.get("sample_id"), "piece_id": (s.get("refs") or {}).get("piece_id"),
            "tenant": (ctx.get("brand_audience") or {}).get("brand_name") or s.get("group"),
            "channel": s.get("channel"), "status": (s.get("pipeline_verdict") or {}).get("status"),
            "text": (s.get("output") or {}).get("content_text") or "", "cta": ctx.get("cta"),
            "brand_pack": ctx.get("brand_audience") or {}, "custom_validators": {},
            "label": {"pass": "light", "fail": "heavy"}.get(verdict), "source": "golden",
        })
    return rows


def load_pack(xlsx: str, panel_key: str, golden: Optional[list[dict]] = None) -> list[dict]:
    import openpyxl

    with open(panel_key, encoding="utf-8") as fh:
        key = json.load(fh).get("pieces") or {}
    packs = {r["tenant"]: (r["brand_pack"], r["cta"]) for r in (golden or []) if r.get("brand_pack")}
    ws = openpyxl.load_workbook(xlsx, read_only=True).worksheets[0]
    rows = []
    it = ws.iter_rows(values_only=True)
    header = [str(c) if c is not None else "" for c in next(it)]
    col = {name: i for i, name in enumerate(header)}
    text_col = next(i for name, i in col.items() if name.startswith("TEXT"))
    for r in it:
        pid = r[col["piece_id"]]
        if not pid:
            continue
        k = key.get(pid) or {}
        tenant = r[col["tenant"]]
        pack, cta = packs.get(tenant, ({}, None))
        rows.append({
            "id": f"r{k.get('round')}#{k.get('idx_in_round')}", "piece_id": pid, "tenant": tenant,
            "channel": r[col["channel"]], "status": k.get("gate_status"), "text": r[text_col] or "", "cta": cta,
            "brand_pack": pack, "custom_validators": {}, "label": k.get("majority"), "source": "pack",
            "panel_ai_tells": (k.get("mean") or {}).get("ai_tells"), "deciding": k.get("deciding_defects"),
        })
    return rows


async def load_db(dsn: Optional[str] = None) -> list[dict]:
    import asyncpg

    from services.acp_shared.brand_pack import pack_from_row

    conn = await asyncpg.connect(dsn or os.environ["DATABASE_URL"], ssl="require")
    try:
        pieces = await conn.fetch(PIECES_SQL)
        # AA-790 review — keyed by the unique text alias: `*` brings its own uuid tenant_id, and asyncpg's
        # record maps a repeated column name to the LAST one, so r["tenant_id"] would be the uuid, not the text
        brands = {r["tenant_key"]: r for r in await conn.fetch(BRAND_SQL)}
        labels = {r["piece_id"]: r["label"] for r in await conn.fetch(LABEL_SQL)}
    finally:
        await conn.close()
    rows = []
    for p in pieces:
        b = brands.get(str(p["tenant_id"]))
        pack = pack_from_row(dict(b)) if b else {}
        rows.append({
            "id": p["piece_id"][:8], "piece_id": p["piece_id"], "tenant": p["tenant"], "channel": p["channel"],
            "status": p["status"], "text": p["content_text"] or "", "cta": pack.get("default_cta"),
            "brand_pack": pack, "custom_validators": _validators(b["custom_validators"]) if b else {},
            "label": labels.get(p["piece_id"]), "source": "db",
        })
    return rows


def load_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    return [r for r in rows if isinstance(r, dict) and r.get("text") is not None]


def export(rows: list[dict], path: str, s3_uri: Optional[str] = None) -> None:
    """Write rows as JSON (and, with ``s3_uri``, upload it with boto3 — the container has no aws CLI)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, default=str)
    if s3_uri:
        import boto3

        bucket, _, key = s3_uri.removeprefix("s3://").partition("/")
        boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).upload_file(path, bucket, key)


def add_args(ap) -> None:
    ap.add_argument("--golden", help="AA-729 fixture JSON (root docs/eval/aa729/fixture-*.json)")
    ap.add_argument("--pack", help="S234 label pack xlsx (needs --panel-key)")
    ap.add_argument("--panel-key", help="agreement/panel_key.json next to the label pack")
    ap.add_argument("--db", action="store_true", help="read acp_shared.content_piece with DATABASE_URL (read-only)")
    ap.add_argument("--rows", help="rows JSON written by `corpus.py --db --export` in the api container")


async def load(args) -> list[dict]:
    rows: list[dict] = []
    golden = load_golden(args.golden) if args.golden else []
    rows += golden
    if args.pack:
        rows += load_pack(args.pack, args.panel_key, golden)
    if args.db:
        rows += await load_db()
    if getattr(args, "rows", None):
        rows += load_rows(args.rows)
    return rows


LIGHT = frozenset({"as_is", "light"})


def main() -> int:
    """The container exporter: ``--db --export <path> [--s3 s3://bucket/key]`` (see the module docstring)."""
    import argparse
    import asyncio
    import sys

    sys.path.insert(0, os.getcwd())  # run as /tmp/s.py from the image's work dir: `services` must import
    ap = argparse.ArgumentParser(description="AA-790 corpus exporter (read-only)")
    add_args(ap)
    ap.add_argument("--export", required=True, help="write the loaded rows here as JSON")
    ap.add_argument("--s3", help="also upload the JSON to this s3://bucket/key")
    args = ap.parse_args()
    rows = asyncio.run(load(args))
    export(rows, args.export, args.s3)
    print(f"exported {len(rows)} rows to {args.export}" + (f" and {args.s3}" if args.s3 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

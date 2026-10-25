#!/usr/bin/env python3
"""AA-977 S4 — seed AA-authored starter content (prep packs, starter templates, campaign blocks) into CIS 275.

Reads `data/aa_content/seed_v1.json` (14 groups x EN + VI = 28 items: 10 "Before you go" prep packs, 2 starter
templates, 2 campaign blocks; hand-written, no LLM) and writes it through `services.aa_content.store`, the same code
and checks as the admin API (`/admin/aa-content`), as actor `seed:AA-977`.

Default = DRY RUN: every seed item is validated (validate.py: kind, locale, group_key, title, body, applies_to, and
the publish rules incl. the prep-pack visa rule), then the existing items are read in a READ ONLY transaction. Prints
what --apply would create and what --publish would publish. Nothing is written.

  --apply            create every (group_key, locale) the database does not have yet, as draft v1. Idempotent: an
                     existing item is never touched, whoever created it.
  --publish          publish the seed's drafts: only items created by `seed:AA-977` that were never published and
                     whose draft is still v1 (a human's later draft is never published by this script). The seed
                     item marked `keep_draft` stays a draft (the rule R check: a draft is never served).
                     May be combined with --apply (create, then publish).
  --expect N         with --apply: refuse (exit 2, nothing written) unless exactly N items are to be created.
  --edit-demo G:L    the rule E check: on the seeded item (group_key G, locale L), which must be published with no
                     draft, save draft n+1 (the published text plus one "Last reviewed" line) and publish it. The old
                     version stays fetchable with its hash; the output lists both versions.
  --seed PATH        the seed file (default: data/aa_content/seed_v1.json next to this repo's scripts/).
  --no-ssl           connect without TLS (a local test database); the default is ssl="require".

Runs inside the CIS api container with its DATABASE_URL (role cis_app: SELECT / INSERT / UPDATE on the two tables,
CIS 275), not a job, so no wave gap is needed. The image has `services/` under /app but no `scripts/` or `data/`, so
both files are fetched next to each other (S3-mediated ECS exec, one line per command):
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa977_seed_aa_content.py s3://$BRONZE/scripts/aa977_seed.py --profile aa365-admin --region us-west-1
  aws s3 cp data/aa_content/seed_v1.json s3://$BRONZE/scripts/aa977_seed_v1.json --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa977_seed.py --profile aa365-admin --region us-west-1 --expires-in 300)
  SEED=$(aws s3 presign s3://$BRONZE/scripts/aa977_seed_v1.json --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  dry run: aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && curl -s \"$SEED\" -o /tmp/seed.json && cd /app && python3 /tmp/s.py --seed /tmp/seed.json'" --profile aa365-admin --region us-west-1
  apply:   same command with 'python3 /tmp/s.py --seed /tmp/seed.json --apply --expect 28 --publish'
  E check: same command with 'python3 /tmp/s.py --seed /tmp/seed.json --edit-demo prep-pack-vietnam:en'
The output prints `seed_sha256`; compare it with `sha256sum data/aa_content/seed_v1.json` to be sure the right file
ran. The JSON summary sits between AA977-JSON-BEGIN and AA977-JSON-END.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
# The repo root locally; /app (the image's WORKDIR) when the file runs from /tmp in the api container.
for _root in (str(REPO), "/app", os.getcwd()):
    if (Path(_root) / "services" / "aa_content").is_dir():
        if _root not in sys.path:
            sys.path.insert(0, _root)
        break

import asyncpg  # noqa: E402

from services.aa_content import store, validate  # noqa: E402
from services.aa_content.validate import ContentInvalid  # noqa: E402

ACTOR = "seed:AA-977"
DEFAULT_SEED = REPO / "data" / "aa_content" / "seed_v1.json"
EXISTING_SQL = f"""
    SELECT i.content_id, i.kind, i.locale, i.group_key, i.created_by, i.retired_at, i.updated_at,
           (SELECT max(v.version) FROM {store.VERSION} v WHERE v.content_id = i.content_id AND v.status = 'published')
               AS published_version,
           (SELECT max(v.version) FROM {store.VERSION} v WHERE v.content_id = i.content_id AND v.status = 'draft')
               AS draft_version
      FROM {store.ITEM} i
     WHERE i.group_key = ANY($1::text[])
"""


# ── the seed file ───────────────────────────────────────────────────────────────────────────────────────────────────

def load_seed(path: Path) -> tuple[list[dict], str]:
    """The flat item list (one entry per group and locale) and the file's sha256."""
    raw = path.read_bytes()
    data = json.loads(raw)
    items: list[dict] = []
    for group in data["groups"]:
        for loc, item in sorted(group["items"].items()):
            items.append({
                "group_key": group["group_key"], "kind": group["kind"], "locale": loc,
                "applies_to": group.get("applies_to") or {}, "title": item["title"],
                "body_markdown": item["body_markdown"], "keep_draft": bool(item.get("keep_draft")),
            })
    return items, hashlib.sha256(raw).hexdigest()


def check_seed(items: list[dict]) -> list[str]:
    """Every rule the API would apply at create and at publish; [] when the seed is clean."""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    group_kind: dict[str, str] = {}
    for it in items:
        key = (it["group_key"], it["locale"])
        label = f"{it['group_key']}:{it['locale']}"
        if key in seen:
            problems.append(f"{label}: listed twice")
        seen.add(key)
        if group_kind.setdefault(it["group_key"], it["kind"]) != it["kind"]:
            problems.append(f"{label}: the group mixes kinds")
        try:
            validate.kind(it["kind"])
            validate.locale(it["locale"])
            validate.group_key(it["group_key"])
            validate.title(it["title"])
            validate.body(it["body_markdown"])
            validate.applies_to(it["applies_to"])
            validate.publishable(it["kind"], it["title"], it["body_markdown"])
        except ContentInvalid as exc:
            problems.append(f"{label}: {exc.code} ({exc.detail})")
    return problems


# ── the plan (pure, unit tested) ────────────────────────────────────────────────────────────────────────────────────

def plan(items: list[dict], existing: list[dict]) -> dict:
    """What --apply and --publish would do, given the existing rows (EXISTING_SQL shape)."""
    have = {(r["group_key"], r["locale"]): r for r in existing}
    to_create = [it for it in items if (it["group_key"], it["locale"]) not in have]
    to_publish, kept, skipped = [], [], []
    for it in items:
        key = (it["group_key"], it["locale"])
        label = f"{it['group_key']}:{it['locale']}"
        row = have.get(key)
        if it["keep_draft"]:
            kept.append(label)
            continue
        if row is None:
            to_publish.append(label)          # created by --apply first, then published
            continue
        if row["created_by"] != ACTOR:
            skipped.append({"item": label, "reason": "not created by the seed"})
        elif row["retired_at"] is not None:
            skipped.append({"item": label, "reason": "retired"})
        elif row["published_version"] is not None:
            skipped.append({"item": label, "reason": f"already published (v{row['published_version']})"})
        elif row["draft_version"] != 1:
            skipped.append({"item": label, "reason": f"draft is v{row['draft_version']}, not the seed's v1"})
        else:
            to_publish.append(label)
    return {
        "to_create": [f"{it['group_key']}:{it['locale']}" for it in to_create],
        "existing": sorted(f"{g}:{loc}" for g, loc in have),
        "to_publish": to_publish,
        "kept_draft": kept,
        "skipped_publish": skipped,
    }


def demo_body(body: str, today: str) -> str:
    """The --edit-demo change: one review line appended (a real-looking edit, not test text)."""
    return f"{body.rstrip()}\n\n_Last reviewed by the Adventure Asia team on {today}._"


# ── database ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _existing(conn: Any, items: list[dict]) -> list[dict]:
    groups = sorted({it["group_key"] for it in items})
    return [dict(r) for r in await conn.fetch(EXISTING_SQL, groups)]


async def _read_existing(conn: Any, items: list[dict]) -> list[dict]:
    if conn.is_in_transaction():                # the integration test's outer transaction
        return await _existing(conn, items)
    async with conn.transaction(readonly=True):
        return await _existing(conn, items)


async def run(args: argparse.Namespace) -> tuple[int, dict]:
    items, sha = load_seed(Path(args.seed))
    out: dict[str, Any] = {"mode": "dry_run", "seed_sha256": sha, "seed_items": len(items),
                           "seed_groups": len({it["group_key"] for it in items})}
    problems = check_seed(items)
    if problems:
        out["seed_problems"] = problems
        return 2, out
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        out["error"] = "DATABASE_URL is not set"
        return 2, out
    conn = await asyncpg.connect(dsn, ssl=None if args.no_ssl else "require")
    try:
        return await execute(conn, args, items, out)
    finally:
        await conn.close()


async def execute(conn: Any, args: argparse.Namespace, items: list[dict], out: dict) -> tuple[int, dict]:
    """The DRY RUN / --apply / --publish / --edit-demo work on one open connection (the integration test passes its
    own rolled-back one). Each store write commits on its own (store functions open their own transaction)."""
    before = plan(items, await _read_existing(conn, items))
    out["plan"] = {**before, "counts": {k: len(v) for k, v in before.items()}}
    if args.edit_demo:
        out["mode"] = "edit_demo"
        return await _edit_demo(conn, args.edit_demo, out)
    if not (args.apply or args.publish):
        return 0, out
    if args.apply and args.expect is not None and len(before["to_create"]) != args.expect:
        out["error"] = f"--expect {args.expect} but {len(before['to_create'])} items are to be created"
        return 2, out
    out["mode"] = "+".join(m for m, on in (("apply", args.apply), ("publish", args.publish)) if on)
    created, published, failed = [], [], []
    if args.apply:
        wanted = set(before["to_create"])
        for it in items:
            label = f"{it['group_key']}:{it['locale']}"
            if label not in wanted:
                continue
            item = await store.create_item(conn, kind=it["kind"], locale=it["locale"], group_key=it["group_key"],
                                           title=it["title"], body_markdown=it["body_markdown"],
                                           applies_to=it["applies_to"], actor=ACTOR)
            created.append({"item": label, "content_id": item["content_id"]})
    if args.publish:
        # Re-plan on the current rows: --apply may just have created them.
        now_plan = plan(items, await _existing(conn, items))
        rows = {f"{r['group_key']}:{r['locale']}": r for r in await _existing(conn, items)}
        for label in now_plan["to_publish"]:
            row = rows.get(label)
            if row is None:                 # --publish without --apply on an item not created yet
                continue
            try:
                res = await store.publish(conn, row["content_id"], expected_updated_at=row["updated_at"],
                                          actor=ACTOR)
            except (store.ContentConflict, ContentInvalid, store.ContentNotFound) as exc:
                failed.append({"item": label, "error": getattr(exc, "code", "not_found"), "detail": str(exc)})
                continue
            body = res["published"]
            published.append({"item": label, "content_id": str(row["content_id"]), "version": body["version"],
                              "content_hash": body["content_hash"]})
        out["skipped_publish"] = now_plan["skipped_publish"]
        out["kept_draft"] = [{"item": label, "content_id": str(rows[label]["content_id"])}
                             for label in now_plan["kept_draft"] if label in rows]
    out["created"], out["published"], out["failed"] = created, published, failed
    out["after"] = {k: len(v) for k, v in plan(items, await _read_existing(conn, items)).items()}
    return (1 if failed else 0), out


async def _edit_demo(conn: Any, target: str, out: dict) -> tuple[int, dict]:
    group_key, _, loc = target.partition(":")
    rows = [r for r in await _existing(conn, [{"group_key": group_key}]) if r["locale"] == loc]
    if not rows:
        out["error"] = f"no item {target}"
        return 2, out
    row = rows[0]
    if row["created_by"] != ACTOR or row["published_version"] is None or row["draft_version"] is not None:
        out["error"] = f"{target} must be a seeded, published item with no draft"
        return 2, out
    content_id = row["content_id"]
    old = await store.get_version(conn, content_id, row["published_version"])
    today = datetime.now(timezone.utc).date().isoformat()
    saved = await store.save_draft(conn, content_id, title=old["title"],
                                   body_markdown=demo_body(old["body_markdown"], today),
                                   applies_to=old["applies_to"], expected_updated_at=_ts(row["updated_at"]),
                                   actor=ACTOR)
    res = await store.publish(conn, content_id, expected_updated_at=_ts(saved["updated_at"]), actor=ACTOR)
    item = await store.get_item(conn, content_id)
    out["edit_demo"] = {
        "item": target, "content_id": str(content_id),
        "versions": [{"version": v["version"], "status": v["status"], "content_hash": v["content_hash"]}
                     for v in item["versions"]],
        "old_version_unchanged": next(v for v in item["versions"]
                                      if v["version"] == old["version"])["content_hash"] == old["content_hash"],
        "new_version": res["published"]["version"],
    }
    return 0, out


def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", default=str(DEFAULT_SEED))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expect", type=int)
    parser.add_argument("--edit-demo", dest="edit_demo")
    parser.add_argument("--no-ssl", dest="no_ssl", action="store_true")
    args = parser.parse_args(argv)
    if args.edit_demo and (args.apply or args.publish):
        parser.error("--edit-demo runs on its own")
    code, out = asyncio.run(run(args))
    print("AA977-JSON-BEGIN")
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    print("AA977-JSON-END")
    return code


if __name__ == "__main__":
    sys.exit(main())

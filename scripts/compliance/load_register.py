#!/usr/bin/env python3
"""AA-1170 (S287 Q2 / Q4) — load the common controls and their requirement mapping into schema compliance (CIS 466).

The register (about 40 controls at the start, each mapped onto requirements of several frameworks) is private: it is
built from the S286 matrix and never committed (the repos are public). This loader is the committed half: it reads a
register file the operator provides, validates it, prints what it would change (DRY RUN, the default) and with
``--apply`` writes it in one transaction. The requirement catalogue itself is seeded by CIS 466.

    python3 load_register.py --file register.json              # DRY RUN: validate and print the plan
    python3 load_register.py --url "<presigned GET>" --apply   # write (the orchestrator, ECS exec, S3-mediated)

Connects with DATABASE_URL (inside the CIS api container: role cis_app, which holds DML on schema compliance), always
with ssl=require unless ``--no-ssl`` (a local test database). Needs only asyncpg and the standard library, so it runs
as a single downloaded file.

File format 1 (an invented example: scripts/compliance/register.example.json):

  {"format": 1,
   "controls": [{"control_id": "EX-IAM-01", "title": "...", "intent": "...", "domain": "identity", "owner_role":
                 "tech lead", "frequency": "daily", "test_type": "auto",
                 "checks": [{"check_id": "repo_rulesets", "required": true}], "scope": "dev", "active": true}],
   "mappings": [{"control_id": "EX-IAM-01", "framework": "iso27001:2022", "ref": "A.5.15", "coverage": "full",
                 "note": "..."}]}

What --apply does (one transaction): upsert every control of the file (title, intent, ... and updated_at); a control in
the table but not in the file is set inactive (never deleted: its evidence history stays readable); the mappings of
the file's controls are replaced by the file's. A mapping to a requirement the catalogue does not have, a control id
or a text field the table would refuse, an owner that looks like a person's e-mail, or text that carries an account
ID, an ARN or an S3 URI stops the run before anything is written. Exit 0 = done (or DRY RUN clean), 1 = refused.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from typing import Any

CONTROL_ID = re.compile(r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,10}){1,3}$")
CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DOMAIN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
FREQUENCIES = ("continuous", "daily", "weekly", "monthly", "quarterly", "annual")
TEST_TYPES = ("auto", "manual", "hybrid")
SCOPES = ("dev", "prod", "all")
COVERAGE = ("full", "partial")
FRAMEWORKS = ("iso27001:2022", "soc2:2017", "iso42001:2023", "owasp-top10:2021", "owasp-llm:2025", "eu-ai-act:2024")
FORBIDDEN = (
    (re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])"), "an account ID"),
    (re.compile(r"arn:aws", re.I), "an ARN"),
    (re.compile(r"s3://", re.I), "an S3 URI"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "an e-mail address"),
)
# The text limits of the CIS 466 CHECK constraints (the database refuses longer text anyway; this says which row).
LIMITS = {"title": (3, 160), "intent": (0, 1000), "owner_role": (2, 60), "note": (0, 300)}


def _text_problems(where: str, field: str, value: Any) -> list[str]:
    if value is None:
        return [] if field in ("intent", "note") else [f"{where}: {field} is missing"]
    if not isinstance(value, str):
        return [f"{where}: {field} is not text"]
    lo, hi = LIMITS[field]
    out = [] if lo <= len(value) <= hi else [f"{where}: {field} must be {lo}-{hi} characters"]
    out += [f"{where}: {field} carries {what}" for rx, what in FORBIDDEN if rx.search(value)]
    return out


def validate(register: Any, catalogue: set[tuple[str, str]]) -> list[str]:
    """Every problem of the file (empty = loadable). ``catalogue`` = the (framework, ref) pairs of
    compliance.requirement."""
    if not isinstance(register, dict) or register.get("format") != 1:
        return ["the register file is not format 1"]
    controls, mappings = register.get("controls"), register.get("mappings")
    if not isinstance(controls, list) or not isinstance(mappings, list):
        return ["controls and mappings must be lists"]
    problems: list[str] = []
    ids: set[str] = set()
    for i, c in enumerate(controls):
        cid = c.get("control_id") if isinstance(c, dict) else None
        where = f"control {cid or i}"
        if not isinstance(cid, str) or not CONTROL_ID.fullmatch(cid):
            problems.append(f"{where}: control_id does not match {CONTROL_ID.pattern}")
            continue
        if cid in ids:
            problems.append(f"{where}: listed twice")
        ids.add(cid)
        for field in ("title", "intent", "owner_role"):
            problems += _text_problems(where, field, c.get(field))
        if not isinstance(c.get("domain"), str) or not DOMAIN.fullmatch(c["domain"]):
            problems.append(f"{where}: domain must match {DOMAIN.pattern}")
        if c.get("frequency") not in FREQUENCIES:
            problems.append(f"{where}: frequency must be one of {', '.join(FREQUENCIES)}")
        if c.get("test_type") not in TEST_TYPES:
            problems.append(f"{where}: test_type must be one of {', '.join(TEST_TYPES)}")
        if c.get("scope", "dev") not in SCOPES:
            problems.append(f"{where}: scope must be one of {', '.join(SCOPES)}")
        checks = c.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 20:
            problems.append(f"{where}: checks must be a list of at most 20")
            continue
        for ck in checks:
            if not (isinstance(ck, dict) and isinstance(ck.get("check_id"), str) and CHECK_ID.fullmatch(ck["check_id"])
                    and isinstance(ck.get("required", True), bool)):
                problems.append(f"{where}: a check needs check_id matching {CHECK_ID.pattern} and a boolean required")
    seen: set[tuple[str, str, str]] = set()
    for m in mappings:
        if not isinstance(m, dict):
            problems.append("a mapping is not an object")
            continue
        key = (m.get("control_id"), m.get("framework"), m.get("ref"))
        where = f"mapping {key}"
        if key[0] not in ids:
            problems.append(f"{where}: control not in the file")
        if key[1] not in FRAMEWORKS:
            problems.append(f"{where}: unknown framework")
        elif (key[1], key[2]) not in catalogue:
            problems.append(f"{where}: the requirement catalogue (CIS 466) has no such ref")
        if m.get("coverage") not in COVERAGE:
            problems.append(f"{where}: coverage must be full or partial")
        problems += _text_problems(where, "note", m.get("note"))
        if key in seen:
            problems.append(f"{where}: listed twice")
        seen.add(key)   # type: ignore[arg-type]
    return problems


def plan(register: dict, existing: set[str]) -> dict:
    ids = {c["control_id"] for c in register["controls"]}
    return {"insert": sorted(ids - existing), "update": sorted(ids & existing), "deactivate": sorted(existing - ids),
            "mappings": len(register["mappings"])}


SQL_UPSERT = """
INSERT INTO compliance.control (control_id, title, intent, domain, owner_role, frequency, test_type, checks, scope,
                                active)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
ON CONFLICT (control_id) DO UPDATE
   SET title = EXCLUDED.title, intent = EXCLUDED.intent, domain = EXCLUDED.domain, owner_role = EXCLUDED.owner_role,
       frequency = EXCLUDED.frequency, test_type = EXCLUDED.test_type, checks = EXCLUDED.checks, scope = EXCLUDED.scope,
       active = EXCLUDED.active, updated_at = now()
"""
SQL_DEACTIVATE = ("UPDATE compliance.control SET active = false, updated_at = now() "
                  "WHERE NOT (control_id = ANY($1::text[])) AND active")
SQL_DELETE_MAPPINGS = "DELETE FROM compliance.requirement_mapping WHERE control_id = ANY($1::text[])"
SQL_INSERT_MAPPING = ("INSERT INTO compliance.requirement_mapping (control_id, framework, ref, coverage, note) "
                      "VALUES ($1, $2, $3, $4, $5)")


async def apply(conn: Any, register: dict) -> None:
    ids = [c["control_id"] for c in register["controls"]]
    async with conn.transaction():
        for c in register["controls"]:
            checks = [{"check_id": k["check_id"], "required": k.get("required", True)} for k in c.get("checks", [])]
            await conn.execute(SQL_UPSERT, c["control_id"], c["title"], c.get("intent"), c["domain"], c["owner_role"],
                               c["frequency"], c["test_type"], json.dumps(checks), c.get("scope", "dev"),
                               bool(c.get("active", True)))
        await conn.execute(SQL_DEACTIVATE, ids)
        await conn.execute(SQL_DELETE_MAPPINGS, ids)
        for m in register["mappings"]:
            await conn.execute(SQL_INSERT_MAPPING, m["control_id"], m["framework"], m["ref"], m["coverage"],
                               m.get("note"))


def _read(args: argparse.Namespace) -> Any:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return json.load(fh)
    with urllib.request.urlopen(args.url, timeout=30) as resp:   # noqa: S310 - an https presigned URL
        return json.loads(resp.read(2 * 1024 * 1024))


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--url")
    ap.add_argument("--apply", action="store_true", help="write (default: DRY RUN)")
    ap.add_argument("--no-ssl", action="store_true", help="local test database only")
    args = ap.parse_args(argv)
    if args.url and not args.url.startswith("https://"):
        print("refused: --url must be https", file=sys.stderr)
        return 1
    register = _read(args)
    import asyncpg   # noqa: PLC0415 - validation and --help work without it

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=None if args.no_ssl else "require")
    try:
        catalogue = {(r["framework"], r["ref"]) for r in await conn.fetch(
            "SELECT framework, ref FROM compliance.requirement")}
        problems = validate(register, catalogue)
        if problems:
            print(json.dumps({"refused": problems[:50], "problems": len(problems)}, indent=1))
            return 1
        existing = {r["control_id"] for r in await conn.fetch("SELECT control_id FROM compliance.control")}
        out = {"mode": "apply" if args.apply else "dry_run", **plan(register, existing)}
        if args.apply:
            await apply(conn, register)
            out["controls_now"] = await conn.fetchval("SELECT count(*) FROM compliance.control WHERE active")
            out["mappings_now"] = await conn.fetchval("SELECT count(*) FROM compliance.requirement_mapping")
        print(json.dumps(out, indent=1))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

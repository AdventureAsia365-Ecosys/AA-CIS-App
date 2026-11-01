#!/usr/bin/env python3
"""AA-900 — the prompt registry gate (CI: tests/unit/test_aa900_prompt_registry_gate.py).

The manifest ``shared/llm_client/prompt_versions.json`` lists, per stage, its prompt versions (newest last) with the
source file, the symbols that hold the prompt and their hash (``prompt_registry.prompt_hash_of``: sha256 of the
symbols' source text). This script checks it against the source:

  python3 scripts/prompt_registry_check.py           # check; exit 1 when a prompt changed without a version bump
  python3 scripts/prompt_registry_check.py --bump    # append a new version for every stage whose prompt changed
  python3 scripts/prompt_registry_check.py --sql     # the INSERT a migration needs to register every manifest row
  python3 scripts/prompt_registry_check.py --sql --unregistered --created-by migration-NNN
                                                     # only the rows no file in api/migrations/ inserts yet

Rules (each failure names the stage and what to do):
  * the newest entry of each stage matches its source today (else: --bump, then register with a migration);
  * every file and symbol exists; (stage, version) and (stage, prompt_sha256) are unique; a stage's versions never
    repeat an older hash; stage / version / path match the shared.prompt_version CHECKs (migration 321);
  * an entry is never edited: older entries keep the hash they were registered with (only the newest is compared);
  * AA-642 D1 (gap OPS-03): every manifest entry is inserted into shared.prompt_version by a file in api/migrations/
    (migration 325 registered the first 32), so a bump without its migration fails here, not on Dev as a silent
    prompt_version NULL on the ledger.

No database, no network, no import of the prompt modules (ast only). Standard library + the repository.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "api" / "migrations"
sys.path.insert(0, str(ROOT))

from shared.llm_client.prompt_registry import (MANIFEST_PATH, PromptSourceError, load_manifest,  # noqa: E402
                                               prompt_hash_of)

STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,62}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


def problems(manifest: dict[str, list[dict]], root: Path = ROOT) -> list[str]:
    """Every rule broken by the manifest (empty = the gate passes). Pure apart from reading the source files."""
    out: list[str] = []
    for stage, entries in sorted(manifest.items()):
        if not STAGE_RE.match(stage):
            out.append(f"{stage}: stage key does not match {STAGE_RE.pattern}")
        versions, hashes = set(), set()
        for e in entries:
            v, sha, path = e.get("version", ""), e.get("prompt_sha256", ""), e.get("source_path", "")
            if not VERSION_RE.match(v):
                out.append(f"{stage} {v}: version does not match {VERSION_RE.pattern}")
            if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
                out.append(f"{stage} {v}: prompt_sha256 is not a sha256 hex")
            if len(path) > 300 or not PATH_RE.match(path) or ".." in path:
                out.append(f"{stage} {v}: source_path {path!r} is not a repository path")
            if v in versions:
                out.append(f"{stage} {v}: version listed twice")
            if sha in hashes:
                out.append(f"{stage} {v}: same prompt_sha256 as an older version (a version is a different prompt)")
            versions.add(v)
            hashes.add(sha)
        latest = entries[-1]
        try:
            current = prompt_hash_of(latest["source_path"], latest["symbols"], root)
        except PromptSourceError as exc:
            out.append(f"{stage} {latest['version']}: {exc} (update the manifest entry's source with a new version)")
            continue
        if current != latest["prompt_sha256"]:
            out.append(f"{stage}: the prompt in {latest['source_path']} ({', '.join(latest['symbols'])}) changed "
                       f"since version {latest['version']}. Bump it: python3 scripts/prompt_registry_check.py --bump, "
                       "then register the new row with a migration (--sql) and run the eval the PR template asks for")
    return out


# One VALUES tuple of an INSERT INTO shared.prompt_version: ('stage', 'version', '<sha256>', ...
_ROW_RE = re.compile(r"\(\s*'([a-z0-9][a-z0-9_]*)'\s*,\s*'([A-Za-z0-9][A-Za-z0-9._-]*)'\s*,\s*'([0-9a-f]{64})'")


def migration_rows(migrations_dir: Path = MIGRATIONS_DIR) -> set[tuple[str, str, str]]:
    """{(stage, version, prompt_sha256)} inserted into shared.prompt_version by the migration files (the tuples after
    an ``INSERT INTO shared.prompt_version`` in a file; a file that never names the table is not read further)."""
    out: set[tuple[str, str, str]] = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        start = text.find("INSERT INTO shared.prompt_version")
        if start < 0:
            continue
        out.update(_ROW_RE.findall(text[start:]))
    return out


def unregistered(manifest: dict[str, list[dict]], rows: set[tuple[str, str, str]]) -> list[dict]:
    """The manifest entries no migration inserts (stage, version and hash all equal), in manifest order."""
    return [e for stage, entries in sorted(manifest.items()) for e in entries
            if (stage, e["version"], e["prompt_sha256"]) not in rows]


def registration_problems(manifest: dict[str, list[dict]], migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """AA-642 D1: one problem per manifest entry that no migration registers (empty = every version is registered)."""
    return [f"{e['stage']} {e['version']}: no migration in api/migrations/ inserts it into shared.prompt_version. "
            "Add one: python3 scripts/prompt_registry_check.py --sql --unregistered --created-by migration-NNN"
            for e in unregistered(manifest, migration_rows(migrations_dir))]


def next_version(version: str) -> str:
    m = re.fullmatch(r"(.*?)(\d+)", version)
    return f"{m.group(1)}{int(m.group(2)) + 1}" if m else f"{version}.2"


def bump(raw: dict, root: Path = ROOT) -> list[str]:
    """Append a new version for every stage whose newest entry no longer matches its source. Returns the stages."""
    manifest: dict[str, list[dict]] = {}
    for e in raw["prompts"]:
        manifest.setdefault(e["stage"], []).append(e)
    bumped = []
    for stage, entries in manifest.items():
        latest = entries[-1]
        current = prompt_hash_of(latest["source_path"], latest["symbols"], root)
        if current != latest["prompt_sha256"]:
            raw["prompts"].append({**latest, "version": next_version(latest["version"]), "prompt_sha256": current})
            bumped.append(stage)
    return bumped


def sql(manifest: dict[str, list[dict]], created_by: str = "migration", only: Optional[list[dict]] = None) -> str:
    """The INSERT that registers every manifest row (``only``: just these entries)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", created_by):
        raise ValueError(f"created_by {created_by!r} is not a short label")
    wanted = None if only is None else {(e["stage"], e["version"]) for e in only}
    rows = [f"    ('{st}', '{e['version']}', '{e['prompt_sha256']}', '{e['source_path']}', '{created_by}')"
            for st, entries in sorted(manifest.items()) for e in entries
            if wanted is None or (st, e["version"]) in wanted]
    if not rows:
        return "-- every manifest row is already registered by a migration\n"
    return ("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) VALUES\n"
            + ",\n".join(rows) + "\nON CONFLICT (stage, version) DO NOTHING;\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--bump", action="store_true", help="append a new version for each changed prompt")
    group.add_argument("--sql", action="store_true", help="print the INSERT that registers every manifest row")
    ap.add_argument("--unregistered", action="store_true",
                    help="with --sql: only the rows no migration in api/migrations/ inserts yet")
    ap.add_argument("--created-by", default="migration", help="with --sql: the created_by label (migration-NNN)")
    ap.add_argument("--migrations", type=Path, default=MIGRATIONS_DIR)
    args = ap.parse_args(argv)
    if args.bump:
        raw = json.loads(args.manifest.read_text(encoding="utf-8"))
        bumped = bump(raw)
        args.manifest.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"bumped: {', '.join(bumped) or 'nothing'}")
        return 0
    manifest = load_manifest(args.manifest)
    if args.sql:
        only = unregistered(manifest, migration_rows(args.migrations)) if args.unregistered else None
        sys.stdout.write(sql(manifest, args.created_by, only))
        return 0
    found = problems(manifest) + registration_problems(manifest, args.migrations)
    for p in found:
        print(f"prompt-registry: {p}")
    print(f"prompt-registry: {sum(len(v) for v in manifest.values())} entries, {len(manifest)} stages, "
          f"{len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())

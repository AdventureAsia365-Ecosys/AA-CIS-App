"""Integrity check of an ``aa_llm`` copy against its own ``MANIFEST.json`` (AA-642 S8). Offline, standard library.

Every file the manifest lists must be present with that sha256, and the folder may hold no other file
(``__pycache__``, ``.pyc`` and dot files aside). A consumer also checks that its ``aa_llm.lock.json`` names this
manifest (``manifest_sha256``).

Usage: ``python3 <path>/aa_llm/check_copy.py [--lock <aa_llm.lock.json>] [--write]``. ``--write`` (canonical
AA-CIS-App only) rewrites the manifest from the files. Exit 0 ok, 1 a problem (each printed on its own line).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = "MANIFEST.json"
NAME = "aa_llm"
_IGNORED_DIRS = {"__pycache__"}


def _files(folder: Path) -> list[str]:
    out = []
    for p in sorted(folder.rglob("*")):
        parts = p.relative_to(folder).parts
        if p.is_file() and not any(x in _IGNORED_DIRS or x.startswith(".") for x in parts) and p.suffix != ".pyc":
            out.append(p.relative_to(folder).as_posix())
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(folder: Path = HERE) -> dict:
    files = {f: {"sha256": sha256(folder / f)} for f in _files(folder) if f != MANIFEST}
    return {"name": NAME, "adr": "root ADR 0009 decision 13", "files": files}


def manifest_text(folder: Path = HERE) -> str:
    return json.dumps(build_manifest(folder), indent=2, sort_keys=True) + "\n"


def problems(folder: Path = HERE, lock: Path | None = None) -> list[str]:
    """Every difference between the folder and its manifest (and the lock, when given)."""
    path = folder / MANIFEST
    if not path.is_file():
        return [f"{MANIFEST} is missing in {folder}"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        listed = {k: v["sha256"] for k, v in manifest["files"].items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f"{MANIFEST} is not a valid manifest: {exc}"]
    out = []
    present = set(_files(folder)) - {MANIFEST}
    for name in sorted(set(listed) - present):
        out.append(f"{name}: listed in {MANIFEST} but missing")
    for name in sorted(present - set(listed)):
        out.append(f"{name}: not listed in {MANIFEST} (a file was added to the copy)")
    for name in sorted(set(listed) & present):
        if sha256(folder / name) != listed[name]:
            out.append(f"{name}: sha256 differs from {MANIFEST} (the copy was edited; re-sync from AA-CIS-App)")
    if lock is not None:
        try:
            want = json.loads(lock.read_text(encoding="utf-8")).get("manifest_sha256")
        except (OSError, ValueError, AttributeError) as exc:
            return [*out, f"{lock.name} cannot be read: {exc}"]
        if want != sha256(path):
            out.append(f"{lock.name}: manifest_sha256 does not match {MANIFEST} (re-run the llm-core sync)")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--lock", type=Path, default=None)
    p.add_argument("--write", action="store_true", help="rewrite MANIFEST.json from the files (canonical copy only)")
    args = p.parse_args(argv)
    if args.write:
        (HERE / MANIFEST).write_text(manifest_text(HERE), encoding="utf-8")
        print(f"{NAME}: wrote {MANIFEST}")
        return 0
    found = problems(HERE, args.lock)
    for line in found:
        print(f"{NAME}: {line}", file=sys.stderr)
    if not found:
        print(f"{NAME}: copy matches {MANIFEST}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate shared/settings/compiled_defaults.json from the settings seed of the CIS migrations (S279 thresholds L1).

The seed of shared.setting_definition lives in the migrations, between the lines ``-- settings-seed:begin`` and
``-- settings-seed:end``, as one JSON array inside ``$seed$ … $seed$`` (CIS 407 is the first). Migrations are read in
number order and a later row of a key replaces an earlier one (a later file changes a definition with ON CONFLICT DO
UPDATE). The reader (shared/settings) serves these defaults when the database cannot be read, so they must equal the
seed: ``--check`` exits 1 when the committed file differs (tests/unit/test_s279_settings_seed_parity.py runs it in CI).

    python3 scripts/settings/gen_compiled_defaults.py           # rewrite the file
    python3 scripts/settings/gen_compiled_defaults.py --check   # CI parity check
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "api" / "migrations"
OUT = ROOT / "shared" / "settings" / "compiled_defaults.json"
BLOCK_RE = re.compile(r"--\s*settings-seed:begin.*?\$seed\$(.*?)\$seed\$.*?--\s*settings-seed:end", re.S)
FILE_RE = re.compile(r"^(\d{3})_.*\.sql$")
FIELDS = ("key", "app", "area", "label", "description", "value_type", "unit", "min_value", "max_value",
          "allowed_values", "default_value", "scope_kind", "impact", "approval", "evidence_required", "env_override",
          "cache_s", "enforced")


def seed_rows(migrations: Path = MIGRATIONS) -> tuple[list[dict], list[str]]:
    rows: dict[str, dict] = {}
    files = sorted((p for p in migrations.glob("*.sql") if FILE_RE.match(p.name)), key=lambda p: p.name)
    sources = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        blocks = BLOCK_RE.findall(text)
        if not blocks:
            continue
        sources.append(path.name)
        for block in blocks:
            for row in json.loads(block):
                missing = [f for f in FIELDS if f not in row]
                if missing:
                    raise SystemExit(f"{path.name}: seed row {row.get('key')} misses {missing}")
                rows[row["key"]] = {f: row[f] for f in FIELDS}
    return [rows[k] for k in sorted(rows)], sources


def render(migrations: Path = MIGRATIONS) -> str:
    rows, sources = seed_rows(migrations)
    doc = {"_generated": "scripts/settings/gen_compiled_defaults.py from the settings seed of " + ", ".join(sources)
                         + "; do not edit by hand",
           "definitions": rows}
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed file differs from the seed")
    a = ap.parse_args(argv)
    text = render()
    if a.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} differs from the migration seed: run "
                  "python3 scripts/settings/gen_compiled_defaults.py and commit the file", file=sys.stderr)
            return 1
        print("compiled settings defaults match the migration seed")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

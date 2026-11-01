#!/usr/bin/env python3
"""AA-1032 (faster CI): split a pytest suite into N shards by test FILE, balanced on recorded durations.

The CI matrix jobs ("Unit shard i/N", "Integration shard i/N") each run one shard; the aggregate jobs keep the
required check names ("Unit Tests", "Integration Tests"). Every shard computes its own list from the same checkout,
so the split is a pure function of (files on disk, durations file, N): no shard can drift from another.

    python3 .github/scripts/ci_shards.py files --shards 4 --index 1 \
        --durations .github/test-durations/integration.json --exclude tests/integration/test_007_rls_isolation.py \
        tests/integration
    python3 .github/scripts/ci_shards.py plan --shards 4 --durations ... tests/integration      # every shard
    python3 .github/scripts/ci_shards.py durations --out new.json junit-1.xml junit-2.xml ...    # refresh weights

Rules:
- Files are those pytest collects by default (``test_*.py`` / ``*_test.py``) under the given roots, minus
  ``--exclude``. Every file lands in exactly one shard (``plan`` checks it), so nothing is skipped by the split.
- Greedy longest-first: files sorted by (weight desc, path), each to the shard with the least load (ties: lower
  index). A file not in the durations file (a new test) weighs the median of the known ones.
- Weight of a file = its recorded seconds / ``--workers`` + ``--file-overhead``. With pytest-xdist every worker of a
  shard collects (imports) all of the shard's files while the tests themselves are spread over the workers, so a shard
  of many small files is slower than its test seconds say (unit suite: ~0.07 s of collection per file on a CI runner).
- A shard lists its files in path order, so within a shard pytest runs them in the same relative order as the
  unsharded run did (an integration file that depends on an earlier file's replay state keeps its predecessor order).
- The durations file is advisory: a stale one only unbalances the shards, never drops a file. Refresh it from the
  shards' junit reports (``durations``; the aggregate job uploads the merged file as an artifact).

Standard library only (runs before any install).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def discover(roots: list[str], exclude: list[str]) -> list[str]:
    """Test files under ``roots`` (pytest's default ``python_files``), POSIX paths relative to the cwd, sorted."""
    skip = {Path(e).as_posix() for e in exclude}
    found: set[str] = set()
    for root in roots:
        base = Path(root)
        if base.is_file():
            found.add(base.as_posix())
            continue
        if not base.is_dir():
            raise SystemExit(f"test root not found: {root}")
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                found.add(path.as_posix())
    return sorted(f for f in found if f not in skip)


def load_durations(path: str | None) -> dict[str, float]:
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items() if not str(k).startswith("_")}


def weights(files: list[str], durations: dict[str, float], workers: int = 1, overhead: float = 0.0) -> dict[str, float]:
    """Expected wall seconds a file adds to its shard (see the module docstring)."""
    if workers < 1 or overhead < 0:
        raise ValueError("workers must be >= 1 and the file overhead >= 0")
    known = [durations[f] for f in files if f in durations]
    default = statistics.median(known) if known else 1.0
    return {f: durations.get(f, default) / workers + overhead for f in files}


def plan(
    files: list[str], durations: dict[str, float], shards: int, workers: int = 1, overhead: float = 0.0
) -> list[list[str]]:
    """Greedy longest-processing-time split. Returns ``shards`` lists of files, each in path order."""
    if shards < 1:
        raise ValueError("shards must be >= 1")
    weight = weights(files, durations, workers, overhead)
    loads = [0.0] * shards
    buckets: list[list[str]] = [[] for _ in range(shards)]
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        i = min(range(shards), key=lambda k: (loads[k], k))
        buckets[i].append(f)
        loads[i] += weight[f]
    return [sorted(b) for b in buckets]


def estimate(bucket: list[str], weight: dict[str, float]) -> float:
    return sum(weight[f] for f in bucket)


def check_partition(files: list[str], buckets: list[list[str]]) -> None:
    seen = [f for b in buckets for f in b]
    if sorted(seen) != sorted(files) or len(seen) != len(set(seen)):
        raise SystemExit("shard split is not a partition of the test files")


def junit_durations(reports: list[str]) -> dict[str, float]:
    """Seconds per test file from pytest junit XML (``-o junit_family=xunit1`` writes the ``file`` attribute)."""
    out: dict[str, float] = {}
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            path = case.get("file")
            if not path:
                # xunit2 fallback: classname "tests.integration.test_x" or "tests.integration.test_x.TestClass".
                parts = (case.get("classname") or "").split(".")
                while parts and not parts[-1].startswith("test_") and not parts[-1].endswith("_test"):
                    parts.pop()
                if not parts:
                    continue
                path = "/".join(parts) + ".py"
            out[path] = round(out.get(path, 0.0) + float(case.get("time") or 0.0), 2)
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("files", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--shards", type=int, required=True)
        p.add_argument("--durations")
        p.add_argument("--exclude", action="append", default=[])
        p.add_argument("--workers", type=int, default=1, help="pytest-xdist workers per shard (-n)")
        p.add_argument("--file-overhead", type=float, default=0.0, help="seconds of collection per file and worker")
        p.add_argument("roots", nargs="+")
        if name == "files":
            p.add_argument("--index", type=int, required=True, help="1-based shard index")
    d = sub.add_parser("durations")
    d.add_argument("--out", required=True)
    d.add_argument("reports", nargs="+")
    args = ap.parse_args(argv)

    if args.cmd == "durations":
        reports = [r for r in args.reports if Path(r).is_file()]
        if not reports:
            print("no junit report found (a shard failed before writing one)")
            return 0
        for report in reports:
            per_file = junit_durations([report])
            print(f"- {Path(report).name}: {len(per_file)} files, {sum(per_file.values()):.0f}s of tests")
        merged = junit_durations(reports)
        Path(args.out).write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"- all: {len(merged)} files, {sum(merged.values()):.0f}s -> {args.out}")
        return 0

    files = discover(args.roots, args.exclude)
    if not files:
        raise SystemExit("no test files found")
    durations = load_durations(args.durations)
    buckets = plan(files, durations, args.shards, args.workers, args.file_overhead)
    check_partition(files, buckets)
    if args.cmd == "files":
        if not 1 <= args.index <= args.shards:
            raise SystemExit(f"--index must be 1..{args.shards}")
        bucket = buckets[args.index - 1]
        if not bucket:
            raise SystemExit(f"shard {args.index}/{args.shards} is empty: lower the shard count")
        print("\n".join(bucket))
        return 0
    weight = weights(files, durations, args.workers, args.file_overhead)
    unknown = [f for f in files if f not in durations]
    print(f"{len(files)} files in {args.shards} shards ({len(unknown)} without a recorded duration)")
    for i, bucket in enumerate(buckets, 1):
        print(f"shard {i}/{args.shards}: {len(bucket)} files, ~{estimate(bucket, weight):.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

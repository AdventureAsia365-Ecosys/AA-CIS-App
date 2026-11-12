"""AA-1081 (S278) — the read-only cache-miss check (scripts/quality/diag_aa1081_debate_cache_miss.py): the pure cause
order and per-tenant verdict over invented facts, and the script only reads (no write statement, read-only session,
no import outside asyncpg + stdlib, so it runs as one file in the api container)."""
from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.quality import diag_aa1081_debate_cache_miss as diag

WINDOW = datetime(2001, 1, 10, tzinfo=timezone.utc)
OLD = datetime(2001, 1, 1, tzinfo=timezone.utc)
NEW = datetime(2001, 1, 15, tzinfo=timezone.utc)


def _f(**kw):
    base = {"segment_id": "zz_seg", "segment_created_at": OLD, "merged_in_window": False, "older_versions": [],
            "cleared_by_atomize": False}
    return {**base, **kw}


def test_cause_order():
    # an older row under another brand version wins: a clear would have removed it
    assert diag.classify_segment(_f(older_versions=[1], cleared_by_atomize=True), WINDOW) == "brand_version"
    assert diag.classify_segment(_f(segment_created_at=NEW, cleared_by_atomize=True), WINDOW) == \
        "new_or_merged_segment"
    assert diag.classify_segment(_f(merged_in_window=True), WINDOW) == "new_or_merged_segment"
    assert diag.classify_segment(_f(segment_created_at=None), WINDOW) == "new_or_merged_segment"
    assert diag.classify_segment(_f(cleared_by_atomize=True), WINDOW) == "cleared_by_atomize"
    assert diag.classify_segment(_f(), WINDOW) == "not_cached_before"


def test_summary_verdict_and_brand_change():
    facts = [_f(cleared_by_atomize=True), _f(cleared_by_atomize=True), _f(older_versions=[1])]
    out = diag.summarize(facts, WINDOW, current_version=2, versions_before_run=[1, 2])
    assert out == {"judged_segments": 3, "verdict": "cleared_by_atomize", "brand_version_changed": False,
                   "by_cause": {"brand_version": 1, "new_or_merged_segment": 0, "cleared_by_atomize": 2,
                                "not_cached_before": 0}}
    # the version Debate keys on now had no rows before the run: a bump
    assert diag.summarize(facts, WINDOW, 3, [1, 2])["brand_version_changed"] is True
    # no rows before the run at all: nothing to compare against
    assert diag.summarize(facts, WINDOW, 3, [])["brand_version_changed"] is False
    # a tie goes to the cause listed first
    tie = diag.summarize([_f(older_versions=[1]), _f(cleared_by_atomize=True)], WINDOW, 2, [1])
    assert tie["verdict"] == "brand_version"
    assert diag.summarize([], WINDOW, 2, [])["verdict"] == "no_misses"


def test_script_only_reads():
    src = Path(diag.__file__).read_text(encoding="utf-8")
    sql = " ".join(v for k, v in vars(diag).items() if k.endswith("_SQL"))
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", sql, re.I)
    assert '"default_transaction_read_only": "on"' in src
    imported = {n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
                for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert imported <= {"__future__", "argparse", "asyncio", "json", "os", "sys", "collections", "datetime",
                        "typing", "asyncpg"}


def test_args():
    a = diag.parse_args(["--run-start", "2001-01-20T00:00:00Z", "--tenant", "t1", "--tenant", "t2"])
    assert a.run_start == "2001-01-20T00:00:00Z" and a.tenant == ["t1", "t2"] and a.since == diag.DEFAULT_SINCE

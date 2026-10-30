"""AA-923 (WM-34) — the two old learning paths stay retired.

The AA-132 lessons loop (`shared.pipeline_lessons`, frozen by AA-915) and the N8 output rules
(`acp_shared.acp_output_rules`, `/v1/rules`, `api/services/acp_post_processor.py`,
`services/acp_produce/rule_adapter.py`) were removed; learned rules reach a writer only through a writing-memory
release (app ADR 0010). This guard replaces the AA-915 freeze test: no runtime file may query either table again,
the modules stay gone, and the flag-fix lessons hook stays a no-op that sends no SQL.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIRS = ("api", "services", "shared", "scripts", "worker")
# The read-only census run before the quarantine (destructive-ops step 1) is the one file allowed to name them.
ALLOWED = {"scripts/aa923/wm34_dry_run.py"}
SQL_FORM = re.compile(r"\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+[\w.\"]*\b(pipeline_lessons|acp_output_rules)\b", re.I)


def _runtime_files():
    for d in RUNTIME_DIRS:
        base = ROOT / d
        if base.is_dir():
            yield from (p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_runtime_file_queries_a_retired_table():
    hits = []
    for p in _runtime_files():
        if str(p.relative_to(ROOT)) in ALLOWED:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if SQL_FORM.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, "retired tables queried again (AA-923 WM-34):\n" + "\n".join(hits)


@pytest.mark.parametrize("module", [
    "services.content_generation.pipeline_lessons",
    "api.services.acp_post_processor",
    "services.acp_produce.rule_adapter",
    "api.routers.v1_rules",
])
def test_retired_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_flag_fix_lessons_hook_is_a_no_op():
    from services.content_generation import flag_fix_node

    assert not hasattr(flag_fix_node, "write_lessons_log")
    with patch("asyncpg.connect", side_effect=AssertionError("no DB call")):
        assert flag_fix_node._write_lessons_safe([{"field": "title", "pattern": "x"}], {"tour": {}}) is None

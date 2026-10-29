"""AA-900 — the LLM prompt register (docs/llm/prompt-register.md) stays true to the code.

Every row names a file that exists and symbols defined in it (a constant or a function), a known gateway stage and a
role the gateway accepts; prompt ids are unique; every stage with a prompt in the SAFE_DEFAULTS route table has a row.
Static text checks only: no import of the prompt modules, no DB, no model.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "llm" / "prompt-register.md"
ROLE_CONFIG = ROOT / "shared" / "llm_client" / "role_config.py"

ROLES = {"writer", "judge", "validate"}
# Stages of SAFE_DEFAULTS that send no prompt of their own (register "Not in the register").
NO_PROMPT_STAGES = {"f10_embed", "a3_place_embed", "s1_atom_writer"}
# Gateway stages with a prompt that are not in SAFE_DEFAULTS (their route row exists only in the DB).
EXTRA_STAGES = {"a3_search_demand"}


def _rows() -> list[dict]:
    text = REGISTER.read_text(encoding="utf-8")
    m = re.search(r"<!-- register:start -->(.*?)<!-- register:end -->", text, re.S)
    assert m, "register markers missing"
    lines = [ln for ln in m.group(1).strip().splitlines() if ln.startswith("|")]
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    assert header == ["Prompt id", "Stage", "Role", "File · symbol", "Version", "Eval baseline"]
    out = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        assert len(cells) == len(header), ln
        out.append(dict(zip(header, cells)))
    return out


def _ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _safe_default_stages() -> set[str]:
    src = ROLE_CONFIG.read_text(encoding="utf-8")
    return set(re.findall(r'^\s*"([a-z0-9_]+)":\s+StageConfig\(', src, re.M))


ROWS = _rows()


def test_register_has_rows_and_unique_ids():
    ids = [_ticks(r["Prompt id"])[0] for r in ROWS]
    assert len(ids) >= 30
    assert len(ids) == len(set(ids)), "duplicate prompt id"


@pytest.mark.parametrize("row", ROWS, ids=lambda r: _ticks(r["Prompt id"])[0])
def test_row_points_at_real_code(row):
    file_cell, _, sym_cell = row["File · symbol"].partition("·")
    files = _ticks(file_cell)
    assert len(files) == 1, row
    path = ROOT / files[0]
    assert path.is_file(), f"{files[0]} does not exist"
    src = path.read_text(encoding="utf-8")
    symbols = _ticks(sym_cell)
    assert symbols, row
    for sym in symbols:
        defined = re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(sym)}\s*\(|^{re.escape(sym)}\s*=", src, re.M)
        assert defined, f"{sym} is not defined in {files[0]}"
    assert row["Role"] in ROLES, row
    known = _safe_default_stages() | EXTRA_STAGES
    for stage in _ticks(row["Stage"]):
        assert stage in known, f"unknown stage {stage}"


def test_every_prompt_stage_of_the_route_defaults_has_a_row():
    covered = {s for r in ROWS for s in _ticks(r["Stage"])}
    missing = _safe_default_stages() - NO_PROMPT_STAGES - covered
    assert not missing, f"stages without a register row: {sorted(missing)}"

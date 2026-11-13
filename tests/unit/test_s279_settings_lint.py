"""S279 thresholds L1 — settings-lint, the CI guard of the settings registry (root audit
docs/audits/2026-11-13-S279-session/thresholds-audit/ section 5.4; tech lead S279: a threshold, limit, cap or policy
value is a setting, never a code literal or an env var alone).

1. **No new policy constant in a governed module.** In the modules of ``GOVERNED`` (quality, content writing, slate,
   cost guard, jobs, retention, publish, writing memory, T2, A1, places backfill, research, lifecycle), a module-level
   UPPER_CASE constant whose name looks like policy (``*_MAX``, ``*_MIN``, ``*_CAP``, ``*_LIMIT``, ``*_DAYS``,
   ``*_HOURS``, ``*_PCT``, ``*_THRESHOLD``, ``*_USD``, ``*_ATTEMPTS``, ``*_RETRIES``, ...) bound to a number literal
   fails, unless its line carries ``# technical-constant: <reason>`` or it is in the baseline of the constants that
   were there on 13/11/2026 (``snapshots/s279_settings_lint_baseline.json``, a list that only shrinks). A numeric
   ``os.environ`` / ``getenv`` read under a policy-looking name fails the same way. Fix: add a setting (a migration
   seed row + ``shared.settings.get``), or mark a true engineering constant.
2. **No dead settings.** Every compiled CIS key (app ``cis``) with ``enforced`` true is named by code outside
   shared/settings (a reader), and every ``settings.get / aget / definition / resolve("cis....")`` literal is a compiled
   key. Another app's keys (TripPlanner, app ``tripplanner``, CIS 414) are read by that app through its catalog view
   (CIS 415); its own repo checks its readers (AA-TripPlanner-Web backend/tests/test_s279_settings.py).
3. **The baseline only shrinks.** A baseline entry whose constant is gone (or now reads a setting) fails until it is
   removed from the file.

No DB. Regenerate the baseline only to remove entries: ``python -m tests.unit.test_s279_settings_lint``."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from shared import settings

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "snapshots" / "s279_settings_lint_baseline.json"

GOVERNED = (
    "services/quality", "services/acp_content_writing", "services/acp_shared/slate.py",
    "services/acp_shared/blog_seed.py", "services/acp_shared/question_topup.py", "shared/cost_guard.py",
    "shared/jobs", "services/retention", "services/acp_publish", "services/writing_memory/config.py",
    "services/content_generation/t2_orchestrator.py", "services/content_generation/graph.py",
    "services/places/backfill.py", "services/jobs/segment_research_job.py", "services/jobs/retention_job.py",
    "services/jobs/erasure_job.py", "services/tour_lifecycle",
)
POLICY_NAME = re.compile(
    r"(^|_)(MAX|MIN|CAP|LIMIT|LIMITS|DAYS|HOURS|PCT|PERCENT|THRESHOLD|THRESHOLDS|USD|ATTEMPTS|RETRIES|QUOTA|BUDGET|"
    r"FLOOR|CEILING|TTL)(_|$)")
MARK = "# technical-constant:"
READ_CALL = re.compile(r"""\b(?:settings|_settings|platform_settings|_platform_settings)"""
                       r"""\.(?:get|aget|definition|resolve)\(\s*["'](cis\.[a-z0-9_.]+)["']""")


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in GOVERNED:
        p = REPO / rel
        out.extend([p] if p.suffix == ".py" else sorted(p.rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts]


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _env_read(node: ast.AST) -> bool:
    """int(os.environ.get(...)) / float(os.getenv(...)) and the like: a numeric env read."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("int", "float")):
        return False
    return any(isinstance(n, ast.Attribute) and n.attr in ("get", "getenv", "environ") for n in ast.walk(node))


def findings() -> list[str]:
    """Every policy-looking numeric constant or numeric env read at module level of a governed file, unmarked."""
    out = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.parse(text).body:
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for t in targets:
                if not (isinstance(t, ast.Name) and t.id.isupper() and POLICY_NAME.search(t.id)):
                    continue
                if not (_is_number(value) or _env_read(value)):
                    continue
                if MARK in lines[node.lineno - 1]:
                    continue
                out.append(f"{path.relative_to(REPO).as_posix()}:{t.id}")
    return sorted(set(out))


def _baseline() -> list[str]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["constants"]


def test_no_new_policy_constant_in_a_governed_module():
    new = sorted(set(findings()) - set(_baseline()))
    assert not new, ("policy-looking constants with no setting (make each a setting in the registry, CIS 407 pattern, "
                     f"or mark '{MARK} <reason>'): {new}")


def test_the_baseline_only_shrinks():
    stale = sorted(set(_baseline()) - set(findings()))
    assert not stale, f"remove these from {BASELINE.name} (python -m tests.unit.test_s279_settings_lint): {stale}"


def _code_text() -> str:
    parts = []
    for top in ("api", "services", "shared", "worker"):
        for p in (REPO / top).rglob("*.py"):
            if "__pycache__" in p.parts or p.parent == REPO / "shared" / "settings":
                continue
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_every_enforced_setting_has_a_reader_and_every_read_key_exists():
    code = _code_text()
    dead = [k for k in settings.READER.keys()
            if settings.definition(k).app == "cis" and settings.definition(k).enforced and f'"{k}"' not in code]
    assert not dead, f"settings with no reader in code (wire them, or seed enforced=false with an issue): {dead}"
    unknown = sorted({k for k in READ_CALL.findall(code)} - set(settings.READER.keys()))
    assert not unknown, f"code reads settings that are not in the compiled defaults: {unknown}"


def test_the_lint_catches_a_new_cap(tmp_path, monkeypatch):
    mod = tmp_path / "services" / "zz_lint"
    mod.mkdir(parents=True)
    (mod / "m.py").write_text('import os\nRUN_CAP_USD = 5.0\nPOOL_SIZE = 3\nWAIT_DAYS = 2  # technical-constant: x\n'
                              'RETRY_MAX = int(os.environ.get("ZZ", "3"))\nNAME_MAX = "x"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "GOVERNED", ("services/zz_lint",))
    assert findings() == ["services/zz_lint/m.py:RETRY_MAX", "services/zz_lint/m.py:RUN_CAP_USD"]


if __name__ == "__main__":  # rewrite the baseline to the current findings (only to remove entries)
    current = findings()
    BASELINE.write_text(json.dumps({"_doc": "S279 settings-lint baseline: policy-looking constants of the governed "
                                            "modules on 13/11/2026 that are not settings yet. Only shrinks.",
                                    "constants": current}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE.relative_to(REPO)} ({len(current)} entries)")

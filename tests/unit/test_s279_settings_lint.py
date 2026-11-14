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
4. **The rest of the code is a ratchet** (AA-1096 follow-up, S280; the pattern of AA-Booking, AA-Partner and
   AA-TripPlanner-Web). The same scan runs over every Python module of ``api/``, ``services/``, ``shared/`` and
   ``worker/`` (:data:`SCANNED`) outside :data:`GOVERNED`, minus tests, scripts and migrations
   (:data:`EXCLUDED_PARTS`). A finding there fails unless it is marked or listed in its own baseline of 14/11/2026
   (``snapshots/aa1096_settings_lint_rest_baseline.json``: unclassified, a list that only shrinks, keyed
   ``<path>:<NAME>`` with no line number). The governed modules stay stricter: the rest baseline never lists one of
   them, so a new governed constant can only enter through rule 1 (a setting or the marker). AA-CIS-App holds no
   vendored copy; it is the owner of the copies the other repos vendor (:data:`COPY_SOURCES`: ``shared/aa_llm``, the
   identity verifier, the settings reader, the job table shape). They are scanned like any other module (the job
   table shape under rule 1, it sits in governed ``shared/jobs``; the rest under rule 4, their findings in the rest
   baseline). A marker added to one changes the byte copy of every consumer, so it ships with the copy refresh, not
   on its own: :data:`COPY_SOURCE_MARKS` pins the marked lines of the copy sources, and a new one fails until it is
   listed there in the same change that refreshes the consumers' copies.

No DB. Regenerate both baselines only to remove entries: ``python -m tests.unit.test_s279_settings_lint``. CI: the
"Unit Tests" job runs this file."""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from shared import settings

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().parent / "snapshots" / "s279_settings_lint_baseline.json"
REST_BASELINE = Path(__file__).resolve().parent / "snapshots" / "aa1096_settings_lint_rest_baseline.json"

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
# Rule 4: the packages scanned outside GOVERNED, and the directory names never scanned (not runtime policy code).
SCANNED = ("api", "services", "shared", "worker")
EXCLUDED_PARTS = frozenset({"tests", "scripts", "migrations", "__pycache__"})
# The canonical sources of the byte copies vendored by AA-Booking, AA-Partner and AA-TripPlanner-Web (their
# check_vendored_copies / *.lock.json): scanned here, never marked on their own (see rule 4).
COPY_SOURCES = ("shared/aa_llm/", "api/core/identity_verifier.py", "shared/settings/reader.py",
                "shared/jobs/job_table_shape.py")
# The marked lines of the copy sources on 14/11/2026, keyed ``<path>:<NAME>`` (see rule 4). Grows only with a copy
# refresh in every consumer (AA-Booking, AA-Partner, AA-TripPlanner-Web) in the same change.
COPY_SOURCE_MARKS = frozenset({"shared/settings/reader.py:REFRESH_TIMEOUT_S"})
_ASSIGNED_NAME = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=")
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


def rest_files() -> list[Path]:
    """Rule 4 scope: every module of SCANNED outside GOVERNED, tests, scripts and migrations."""
    governed = set(_files())
    out = []
    for top in SCANNED:
        for p in sorted((REPO / top).rglob("*.py")):
            if EXCLUDED_PARTS & set(p.relative_to(REPO).parts) or p in governed:
                continue
            out.append(p)
    return out


def findings(files: list[Path] | None = None) -> list[str]:
    """Every policy-looking numeric constant or numeric env read at module level of the files (default: governed),
    unmarked, as ``<path>:<NAME>``."""
    out = []
    for path in _files() if files is None else files:
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


def rest_findings() -> list[str]:
    return findings(rest_files())


def _baseline(path: Path = BASELINE) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["constants"]


def test_no_new_policy_constant_in_a_governed_module():
    new = sorted(set(findings()) - set(_baseline()))
    assert not new, ("policy-looking constants with no setting (make each a setting in the registry, CIS 407 pattern, "
                     f"or mark '{MARK} <reason>'): {new}")


def test_the_baseline_only_shrinks():
    stale = sorted(set(_baseline()) - set(findings()))
    assert not stale, f"remove these from {BASELINE.name} (python -m tests.unit.test_s279_settings_lint): {stale}"


def test_no_new_policy_constant_in_the_rest_of_the_code():
    new = sorted(set(rest_findings()) - set(_baseline(REST_BASELINE)))
    assert not new, ("policy-looking constants with no setting (make each a setting in the registry, CIS 407 pattern, "
                     f"or mark '{MARK} <reason>' when it is an engineering constant): {new}")


def test_the_rest_baseline_only_shrinks():
    stale = sorted(set(_baseline(REST_BASELINE)) - set(rest_findings()))
    assert not stale, f"remove these from {REST_BASELINE.name} (python -m tests.unit.test_s279_settings_lint): {stale}"


def test_the_rest_baseline_never_lists_a_governed_module():
    governed = {p.relative_to(REPO).as_posix() for p in _files()}
    listed = sorted(e for e in _baseline(REST_BASELINE) if e.rsplit(":", 1)[0] in governed)
    assert not listed, f"governed modules have no rest baseline (rule 1 decides them): {listed}"


def test_the_rest_scope():
    rest = {p.relative_to(REPO).as_posix() for p in rest_files()}
    governed = {p.relative_to(REPO).as_posix() for p in _files()}
    assert rest and not rest & governed
    assert {"api/main.py", "api/lambda/s4_trigger/handler.py", "shared/settings/reader.py", "worker/__main__.py",
            "api/core/identity_verifier.py", "shared/aa_llm/budget.py"} <= rest
    assert not any(EXCLUDED_PARTS & set(r.split("/")) for r in rest), "tests, scripts or migrations scanned"
    assert all((REPO / src).exists() for src in COPY_SOURCES), "a copy source moved: update COPY_SOURCES"
    assert "shared/jobs/job_table_shape.py" in governed, "the job table shape copy source left the governed modules"


def _copy_source_files() -> list[Path]:
    out: list[Path] = []
    for src in COPY_SOURCES:
        p = REPO / src
        out.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p] if p.is_file() else [])  # gone: test_the_rest_scope
    return [p for p in out if "__pycache__" not in p.parts]


def copy_source_marks() -> list[str]:
    """Every line of a copy source carrying the marker, as ``<path>:<NAME>`` (the assigned name, else the line)."""
    out = []
    for path in _copy_source_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            if MARK in line:
                m = _ASSIGNED_NAME.match(line)
                out.append(f"{path.relative_to(REPO).as_posix()}:{m.group(1) if m else line.strip()}")
    return sorted(set(out))


def test_no_copy_source_is_marked_on_its_own():
    new = sorted(set(copy_source_marks()) - COPY_SOURCE_MARKS)
    assert not new, ("a marker in a file the other repos vendor as a byte copy breaks their copy check: refresh the "
                     f"copies in AA-Booking, AA-Partner and AA-TripPlanner-Web in the same change and list it in "
                     f"COPY_SOURCE_MARKS, or leave the constant in the rest baseline: {new}")
    gone = sorted(COPY_SOURCE_MARKS - set(copy_source_marks()))
    assert not gone, f"remove these from COPY_SOURCE_MARKS: {gone}"


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


def test_the_rest_lint_catches_a_new_cap_outside_the_governed_modules(tmp_path, monkeypatch):
    planted = 'HOLD_DAYS = 7\nPAGE_MAX = 50  # technical-constant: page size\nLABEL_MAX = "x"\n'
    for rel in ("api/routers/zz.py", "services/zz_lint/m.py", "shared/zz.py", "api/tests/t.py",
                "services/scripts/s.py", "api/migrations/m.py", "frontend/x.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(planted, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "GOVERNED", ("services/zz_lint",))
    assert rest_findings() == ["api/routers/zz.py:HOLD_DAYS", "shared/zz.py:HOLD_DAYS"]
    assert findings() == ["services/zz_lint/m.py:HOLD_DAYS"]


def test_a_new_marker_in_a_copy_source_is_caught(tmp_path, monkeypatch):
    (tmp_path / "shared" / "settings").mkdir(parents=True)
    (tmp_path / "shared" / "settings" / "reader.py").write_text(
        "REFRESH_TIMEOUT_S = 3.0  # technical-constant: x\nSNAPSHOT_TTL_S: float = 5.0  # technical-constant: y\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "COPY_SOURCES", ("shared/aa_llm/", "shared/settings/reader.py"))
    assert copy_source_marks() == ["shared/settings/reader.py:REFRESH_TIMEOUT_S",
                                   "shared/settings/reader.py:SNAPSHOT_TTL_S"]
    assert set(copy_source_marks()) - COPY_SOURCE_MARKS == {"shared/settings/reader.py:SNAPSHOT_TTL_S"}


if __name__ == "__main__":  # rewrite both baselines to the current findings (only to remove entries)
    for path, current, doc in (
        (BASELINE, findings(), "S279 settings-lint baseline: policy-looking constants of the governed modules on "
                               "13/11/2026 that are not settings yet. Only shrinks."),
        (REST_BASELINE, rest_findings(), "AA-1096 settings-lint rule 4 baseline: policy-looking constants of api/, "
                                         "services/, shared/ and worker/ outside the governed modules on 14/11/2026, "
                                         "not classified yet. Only shrinks."),
    ):
        path.write_text(json.dumps({"_doc": doc, "constants": current}, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)} ({len(current)} entries)")

"""AA-1096 lane l5 (CIS 428) — settings-lint: an env override lives one release, then goes.

Tech lead S279: "env vars stay for one release as an emergency override, then go". CIS 428 retired the 25 overrides
CIS 407 / 411 / 412 / 418 had given (``snapshots/aa1096_retired_env_overrides.json``, ``{key: env var}``). These
rules keep the drift from coming back while the ``env_override`` mechanism itself stays for a future one-release
override:

1. **A retired override never comes back.** No compiled definition of a retired key names an env var, and no compiled
   definition names a retired env var (a new key cannot pick up an old switch).
2. **Every override has its window.** A compiled definition with an ``env_override`` must be listed in
   ``snapshots/aa1096_env_override_window.json`` (``{key: {"env", "added", "remove_by", "issue"}}``, dates
   ``YYYY-MM-DD``), with the same env var and at most :data:`MAX_WINDOW_DAYS` between ``added`` and ``remove_by``.
   Once ``remove_by`` has passed the test fails until a migration retires the override (a ``settings-edit`` block
   with ``"env_override": null``, the CIS 428 pattern) and the entry leaves the file; an entry whose definition no
   longer carries that override fails too (remove it).
3. **No runtime code names a retired env var.** In ``api/``, ``services/``, ``shared/``, ``worker/`` and ``scripts/``
   no string literal equals a retired env var or starts a retired per-class family (``T10_SA_MODE_`` /
   ``T10_ADJ_MODE_``): the reader serves the setting, so a direct read, a fingerprint entry or a script that sets the
   env var would act on nothing. The one exception is the per-job trial flag ``T9_LENGTH_EXTEND`` (a key of a job's
   ``trial_flags`` payload, never an env var: ``services/quality/trial_flags.py``), in :data:`ALLOWED_LITERALS`.

No DB. CI: the "Unit Tests" job runs this file (CIS has no separate settings-lint job)."""
from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest

from shared import settings

REPO = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
RETIRED = SNAPSHOTS / "aa1096_retired_env_overrides.json"
WINDOW = SNAPSHOTS / "aa1096_env_override_window.json"
# One release, with room for a release that slips a few days (S279 -> S280 took one day).
MAX_WINDOW_DAYS = 14  # technical-constant: the CI rule's window length (tech lead S279: one release)
SCANNED = ("api", "services", "shared", "worker", "scripts")
SKIPPED_PARTS = frozenset({"tests", "migrations", "__pycache__", "node_modules"})
RETIRED_FAMILIES = ("T10_SA_MODE_", "T10_ADJ_MODE_")
# path -> literals that equal a retired env var name but are not an env var there.
ALLOWED_LITERALS = {
    "services/quality/trial_flags.py": frozenset({"T9_LENGTH_EXTEND"}),       # the trial flag of a t9_write job
    "services/acp_content_writing/length_extend.py": frozenset({"T9_LENGTH_EXTEND"}),  # EXTEND_FLAG reads it
}


def retired() -> dict[str, str]:
    return json.loads(RETIRED.read_text(encoding="utf-8"))


def window() -> dict[str, dict]:
    return json.loads(WINDOW.read_text(encoding="utf-8"))


def _overrides(definitions=None) -> dict[str, str]:
    defs = definitions if definitions is not None else settings.load_definitions()
    return {d["key"]: d["env_override"] for d in defs if d.get("env_override")}


def retired_findings(definitions=None, gone: dict[str, str] | None = None) -> list[str]:
    gone = retired() if gone is None else gone
    names = set(gone.values())
    out = []
    for key, env in sorted(_overrides(definitions).items()):
        if key in gone:
            out.append(f"{key}: its env override was retired by CIS 428 and may not come back ({env})")
        elif env in names:
            out.append(f"{key}: {env} is a retired env override (CIS 428); name the key's own switch on the page")
    return out


def window_findings(definitions=None, listed: dict[str, dict] | None = None, today: date | None = None) -> list[str]:
    listed = window() if listed is None else listed
    today = today or date.today()
    overrides = _overrides(definitions)
    out = []
    for key, env in sorted(overrides.items()):
        entry = listed.get(key)
        if entry is None:
            out.append(f"{key}: env override {env} has no window in {WINDOW.name} (added, remove_by, issue)")
            continue
        if entry.get("env") != env:
            out.append(f"{key}: the window names {entry.get('env')}, the definition {env}")
        try:
            added, remove_by = date.fromisoformat(entry["added"]), date.fromisoformat(entry["remove_by"])
        except (KeyError, TypeError, ValueError):
            out.append(f"{key}: the window needs added and remove_by as YYYY-MM-DD")
            continue
        if not entry.get("issue"):
            out.append(f"{key}: the window needs the issue that retires it")
        if (remove_by - added).days > MAX_WINDOW_DAYS:
            out.append(f"{key}: a window of {(remove_by - added).days} days is longer than one release "
                       f"({MAX_WINDOW_DAYS} days)")
        if today > remove_by:
            out.append(f"{key}: the one release of {env} ended on {remove_by}: retire it with a migration "
                       "(settings-edit block, env_override null) and drop the window entry")
    for key in sorted(set(listed) - set(overrides)):
        out.append(f"{key}: the window lists an override the definition no longer has: remove the entry")
    return out


def _runtime_files() -> list[Path]:
    files = []
    for top in SCANNED:
        for path in sorted((REPO / top).rglob("*.py")):
            if not SKIPPED_PARTS.intersection(path.relative_to(REPO).parts):
                files.append(path)
    return files


def literal_findings(files: list[Path] | None = None, gone: dict[str, str] | None = None) -> list[str]:
    names = set((retired() if gone is None else gone).values())
    out = []
    for path in files if files is not None else _runtime_files():
        rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name
        allowed = ALLOWED_LITERALS.get(rel, frozenset())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in docstrings:
                continue
            v = node.value.strip()
            if v in allowed:
                continue
            if v in names or (v.startswith(RETIRED_FAMILIES) and v.isupper()) or v in {f.rstrip("_") for f in
                                                                                         RETIRED_FAMILIES}:
                out.append(f"{rel}:{node.lineno}: {v!r} is a retired env override (CIS 428): read the setting")
    return out


def test_no_retired_override_comes_back():
    assert retired_findings() == []


def test_every_env_override_has_a_one_release_window():
    assert window_findings() == []


def test_no_runtime_code_names_a_retired_env_var():
    assert literal_findings() == []


def test_the_retired_list_names_the_cis_428_keys():
    gone = retired()
    assert len(gone) == 25 and all(k in settings.READER.keys() for k in gone)
    assert gone["cis.erasure.tenant_retention_days"] == "ERASURE_TENANT_RETENTION_DAYS"
    assert gone["cis.t9.blog_length_extend"] == "T9_LENGTH_EXTEND"
    assert all(settings.definition(k).env_override is None for k in gone)


# ── the rules catch what they are for (invented keys and names) ─────────────────────────────────────────────────────

def _defn(key: str, env: str | None) -> dict:
    return {"key": key, "env_override": env}


def test_a_retired_override_put_back_is_caught():
    gone = {"cis.zz.alpha_mode": "ZZ_ALPHA_MODE"}
    assert retired_findings([_defn("cis.zz.alpha_mode", "ZZ_ALPHA_MODE")], gone)
    assert retired_findings([_defn("cis.zz.beta_mode", "ZZ_ALPHA_MODE")], gone)
    assert retired_findings([_defn("cis.zz.beta_mode", "ZZ_BETA_MODE")], gone) == []


@pytest.mark.parametrize("entry,today,ok", [
    ({"env": "ZZ_GAMMA_CAP", "added": "2026-11-01", "remove_by": "2026-11-10", "issue": "AA-1"}, "2026-11-05", True),
    ({"env": "ZZ_GAMMA_CAP", "added": "2026-11-01", "remove_by": "2026-11-10", "issue": "AA-1"}, "2026-11-11", False),
    ({"env": "ZZ_GAMMA_CAP", "added": "2026-11-01", "remove_by": "2026-12-20", "issue": "AA-1"}, "2026-11-05", False),
    ({"env": "ZZ_OTHER", "added": "2026-11-01", "remove_by": "2026-11-10", "issue": "AA-1"}, "2026-11-05", False),
    ({"env": "ZZ_GAMMA_CAP", "added": "2026-11-01", "remove_by": "2026-11-10"}, "2026-11-05", False),
    ({"env": "ZZ_GAMMA_CAP", "added": "11/01/2026", "remove_by": "2026-11-10", "issue": "AA-1"}, "2026-11-05", False),
])
def test_the_window_rule(entry, today, ok):
    found = window_findings([_defn("cis.zz.gamma_cap", "ZZ_GAMMA_CAP")], {"cis.zz.gamma_cap": entry},
                            date.fromisoformat(today))
    assert (found == []) is ok, found


def test_an_override_without_a_window_and_a_stale_entry_are_caught():
    assert window_findings([_defn("cis.zz.delta_cap", "ZZ_DELTA_CAP")], {}, date(2026, 11, 5))
    stale = {"cis.zz.delta_cap": {"env": "ZZ_DELTA_CAP", "added": "2026-11-01", "remove_by": "2026-11-10",
                                  "issue": "AA-1"}}
    assert window_findings([_defn("cis.zz.delta_cap", None)], stale, date(2026, 11, 5))


def test_a_runtime_literal_of_a_retired_name_is_caught(tmp_path):
    src = tmp_path / "zz_reader.py"
    src.write_text('"""Mentions ZZ_EPSILON_MODE in a docstring: fine."""\nimport os\n'
                   'MODE = os.environ.get("ZZ_EPSILON_MODE")\nFAMILY = "T10_SA_MODE_" + "X"\nOK = "zz text"\n',
                   encoding="utf-8")
    found = literal_findings([src], {"cis.zz.epsilon_mode": "ZZ_EPSILON_MODE"})
    assert len(found) == 2 and "ZZ_EPSILON_MODE" in found[0] and "T10_SA_MODE_" in found[1]

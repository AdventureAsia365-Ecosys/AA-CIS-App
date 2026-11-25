"""AA-1111 (S294) — the admin app's (frontend/) dependency floor and its npm audit gate.

1. ``next`` is at least 16.4.6 (the November 2026 security release, the version the Booking admin and the CRM web run).
2. ``@sentry/nextjs`` is pinned to an exact 11.x: 10.x pulls ``glob`` < 14.0.2 (GHSA-3g3m-2fjr-6pw4) and ``minimatch``
   < 11.0.1 (GHSA-g3hv-75wg-m249) through its bundler plugins. Sentry 11 serves the build wrapper from
   ``@sentry/nextjs/config``; importing it from the package root is what broke the build of a Dependabot bump
   (``withSentryConfig is not a function``).
3. The lockfile resolves every ``zod`` to 4.6.6 or later (GHSA-mrhx-c7xc-m3w5, high) and every ``glob`` 9.x-14.x and
   ``minimatch`` 7.x-11.x to a patched version.
4. CI's "Security Audit" job runs ``npm audit`` in frontend/ through ``.github/scripts/npm_audit_gate.py`` with the
   allowlist, so a new high or critical advisory fails the pull request.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.match(r"\d+(?:\.\d+)*", text).group(0).split("."))


def _lock_versions(name: str) -> list[tuple[str, tuple[int, ...]]]:
    packages = json.loads((FE / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    return [(path, _version(meta["version"])) for path, meta in packages.items()
            if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")]


def test_next_and_sentry_versions():
    deps = json.loads((FE / "package.json").read_text(encoding="utf-8"))["dependencies"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", deps["next"]), "next stays pinned exactly"
    assert _version(deps["next"]) >= (16, 4, 6)
    assert re.fullmatch(r"11\.\d+\.\d+", deps["@sentry/nextjs"]), "@sentry/nextjs: an exact 11.x pin"


def test_build_wrapper_comes_from_the_config_entry_point():
    cfg = re.sub(r"^\s*//.*$", "", (FE / "next.config.ts").read_text(encoding="utf-8"), flags=re.M)
    assert 'import { withSentryConfig } from "@sentry/nextjs/config";' in cfg
    assert 'from "@sentry/nextjs";' not in cfg
    assert "disableLogger" not in cfg, "removed in Sentry 11: bundleSizeOptimizations.excludeDebugStatements"
    assert "buildTimeInstrumentation: false," in cfg


def test_lockfile_has_no_vulnerable_zod_glob_or_minimatch():
    zod = _lock_versions("zod")
    assert zod and all(v >= (4, 6, 6) for _, v in zod), zod
    for name, low, fixed in (("glob", (9,), (14, 0, 2)), ("minimatch", (7,), (11, 0, 1))):
        bad = [(p, v) for p, v in _lock_versions(name) if low <= v < fixed]
        assert not bad, f"{name} in the vulnerable range: {bad}"


def test_security_audit_runs_the_npm_audit_gate():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = {s.get("name", ""): s for s in ci["jobs"]["security-audit"]["steps"]}
    step = steps["npm audit (admin frontend, failing on high)"]
    assert step["working-directory"] == "frontend"
    assert "npm audit --json > ../npm-audit.json || true" in step["run"]
    gate = "python3 ../.github/scripts/npm_audit_gate.py ../.github/npm-audit-allow.json < ../npm-audit.json"
    assert gate in step["run"]
    allow = json.loads((ROOT / ".github" / "npm-audit-allow.json").read_text(encoding="utf-8"))
    assert isinstance(allow.get("allow"), list)
    lint = (ROOT / ".github" / "workflows" / "workflow-lint.yml").read_text(encoding="utf-8")
    assert "-p 'test_npm_audit_gate.py'" in lint

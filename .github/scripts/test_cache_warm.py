"""Tests of .github/workflows/cache-warm.yml against the caches CI restores (S273, AA-1024).

CI runs on pull_request / merge_group only, and caches are saved only on the default branch (workflow_lint WL4), so the
Cache warm workflow is what writes main's entries. A key or path that differs by one character between a CI restore and
the warm save means every run misses. These tests read the workflows (offline, PyYAML): every actions/cache/restore in a
workflow that never runs on main (no push / schedule trigger) has a warm lookup with the same path and key, every warm
lookup serves a restore, every warm save is a default-branch save of its lookup's key, and the trivy DB / release
binary jobs cover what the Docker Build Check uses. Same file in AA-CIS-App, AA-Booking and AA-Partner.
Run: python3 -B -m unittest discover -s .github/scripts -p 'test_cache_warm.py'
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WARM = WORKFLOWS / "cache-warm.yml"
DEFAULT_REF = "github.ref == 'refs/heads/main'"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on(wf: dict) -> dict:
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {e: None for e in on}
    return on or {}


def _steps(wf: dict):
    for job_id, job in (wf.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict):
                yield job_id, step


def _restores(wf: dict) -> set[tuple[str, str]]:
    return {(str(s["with"]["path"]), str(s["with"]["key"])) for _, s in _steps(wf)
            if str(s.get("uses", "")).startswith("actions/cache/restore@")}


class CacheWarm(unittest.TestCase):
    def setUp(self):
        self.warm = _load(WARM)
        self.lookups = {}
        for job_id, step in _steps(self.warm):
            if str(step.get("uses", "")).startswith("actions/cache/restore@"):
                self.assertIs(step["with"].get("lookup-only"), True, f"{job_id}: a warm restore is lookup-only")
                self.lookups[(str(step["with"]["path"]), str(step["with"]["key"]))] = (job_id, step["id"])

    def test_triggers_permissions_and_default_branch_only(self):
        on = _on(self.warm)
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertIn("schedule", on)
        self.assertIn("workflow_dispatch", on)
        self.assertNotIn("pull_request", on)
        self.assertEqual(self.warm["permissions"], {"contents": "read"})
        for job_id, job in self.warm["jobs"].items():
            self.assertEqual(job["if"], DEFAULT_REF, job_id)

    def test_every_ci_restore_has_a_warm_lookup(self):
        missing = []
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            if path == WARM:
                continue
            wf = _load(path)
            on = _on(wf)
            if "push" in on or "schedule" in on:
                continue  # it runs on main itself and saves there (WL4 checks the save's default-branch if)
            for restore in _restores(wf):
                if "matrix." in restore[1]:
                    continue  # a per-matrix key (other repos' lock files) only falls back to the shared prefix
                if restore not in self.lookups:
                    missing.append(f"{path.name}: {restore}")
        self.assertEqual(missing, [])

    def test_every_warm_lookup_serves_a_restore(self):
        restores = set()
        for path in WORKFLOWS.glob("*.y*ml"):
            if path != WARM:
                restores |= _restores(_load(path))
        self.assertEqual(sorted(k for k in self.lookups if k not in restores), [])

    def test_saves_are_default_branch_saves_of_the_lookup_key(self):
        saves = 0
        for job_id, step in _steps(self.warm):
            if str(step.get("uses", "")).startswith("actions/cache/save@"):
                saves += 1
                ids = [i for (j, i) in self.lookups.values() if j == job_id]
                self.assertEqual(len(ids), 1, job_id)
                self.assertIn(DEFAULT_REF, step["if"])
                self.assertIn(f"steps.{ids[0]}.outputs.cache-hit != 'true'", step["if"])
                self.assertEqual(step["with"]["key"], f"${{{{ steps.{ids[0]}.outputs.cache-primary-key }}}}")
                lookup = next(k for k, v in self.lookups.items() if v == (job_id, ids[0]))
                self.assertEqual(step["with"]["path"], lookup[0])
        self.assertEqual(saves, len(self.lookups))

    def test_release_binaries_and_trivy_db_cover_the_docker_build_check(self):
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        used = set(re.findall(r"uses: \./\.github/actions/release-bin\n\s+with:\n\s+tool: (\S+)", ci))
        self.assertTrue({"trivy", "syft"} <= used)
        warm_text = WARM.read_text(encoding="utf-8")
        for tool in used:
            self.assertRegex(warm_text, rf"tool: {tool}\n", tool)
        self.assertIn("trivy image --download-db-only", warm_text)
        self.assertIn("aquasecurity/trivy-action@", ci)
        self.assertNotIn("anchore/sbom-action@", ci)


if __name__ == "__main__":
    unittest.main()

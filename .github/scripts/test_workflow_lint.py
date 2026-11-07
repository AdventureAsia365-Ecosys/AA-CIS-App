"""Tests of .github/scripts/workflow_lint.py and the Workflow Lint workflow (AA-1024 G15, vendored from AA-CIS-Infra).

Offline, stdlib unittest + PyYAML: synthetic workflows for each rule, the --skip switch, then this repo's own workflows
(clean with the skips the workflow passes), then the shape of .github/workflows/workflow-lint.yml (actionlint pinned by
version and SHA-256, checked before it runs). Invented names only.
Run: python3 -B -m unittest discover -s .github/scripts -p 'test_workflow_lint.py'
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import tempfile
import textwrap
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCRIPT = os.path.join(HERE, "workflow_lint.py")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "workflow-lint.yml")

_spec = importlib.util.spec_from_file_location("workflow_lint", SCRIPT)
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)


def _lint(body: str, repo_root: str = REPO_ROOT, skip: frozenset = frozenset()) -> list[str]:
    return wl.lint_workflow("demo.yml", textwrap.dedent(body), repo_root, skip)


def _git_env() -> dict:
    # a git-running test drops the repo-local git variables a hook exports (S272: GIT_DIR made a "temporary" repo the
    # real clone)
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _skip_args() -> frozenset:
    """The rules the workflow skips for this repo (its `--skip` argument)."""
    with open(WORKFLOW, encoding="utf-8") as fh:
        m = re.search(r"workflow_lint\.py --skip ([A-Z0-9,]+)", fh.read())
    return frozenset(m.group(1).split(",")) if m else frozenset()


class WorkflowLintRules(unittest.TestCase):
    def test_wl1_sensitive_vars(self):
        body = """
            on: push
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - run: echo "${{ vars.DEMO_ROLE_ARN }} ${{ vars.DEMO_APP_ID }} ${{ secrets.DEMO_KEY_ARN }}"
                  # vars.DEMO_OLD_ROLE_ARN in a comment is not a use
            """
        bad = _lint(body)
        self.assertEqual(len(bad), 1)
        self.assertIn(":WL1 vars.DEMO_ROLE_ARN", bad[0])
        self.assertEqual(_lint(body, skip=frozenset({"WL1"})), [])

    def test_wl2_playwright_pins_the_runner_and_a_shell_comment_is_not_a_use(self):
        bad = _lint("""
            on: push
            jobs:
              e2e:
                runs-on: ubuntu-latest
                steps:
                  - run: npx playwright install --with-deps chromium
              notes:
                runs-on: ubuntu-latest
                steps:
                  - run: |
                      # the invented app's smoke needs Playwright and a live host, so it is not run here
                      npm test
            """)
        self.assertEqual(len(bad), 1)
        self.assertIn(":e2e:WL2 ", bad[0])

    def test_wl3_tracked_lockfile_and_expression_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True, env=_git_env())
            for d in ("web", "api"):
                os.makedirs(os.path.join(tmp, d))
                with open(os.path.join(tmp, d, "package-lock.json"), "w") as fh:
                    fh.write("{}")
            subprocess.run(["git", "-C", tmp, "add", "web/package-lock.json"], check=True, env=_git_env())
            wl.UNCHECKED.clear()
            findings = _lint("""
                on: push
                jobs:
                  web:
                    runs-on: ubuntu-24.04
                    defaults:
                      run:
                        working-directory: web
                    steps:
                      - run: npm ci && npm test
                  api:
                    runs-on: ubuntu-24.04
                    steps:
                      - working-directory: api
                        run: npm ci
                  dyn:
                    runs-on: ubuntu-24.04
                    steps:
                      - working-directory: other/${{ matrix.app }}
                        run: npm ci
                """, tmp)
        self.assertEqual([f.split(":")[1] for f in findings], ["api"])
        self.assertIn("api/package-lock.json", findings[0])
        self.assertEqual(len(wl.UNCHECKED), 1)

    def test_cli_skip_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.yml")
            bad = os.path.join(tmp, "bad.yml")
            broken = os.path.join(tmp, "broken.yml")
            with open(good, "w") as fh:
                fh.write("on: push\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo ok\n")
            with open(bad, "w") as fh:
                fh.write("on: push\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n"
                         "      - run: echo ${{ vars.DEMO_ROLE_ARN }}\n")
            with open(broken, "w") as fh:
                fh.write("on: [push\n")
            self.assertEqual(wl.main([good]), 0)
            self.assertEqual(wl.main([good, bad]), 1)
            self.assertEqual(wl.main(["--skip", "WL1", good, bad]), 0)
            self.assertEqual(wl.main(["--skip", "WL9", good]), 2)
            self.assertEqual(wl.main([broken]), 2)


class ThisRepo(unittest.TestCase):
    def test_the_repo_workflows_are_clean_with_the_workflows_skips(self):
        skip = _skip_args()
        self.assertTrue(skip <= {"WL1"}, "only WL1 may be skipped (vars moving to secrets is a repo-settings step)")
        wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
        findings = []
        for name in sorted(os.listdir(wf_dir)):
            if name.endswith((".yml", ".yaml")):
                with open(os.path.join(wf_dir, name), encoding="utf-8") as fh:
                    findings += wl.lint_workflow(name, fh.read(), REPO_ROOT, skip)
        self.assertEqual(findings, [])

    def test_workflow_pins_and_verifies_actionlint(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
        wf = yaml.safe_load(text)
        on = wf.get("on", wf.get(True))
        paths = on["pull_request"]["paths"]
        for p in (".github/workflows/**", ".github/scripts/workflow_lint.py", ".github/scripts/test_workflow_lint.py"):
            self.assertIn(p, paths)
        self.assertEqual(wf["permissions"], {"contents": "read"})
        job = wf["jobs"]["workflow-lint"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertRegex(job["env"]["ACTIONLINT_VERSION"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(job["env"]["ACTIONLINT_SHA256"], r"^[0-9a-f]{64}$")
        steps = {s.get("name", ""): s for s in job["steps"]}
        install = steps["Install actionlint (pinned, checksum verified)"]["run"]
        self.assertLess(install.index("sha256sum -c -"), install.index("tar -xzf"))
        self.assertTrue(steps["actionlint"]["run"].startswith("actionlint "))
        self.assertIn(".github/scripts/workflow_lint.py", steps["Repo workflow rules"]["run"])
        self.assertIn("test_workflow_lint.py", steps["Repo workflow rules"]["run"])
        self.assertIs(steps["Checkout"]["with"]["persist-credentials"], False)
        self.assertNotIn("secrets.", text)


if __name__ == "__main__":
    unittest.main()

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
    # the WL1-WL5 fixtures name actions by tag for readability; WL6 (SHA pins) has its own test
    return [f for f in wl.lint_workflow("demo.yml", textwrap.dedent(body), repo_root, skip) if ":WL6 " not in f]


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

    def test_wl4_caches_save_only_on_the_default_branch(self):
        bad = _lint("""
            on: pull_request
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/setup-python@0000000000000000000000000000000000000000 # v7.1.0
                    with:
                      python-version: "3.12"
                      cache: pip
                  - uses: actions/setup-node@v7
                    with:
                      node-version: 22
                  - uses: actions/setup-node@v4
                    with:
                      node-version: 22
                      cache: npm
                  - name: Set up uv
                    uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v10.3.0
                    with:
                      enable-cache: true
                  - uses: actions/cache@v6
                    with:
                      path: demo
                      key: demo
                  - uses: actions/cache/save@v6
                    if: always()
                    with:
                      path: demo
                      key: demo
                  - name: Scan
                    uses: aquasecurity/trivy-action@v0.36.0
                    with:
                      image-ref: demo
                      skip-setup-trivy: "true"
            """)
        self.assertEqual([f.split(":WL4 ")[0] for f in bad if ":WL4 " in f], ["demo.yml:a"] * 7)
        self.assertEqual(len(bad), 7, bad)
        ok = _lint("""
            on: push
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/setup-python@v7
                    with:
                      python-version: "3.12"
                  - uses: actions/setup-node@v7
                    with:
                      node-version: 22
                      package-manager-cache: false
                  - uses: astral-sh/setup-uv@v10
                    with:
                      enable-cache: true
                      save-cache: ${{ github.ref == 'refs/heads/main' }}
                  - uses: astral-sh/setup-uv@v10
                    with:
                      enable-cache: false
                  - id: c
                    uses: actions/cache/restore@v6
                    with:
                      path: demo
                      key: demo
                  - uses: actions/cache/save@v6
                    if: github.ref == 'refs/heads/master' && steps.c.outputs.cache-hit != 'true'
                    with:
                      path: demo
                      key: demo
                  - uses: aquasecurity/trivy-action@v0.36.0
                    with:
                      cache: "false"
                      skip-setup-trivy: "true"
            """)
        self.assertEqual(ok, [])
        nested = _lint("""
            on: pull_request
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/cache/restore@v6
                    with:
                      path: ~/.npm
                      key: npm-${{ hashFiles('apps/${{ matrix.app }}/package-lock.json') }}
                  - uses: actions/cache/restore@v6
                    with:
                      path: ~/.npm
                      key: npm-${{ hashFiles(format('apps/{0}/package-lock.json', matrix.app)) }}
            """)
        self.assertEqual(len(nested), 1)
        self.assertIn("inside an expression", nested[0])
        self.assertEqual(_lint("""
            on: push
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/setup-python@v7
                    with:
                      cache: pip
            """, skip=frozenset({"WL4"})), [])

    def test_wl5_external_downloads_need_a_cache_and_a_sha256_pin(self):
        bad = _lint("""
            on: pull_request
            env:
              DEMO_SHA: "0000000000000000000000000000000000000000000000000000000000000000"
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: anchore/sbom-action@v0.24.3
                  - uses: aquasecurity/trivy-action@v0.36.0
                    with:
                      cache: "false"
                  - name: Fetch demotool
                    run: |
                      curl -fsSL -o t.tgz "https://github.com/demo-org/demotool/releases/download/v1.0.0/t.tar.gz"
                      echo "${DEMO_SHA}  t.tgz" | sha256sum -c -
                  - name: Installer script
                    run: curl -sSfL https://downloads.example.test/get | sh
              b:
                runs-on: ubuntu-24.04
                steps:
                  - id: c
                    uses: actions/cache/restore@v6
                    with:
                      path: bin
                      key: demo
                  - name: Cached but unpinned
                    run: wget -q https://downloads.example.test/demo/1.0/demo.deb
            """)
        wl5 = [f for f in bad if ":WL5 " in f]
        self.assertEqual(len(wl5), 5, bad)
        self.assertIn("anchore/sbom-action", wl5[0])
        self.assertIn("skip-setup-trivy", wl5[1])
        self.assertIn("(no cache)", wl5[2])  # pinned through the workflow env it names, but the job has no cache
        self.assertIn("(no cache and no sha256 check)", wl5[3])
        self.assertIn("(no sha256 check)", wl5[4])
        ok = _lint("""
            on: pull_request
            jobs:
              a:
                runs-on: ubuntu-24.04
                env:
                  DEMO_URL: https://example.test
                steps:
                  - uses: ./.github/actions/release-bin
                    with:
                      tool: demotool
                  - name: Health checks and API calls are not downloads
                    run: |
                      curl -fsS http://127.0.0.1:3000/api/health -o /tmp/h.json
                      curl -fsS "https://${PUBLIC_HOST}/sitemap.xml" -o /tmp/s.xml
                      curl -s https://api.example.test/v1/items | jq .
                      # curl -o x.tgz https://github.com/demo-org/demotool/releases/download/v1/x.tar.gz (a comment)
                  - id: c
                    uses: actions/cache/restore@v6
                    with:
                      path: bin
                      key: demo
                  - name: Cached and pinned
                    run: |
                      curl -fsSL -o t.tgz https://github.com/demo-org/demotool/releases/download/v1.0.0/t.tar.gz
                      echo "0000000000000000000000000000000000000000000000000000000000000000  t.tgz" | sha256sum -c -
            """)
        self.assertEqual(ok, [])
        action = textwrap.dedent("""
            name: demo
            runs:
              using: composite
              steps:
                - shell: bash
                  run: curl -fsSL -o x.zip https://downloads.example.test/x/1.0/x.zip
            """)
        self.assertEqual(len(wl.lint_action("demo", action)), 1)
        self.assertEqual(wl.lint_action("demo", action, frozenset({"WL5"})), [])
        self.assertEqual(wl.lint_action("demo", "name: demo\nruns:\n  using: node24\n  main: index.js\n"), [])

    def test_wl6_every_action_is_pinned_to_a_commit_sha_with_its_tag(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        body = textwrap.dedent(f"""
            on: pull_request
            jobs:
              shared:
                uses: demo-org/demo-workflows/.github/workflows/demo.yml@main
              local:
                uses: ./.github/workflows/demo-local.yml
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: demo-org/demo-setup@v3
                  - uses: "demo-org/demo-setup@v3.1.0"
                  - uses: demo-org/demo-setup@{sha[:12]} # v3.1.0
                  - uses: demo-org/demo-setup@{sha} # v3.1.0
                  - uses: demo-org/demo-cache/save@{sha} # v6.1.0
                  - uses: demo-org/demo-setup@{sha}
                  - uses: ./.github/actions/demo-local
                  - uses: docker://demo/image:1.0
                  - uses: docker://demo/image@sha256:{"a" * 64}
                  - run: |
                      # uses: demo-org/in-a-script@v1 (a shell comment, not a use)
                      echo ok
            """)
        found = [f for f in wl.lint_workflow("demo.yml", body) if ":WL6 " in f]
        self.assertEqual(len(found), 6, found)
        for ref in ("demo-org/demo-workflows/.github/workflows/demo.yml@main", "demo-org/demo-setup@v3 ",
                    "demo-org/demo-setup@v3.1.0", f"demo-org/demo-setup@{sha[:12]}", "docker://demo/image:1.0"):
            self.assertTrue(any(ref in f and "not pinned" in f for f in found), ref)
        self.assertTrue(any(f"keep the release tag of demo-org/demo-setup@{sha} " in f for f in found), found)
        self.assertFalse(any("in-a-script" in f or "demo-local" in f or "image@sha256" in f for f in found), found)
        # --skip never turns WL6 off: the org setting refuses a tag pin whatever this script says
        self.assertNotIn("WL6", wl.RULES)
        self.assertEqual(len([f for f in wl.lint_workflow("demo.yml", body, skip=frozenset(wl.RULES))
                              if ":WL6 " in f]), 6)
        action = textwrap.dedent(f"""
            name: demo
            runs:
              using: composite
              steps:
                - uses: demo-org/demo-cache/restore@v6
                - uses: demo-org/demo-cache/save@{sha} # v6.1.0
            """)
        self.assertEqual(wl.lint_action("actions/demo/action.yml", action, frozenset({"WL5"})),
                         ["actions/demo/action.yml:-:WL6 demo-org/demo-cache/restore@v6 is not pinned to a full-length "
                          "commit SHA: use <owner>/<repo>@<40-hex sha> # vX.Y.Z (the tag's commit from gh api "
                          "repos/<owner>/<repo>/git/ref/tags/<tag>, annotated tags dereferenced)"])

    def test_wl4_reads_the_setup_node_major_from_the_pin_comment(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        tmpl = """
            on: pull_request
            jobs:
              a:
                runs-on: ubuntu-24.04
                steps:
                  - uses: actions/setup-node@{sha} # {tag}
                    with:
                      node-version: 22
            """
        self.assertEqual(_lint(tmpl.format(sha=sha, tag="v4.4.0")), [])
        self.assertEqual(len(_lint(tmpl.format(sha=sha, tag="v7.1.0"))), 1)
        self.assertEqual(wl.pin_tags(f"      - uses: actions/setup-node@{sha} # v4.4.0\n# uses: x/y@{sha} # v9\n"),
                         {f"actions/setup-node@{sha}": "v4.4.0"})

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
        for p in (".github/workflows/**", ".github/actions/**", ".github/scripts/workflow_lint.py",
                  ".github/scripts/test_workflow_lint.py", ".github/scripts/release_bin.py",
                  ".github/scripts/cache_cleanup.py"):
            self.assertIn(p, paths)
        self.assertEqual(wf["permissions"], {"contents": "read"})
        job = wf["jobs"]["workflow-lint"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        steps = {s.get("name", ""): s for s in job["steps"]}
        # S273: actionlint comes from the cached, sha256-pinned release-bin action (pins.txt), not a bare download.
        install = steps["Install actionlint (pinned, checksum verified)"]
        self.assertEqual(install["uses"], "./.github/actions/release-bin")
        self.assertEqual(install["with"], {"tool": "actionlint"})
        pins = os.path.join(REPO_ROOT, ".github", "actions", "release-bin", "pins.txt")
        with open(pins, encoding="utf-8") as fh:
            self.assertRegex(fh.read(), r"(?m)^actionlint v\d+\.\d+\.\d+ x86_64 [0-9a-f]{64} https://github\.com/rhysd/")
        self.assertTrue(steps["actionlint"]["run"].startswith("actionlint "))
        rules = steps["Repo workflow rules"]["run"]
        for needle in (".github/scripts/workflow_lint.py", "test_workflow_lint.py", "test_release_bin.py",
                       "test_cache_cleanup.py"):
            self.assertIn(needle, rules)
        self.assertIs(steps["Checkout"]["with"]["persist-credentials"], False)
        self.assertNotIn("secrets.", text)

    def test_dependabot_bumps_the_pinned_actions_weekly_in_one_group(self):
        with open(os.path.join(REPO_ROOT, ".github", "dependabot.yml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        gha = [u for u in cfg["updates"] if u["package-ecosystem"] == "github-actions"]
        self.assertEqual(len(gha), 1)
        dirs = gha[0].get("directories") or [gha[0].get("directory")]
        self.assertIn("/", dirs)
        if os.path.isdir(os.path.join(REPO_ROOT, ".github", "actions")):
            self.assertIn("/.github/actions/*", dirs)
        self.assertEqual(gha[0]["schedule"]["interval"], "weekly")
        self.assertTrue(gha[0].get("groups"), "one grouped PR for every action bump")
        # never auto-merged: no workflow step approves or merges a Dependabot PR (comment lines may say so)
        wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
        for name in os.listdir(wf_dir):
            with open(os.path.join(wf_dir, name), encoding="utf-8") as fh:
                text = wl._strip_comments(fh.read())
            self.assertFalse("dependabot" in text.lower() and re.search(
                r"gh\s+pr\s+(?:merge|review)|auto-?merge|enablePullRequestAutoMerge", text, re.I), name)

    def test_the_repo_composite_actions_are_clean(self):
        actions = os.path.join(REPO_ROOT, ".github", "actions")
        findings = []
        for name in sorted(os.listdir(actions)):
            path = os.path.join(actions, name, "action.yml")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    findings += wl.lint_action(name, fh.read())
        self.assertEqual(findings, [])

if __name__ == "__main__":
    unittest.main()

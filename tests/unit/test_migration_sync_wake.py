"""Shape of .github/workflows/migration-sync-wake.yml (AA-993, root ADR 0011a decision 3 as amended in S258).

The workflow mints an aa-ecosys-ci-bot token from the private key that can mint any permission the App has, so the
narrowing in the file is the control. Pinned here, as text (stdlib only: the same file runs under pytest and under
``python3 -m unittest``): ``permissions: {}``; no checkout; push to main on the migrations folder only; the token
minted in environment ``release`` for AA-CIS-Infra with ``permission-issues: write`` and no other permission; the only
write is one REST comment on the wake-up issue (no dispatch, no PR, no label). The workflow differs between AA-CIS-App,
AA-Booking, AA-TripPlanner-Web and AA-Partner only in its migrations path (and AA-Partner's environment note); this
test is identical in all four."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


def _repo() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / ".github" / "workflows" / "migration-sync-wake.yml").is_file():
            return p
    raise RuntimeError("migration-sync-wake.yml not found above this test")


REPO = _repo()
WF = (REPO / ".github" / "workflows" / "migration-sync-wake.yml").read_text()


class MigrationSyncWakeShapeTest(unittest.TestCase):
    def test_no_github_token_permission_and_no_checkout(self):
        self.assertRegex(WF, r"(?m)^permissions: \{\}$")
        self.assertEqual(len(re.findall(r"(?m)^\s*permissions:", WF)), 1)  # no job-level grant either
        self.assertNotIn("actions/checkout", WF)

    def test_triggers_push_to_main_on_the_migrations_folder_only(self):
        on = WF.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("    branches: [main]\n", on)
        paths = re.findall(r'(?m)^      - "([^"]+)"$', on)
        self.assertEqual(len(paths), 1)
        self.assertRegex(paths[0], r"^(api/)?migrations/\*\*$")
        self.assertTrue((REPO / paths[0][: -len("/**")]).is_dir(), paths[0])
        self.assertEqual(set(re.findall(r"(?m)^  ([a-z_]+):", on)), {"push", "workflow_dispatch"})
        self.assertIn("    if: github.ref == 'refs/heads/main'\n", WF)

    def test_token_is_infra_only_and_issues_write_only(self):
        self.assertEqual(WF.count("actions/create-github-app-token@"), 1)
        self.assertIn("    environment: release\n", WF)
        self.assertIn("          owner: AdventureAsia365-Ecosys\n", WF)
        self.assertIn("          repositories: AA-CIS-Infra\n", WF)
        self.assertEqual(re.findall(r"(?m)^\s*(permission-[a-z-]+): *(\S+)", WF), [("permission-issues", "write")])

    def test_the_only_write_is_one_comment_on_the_wake_up_issue(self):
        writes = re.findall(r"gh api --method (\w+) \"([^\"]+)\"", WF)
        self.assertEqual(writes, [("POST", "repos/$INFRA/issues/$N/comments")])
        # Two gh api calls: the GET of the issue number and that POST. A body field makes gh api POST by itself, so
        # exactly one field / input flag may appear (the comment body).
        self.assertEqual(len(re.findall(r"\bgh api\b", WF)), 2)
        self.assertEqual(len(re.findall(r"\bgh api (-X|--method)\b", WF)), 1)
        self.assertEqual(re.findall(r"(?<![\w-])(-f|-F|--field|--raw-field|--input)[ =]", WF), ["-f"])
        for banned in ("gh workflow", "gh pr ", "gh issue ", "gh label", "dispatches", "/labels", "git push"):
            self.assertNotIn(banned, WF)
        self.assertIn("labels=migration-sync-wake", WF)


if __name__ == "__main__":
    unittest.main()

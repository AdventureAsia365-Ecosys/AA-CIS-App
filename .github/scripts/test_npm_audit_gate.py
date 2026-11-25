"""Tests of .github/scripts/npm_audit_gate.py on an invented npm audit report."""
from __future__ import annotations

import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import npm_audit_gate as gate  # noqa: E402

A = "GHSA-aaaa-bbbb-cccc"
B = "GHSA-dddd-eeee-ffff"
PKGS = ["globlib", "matcher", "lintkit", "loop-a", "loop-b"]
REPORT = {
    "auditReportVersion": 2,
    "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 1, "high": 5, "critical": 0, "total": 6}},
    "vulnerabilities": {
        "globlib": {"severity": "high", "via": [{"source": 1, "url": f"https://github.com/advisories/{A}", "severity": "high"}]},
        "matcher": {"severity": "high", "via": ["globlib"]},
        "lintkit": {"severity": "high", "via": ["matcher"]},
        "loop-a": {"severity": "high", "via": ["loop-b"]},
        "loop-b": {"severity": "high", "via": ["loop-a", "globlib"]},
        "minor": {"severity": "moderate", "via": [{"url": f"https://github.com/advisories/{B}", "severity": "moderate"}]},
    }
}
TODAY = dt.date(2026, 11, 18)


def run(report: dict, allow: dict | None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "allow.json")
        if allow is not None:
            Path(path).write_text(json.dumps(allow), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TODAY_OVERRIDE": TODAY.isoformat()}), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(report))
        ), redirect_stdout(out):
            code = gate.main(["gate", path])
        return code, out.getvalue()


class NpmAuditGate(unittest.TestCase):
    def test_chains_resolve_to_the_one_advisory(self):
        found = gate.failing_advisories(REPORT)
        self.assertEqual(set(found), {A})
        self.assertEqual(found[A], {"globlib", "matcher", "lintkit", "loop-a", "loop-b"})

    def test_unaccepted_high_fails(self):
        code, out = run(REPORT, None)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {A}", out)

    def test_accepted_with_reason_and_expiry_passes(self):
        code, out = run(REPORT, {"allow": [{"id": A, "expires": "2026-12-18", "reason": "build tooling only", "packages": PKGS}]})
        self.assertEqual(code, 0, out)
        self.assertIn(f"accepted {A} until 2026-12-18", out)

    def test_expired_entry_fails_again(self):
        code, _ = run(REPORT, {"allow": [{"id": A, "expires": "2026-11-18", "reason": "build tooling only", "packages": PKGS}]})
        self.assertEqual(code, 1)

    def test_entry_needs_reason_and_near_expiry(self):
        code, out = run(REPORT, {"allow": [{"id": A, "expires": "2026-12-18", "reason": " ", "packages": PKGS}]})
        self.assertEqual(code, 1)
        self.assertIn("a reason (text) is required", out)
        code, out = run(REPORT, {"allow": [{"id": A, "expires": "2027-06-01", "reason": "x", "packages": PKGS}]})
        self.assertEqual(code, 1)
        self.assertIn("more than 90 days ahead", out)

    def test_moderate_never_blocks(self):
        rep = {"auditReportVersion": 2, "metadata": {"vulnerabilities": {"moderate": 1, "high": 0, "critical": 0}},
               "vulnerabilities": {"minor": REPORT["vulnerabilities"]["minor"]}}
        code, _ = run(rep, None)
        self.assertEqual(code, 0)

    def test_npm_failure_fails_closed(self):
        allow = {"allow": [{"id": A, "expires": "2026-12-18", "reason": "x", "packages": PKGS}]}
        network = {"message": "request to the registry failed, reason: connect ECONNREFUSED", "error": {"summary": ""}}
        for bad in (network, {}, {"message": "Invalid package tree"}, [], None):
            code, out = run(bad, allow)
            self.assertEqual(code, 1, (bad, out))

    def test_count_mismatch_fails(self):
        rep = copy.deepcopy(REPORT)
        rep["metadata"]["vulnerabilities"]["high"] = 9
        code, out = run(rep, {"allow": [{"id": A, "expires": "2026-12-18", "reason": "x", "packages": PKGS}]})
        self.assertEqual(code, 1)
        self.assertIn("metadata counts 9", out)

    def test_untraceable_high_package_fails(self):
        rep = copy.deepcopy(REPORT)
        rep["vulnerabilities"]["orphan"] = {"severity": "high", "via": ["not-in-report"]}
        rep["vulnerabilities"]["nosev"] = {"severity": "high", "via": [{"url": "https://example.test/x"}]}
        rep["metadata"]["vulnerabilities"]["high"] = 7
        code, out = run(rep, {"allow": [{"id": A, "expires": "2026-12-18", "reason": "x", "packages": PKGS}]})
        self.assertEqual(code, 1)
        self.assertIn("UNRESOLVED:orphan", out)
        self.assertIn("FAIL https://example.test/x", out)

    def test_new_path_outside_the_listed_packages_fails(self):
        allow = {"allow": [{"id": A, "expires": "2026-12-18", "reason": "x", "packages": ["globlib", "matcher"]}]}
        code, out = run(REPORT, allow)
        self.assertEqual(code, 1)
        self.assertIn("not in its allowlist packages: lintkit, loop-a, loop-b", out)

    def test_own_critical_advisory_is_not_masked(self):
        rep = copy.deepcopy(REPORT)
        rep["vulnerabilities"]["lintkit"]["via"].append({"url": f"https://github.com/advisories/{B}", "severity": "critical"})
        code, out = run(rep, {"allow": [{"id": A, "expires": "2026-12-18", "reason": "x", "packages": PKGS}]})
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {B}", out)


if __name__ == "__main__":
    unittest.main()

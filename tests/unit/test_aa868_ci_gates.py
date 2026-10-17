"""AA-868 — the CI/deploy gates fail closed.

Runs the real bash scripts:
- .github/scripts/deploy_job_guard.sh against a local HTTP stand-in of the API deploy gate: a missing
  credential, a gate error, a non-JSON answer and active jobs all block; an override reason lets
  each through; an idle gate passes.
- .github/scripts/pip_audit_gate.sh: an expired or malformed ignore entry fails before pip-audit runs.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / ".github" / "scripts" / "deploy_job_guard.sh"
AUDIT = REPO / ".github" / "scripts" / "pip_audit_gate.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl") and shutil.which("jq")),
    reason="needs bash, curl and jq",
)


class _Gate(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"{}"

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def gate():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Gate)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def answer(status, body):
        _Gate.status = status
        _Gate.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        return f"http://127.0.0.1:{server.server_port}/admin/job-runner/deploy-gate"

    yield answer
    server.shutdown()


def _guard(secret="s3cret", url="http://127.0.0.1:9/none", override=""):
    env = {"PATH": os.environ["PATH"], "ADMIN_SECRET": secret, "GATE_URL": url, "OVERRIDE": override}
    return subprocess.run(["bash", str(GUARD)], env=env, capture_output=True, text=True, timeout=120)


def test_guard_passes_when_idle(gate):
    r = _guard(url=gate(200, {"safe_to_deploy": True, "active": 0}))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("case", ["no_secret", "http_500", "not_json", "no_field", "jobs_active"])
def test_guard_blocks_and_override_releases(gate, case):
    kwargs = {
        "no_secret": {"secret": "", "url": gate(200, {"safe_to_deploy": True})},
        "http_500": {"url": gate(500, {"detail": "boom"})},
        "not_json": {"url": gate(200, b"<html>bad gateway</html>")},
        "no_field": {"url": gate(200, {"active": 0})},
        "jobs_active": {"url": gate(200, {"safe_to_deploy": False, "active": 2})},
    }[case]
    blocked = _guard(**kwargs)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "::error::Deploy blocked" in blocked.stdout
    released = _guard(override="test reason", **kwargs)
    assert released.returncode == 0, released.stdout + released.stderr
    assert "OVERRIDDEN" in released.stdout


def _audit(tmp_path, lines, today="2026-10-17"):
    ignore = tmp_path / "ignore.txt"
    ignore.write_text("\n".join(lines) + "\n")
    # pip-audit must not be reached when the list is invalid: a stub that would pass proves it.
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "pip-audit").write_text("#!/bin/sh\necho STUB-RAN\n")
    (stub / "pip-audit").chmod(0o755)
    env = {"PATH": f"{stub}:{os.environ['PATH']}", "TODAY_OVERRIDE": today}
    return subprocess.run(["bash", str(AUDIT), "requirements.txt", str(ignore)], env=env,
                          capture_output=True, text=True, timeout=60, cwd=REPO)


def test_audit_runs_with_valid_list(tmp_path):
    r = _audit(tmp_path, ["# comment", "PYSEC-1 2026-12-31 a reason"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STUB-RAN" in r.stdout


@pytest.mark.parametrize("line", ["PYSEC-1 2026-10-16 expired yesterday", "PYSEC-1 2026-12-31", "PYSEC-1 soon reason"])
def test_audit_rejects_expired_or_malformed(tmp_path, line):
    r = _audit(tmp_path, [line])
    assert r.returncode == 1
    assert "STUB-RAN" not in r.stdout


def test_repo_ignore_list_is_valid_today(tmp_path):
    # The committed list must parse and have no expired entry (stub pip-audit, no network).
    lines = (REPO / ".github" / "pip-audit-ignore.txt").read_text().splitlines()
    r = _audit(tmp_path, lines, today=os.environ.get("TODAY_OVERRIDE", ""))
    assert r.returncode == 0, r.stdout + r.stderr

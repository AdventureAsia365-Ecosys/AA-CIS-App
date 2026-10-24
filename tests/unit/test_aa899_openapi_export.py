"""AA-899 — the committed OpenAPI document (docs/api/openapi.json) is current, deterministic and host-free, and the
public surface does not grow new untyped responses.

The "API contract" workflow diffs the committed file against the base branch with oasdiff, so a stale file would
hide a breaking change: test_committed_document_is_current is what keeps it current (it runs in "Unit Tests").

oasdiff only sees a removed response field when the route declares a model. The public operations whose 2xx JSON
response is still an empty schema are listed in snapshots/aa899_untyped_public_routes.json. The list may only
shrink: a new public route must declare its response model, and a route that gains one leaves the list.
"""
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)

UNTYPED_SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "aa899_untyped_public_routes.json"
INTERNAL_PREFIX = "/admin/"  # same rule as INTERNAL_PATHS in .github/workflows/api-contract.yml


@pytest.fixture(autouse=True, scope="module")
def _offline_aws():
    """Build the spec the way the script does: a laptop with a live AWS profile must not reach Secrets Manager (or
    prompt for MFA) when this module is the first to import api.main. The environment is restored afterwards."""
    saved = dict(os.environ)
    export.offline_environment(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def untyped_public_operations(spec: dict) -> list[str]:
    out = []
    for path, ops in spec["paths"].items():
        if path.startswith(INTERNAL_PREFIX):
            continue
        for method, op in ops.items():
            for code, resp in op.get("responses", {}).items():
                if not code.startswith("2") or code == "204":
                    continue
                content = resp.get("content", {})
                if "application/json" in content and not content["application/json"].get("schema"):
                    out.append(f"{method.upper()} {path} {code}")
    return sorted(out)


def test_committed_document_is_current():
    problems = export.check(export.render(export.build_spec()))
    assert not problems, (
        f"docs/api/openapi.json is stale ({export.versions()}); run: python3 scripts/export_openapi.py\n"
        + "\n".join(problems))


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_export_script_is_tracked():
    """`scripts/` is gitignored (only `git add -f` scripts are kept): an untracked export script passes here and
    fails CI at import, so check the index, not the working tree."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "scripts/export_openapi.py"],
                         capture_output=True, text=True)
    assert out.returncode == 0, "scripts/export_openapi.py is not tracked: git add -f scripts/export_openapi.py"


def test_render_is_stable_and_host_free():
    text = export.render(export.build_spec())
    assert text == export.render(export.build_spec())
    assert text.endswith("}\n")
    doc = json.loads(text)
    assert "servers" not in doc
    assert doc["info"]["version"] == export.UNRELEASED
    assert "do not edit" in doc["info"]["x-generated-by"]


def test_normalize_does_not_mutate_and_pins_the_version():
    raw = {"openapi": "3.1.0", "info": {"title": "t", "version": "9.9.9"}, "servers": [{"url": "https://x"}]}
    out = export.normalize(raw)
    assert "servers" not in out and out["info"]["version"] == "unreleased"
    assert raw["info"]["version"] == "9.9.9" and "servers" in raw


def test_check_reports_a_stale_or_missing_file(tmp_path):
    path = tmp_path / "openapi.json"
    assert export.check("{}\n", path)
    path.write_text("{}\n", encoding="utf-8")
    assert export.check("{}\n", path) == []
    assert any(line.startswith("+") for line in export.check('{"a": 1}\n', path))


def test_offline_environment_never_keeps_a_real_aws_credential():
    env = {"AWS_PROFILE": "aa365-admin", "AWS_SESSION_TOKEN": "real", "JWT_SECRET": "kept"}
    export.offline_environment(env)
    assert "AWS_PROFILE" not in env and "AWS_SESSION_TOKEN" not in env
    assert env["AWS_ACCESS_KEY_ID"].startswith("stub") and env["AWS_ENDPOINT_URL"].startswith("http://127.0.0.1")
    assert env["JWT_SECRET"] == "kept" and env["ENVIRONMENT"] == "test"


def test_untyped_public_routes_only_shrink():
    current = untyped_public_operations(export.build_spec())
    allowed = json.loads(UNTYPED_SNAPSHOT.read_text(encoding="utf-8"))
    new = sorted(set(current) - set(allowed))
    gone = sorted(set(allowed) - set(current))
    assert not new, f"public route(s) without a response model (declare response_model=): {new}"
    assert not gone, f"typed now, remove from {UNTYPED_SNAPSHOT.name}: {gone}"


def test_public_platform_routes_name_every_field():
    schemas = export.build_spec()["components"]["schemas"]
    assert list(schemas["Health"]["properties"]) == ["status", "service", "version", "git_sha"]
    assert set(schemas["Readiness"]["required"]) == {"status", "db", "schema_version", "required_schema", "git_sha"}
    assert set(schemas["TenantSession"]["properties"]) == {"valid", "tenant_id", "name", "plan_tier"}
    assert set(schemas["Quota"]["properties"]) == {
        "plan_tier", "tours_per_month", "rewrites_used", "rewrites_remaining", "resets_at"}

"""Export the CIS API's OpenAPI document to ``docs/api/openapi.json`` (AA-899).

The committed file is the API reference of this repo and the base of the breaking-change check
(``.github/workflows/api-contract.yml``, oasdiff). It is generated, never edited by hand:

    python3 scripts/export_openapi.py           # rewrite docs/api/openapi.json
    python3 scripts/export_openapi.py --check   # exit 1 when the committed file is stale

The required "Unit Tests" check runs the same comparison (``tests/unit/test_aa899_openapi_export.py``).

No database, no Redis, no secret, no AWS call. ``api.main`` is only imported (the lifespan, which opens the pools,
never runs). Before the import this script sets the dummy values the unit tests use (``tests/unit/conftest.py``) and
points the AWS SDK at nothing: stub keys, no profile or config file, an endpoint on a closed local port, one
attempt. So ``init_sentry()`` (which reads the DSN from Secrets Manager at import) fails fast and offline even on a
laptop with a live MFA session, and no real credential is ever used.

The document is deterministic: keys sorted, two-space indent, UTF-8, one trailing newline; ``servers`` dropped (no
host in a public repo); ``info.version`` fixed to ``unreleased`` (the release workflow stamps the tag into the copy
it attaches to the GitHub release, so a release PR, which only bumps ``api/version.py``, never makes this file
stale); ``info.x-generated-by`` names this script. FastAPI and Pydantic are pinned in ``requirements.txt`` because
another version can change the output; ``--check`` prints the versions it ran with.

AA-1024 G6 (lessons log, CI family: openapi.json built with a local FastAPI 0.110 went red on CI): both modes
refuse (exit 2) when the installed FastAPI or Pydantic is not the ``==`` pin of requirements.txt, before importing the
app. Run it in a venv made from requirements.txt; ``--allow-unpinned`` only shows a diff, never commit its output.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "api" / "openapi.json"
GENERATED_BY = "scripts/export_openapi.py (AA-899) - generated, do not edit"
UNRELEASED = "unreleased"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PINNED_PACKAGES = ("fastapi", "pydantic")  # their versions change the generated document

# Same dummies as tests/unit/conftest.py (_CI_UNIT_ENV); applied only when unset.
STUB_ENV = {
    "ENVIRONMENT": "test",
    "AWS_REGION": "us-west-1",
    "AWS_DEFAULT_REGION": "us-west-1",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "OPENAI_API_KEY": "sk-test-dummy",
    "JWT_SECRET": "test-jwt-secret-ci-only-not-real",
    "DATAFORSEO_LOGIN": "test",
    "DATAFORSEO_PASSWORD": "test",
    "LANGFUSE_SECRET_KEY": "test",
    "LANGFUSE_PUBLIC_KEY": "test",
    "LANGFUSE_HOST": "http://localhost:3000",
    "BRONZE_BUCKET": "aa-cis-bronze-test",
    "BEDROCK_ACCT_PRIMARY": "acc3-test-account",
}
# Always forced: the AWS SDK must never find a real credential or reach AWS from this script.
OFFLINE_AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "stub-export-openapi",
    "AWS_SECRET_ACCESS_KEY": "stub-export-openapi",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_MAX_ATTEMPTS": "1",
    "AWS_RETRY_MODE": "standard",
}
UNSET_AWS_ENV = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN")


def offline_environment(environ: dict[str, str] | os._Environ = os.environ) -> None:
    for key, value in STUB_ENV.items():
        environ.setdefault(key, value)
    for key in UNSET_AWS_ENV:
        environ.pop(key, None)
    environ.update(OFFLINE_AWS_ENV)


def build_spec() -> dict[str, Any]:
    """A fresh ``app.openapi()`` of ``api.main.app`` (FastAPI's cached copy is bypassed, then put back)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from api.main import app

    cached = app.openapi_schema
    app.openapi_schema = None
    try:
        return app.openapi()
    finally:
        app.openapi_schema = cached


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    out.pop("servers", None)
    info = dict(out.get("info") or {})
    info["version"] = UNRELEASED
    info["x-generated-by"] = GENERATED_BY
    out["info"] = info
    return out


def render(spec: dict[str, Any]) -> str:
    return json.dumps(normalize(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def versions() -> str:
    import fastapi
    import pydantic

    return f"fastapi {fastapi.__version__}, pydantic {pydantic.VERSION}"


def pinned_versions(path: Path = REQUIREMENTS) -> dict[str, str]:
    """The ``name==version`` pins of PINNED_PACKAGES in requirements.txt."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s;#]+)", line)
        if m and m.group(1).lower() in PINNED_PACKAGES:
            out[m.group(1).lower()] = m.group(2)
    return out


def installed_versions() -> dict[str, str | None]:
    from importlib import metadata

    out: dict[str, str | None] = {}
    for name in PINNED_PACKAGES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def pin_problems(pins: dict[str, str] | None = None, have: dict[str, str | None] | None = None) -> list[str]:
    """Why this interpreter cannot produce the committed document; empty when every pin matches."""
    pins = pinned_versions() if pins is None else pins
    have = installed_versions() if have is None else have
    problems = []
    for name in PINNED_PACKAGES:
        want = pins.get(name)
        if want is None:
            problems.append(f"requirements.txt has no '{name}==<version>' pin")
        elif have.get(name) != want:
            problems.append(f"{name} {have.get(name) or 'not installed'} here, requirements.txt pins {want}")
    return problems


def check(expected: str, path: Path = OUTPUT) -> list[str]:
    """Empty when ``path`` holds exactly ``expected``; else a short unified diff (at most 40 lines)."""
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if current == expected:
        return []
    lines = list(difflib.unified_diff(
        current.splitlines(), expected.splitlines(), "committed", "generated", lineterm="", n=1))
    return lines[:40] + ([f"... {len(lines) - 40} more diff lines"] if len(lines) > 40 else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file is stale")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="file to write or check (default: %(default)s)")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="run with other FastAPI / Pydantic versions (to read a diff only; never commit it)")
    args = parser.parse_args(argv)
    problems = pin_problems()
    if problems:
        print("::error::export_openapi: " + "; ".join(problems) + ". Another version changes the document, and CI "
              "builds it with the pins: python3 -m venv /tmp/oa && /tmp/oa/bin/pip install -r requirements.txt, "
              "then run this script with /tmp/oa/bin/python.")
        if not args.allow_unpinned:
            return 2
        print("warning: --allow-unpinned: do not commit this output")
    offline_environment()
    text = render(build_spec())
    if args.check:
        problems = check(text, args.output)
        if problems:
            print("\n".join(problems))
            print(f"::error::{args.output.name} is stale. Run: python3 scripts/export_openapi.py")
            print(f"generated with {versions()}")
            return 1
        print(f"{args.output} is up to date ({versions()})")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(json.loads(text)['paths'])} paths, {versions()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

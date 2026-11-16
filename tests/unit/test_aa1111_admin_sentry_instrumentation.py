"""AA-1111 (S282 Sentry coverage) — the admin app (frontend/) reports errors to Sentry on Next 16 + @sentry/nextjs 10.

The S282 triage found project aa-cis-frontend silent for 90 days: besides the missing DSN variable, the app had only the
legacy ``sentry.client/server/edge.config.ts``, which a Turbopack build of Next 16 never loads. This is the CI-run
copy of the wiring checks (the behaviour of the options and the scrub is ``frontend/lib/sentryConfig.test.mjs``,
``npm run test:unit``):

1. ``instrumentation.ts`` imports the server config in the nodejs runtime and the edge config in the edge runtime, and
   exports ``onRequestError = Sentry.captureRequestError``.
2. ``instrumentation-client.ts`` exists, calls ``Sentry.init`` only with the options of ``lib/sentryConfig.ts`` (null =
   no DSN = no init) and the legacy ``sentry.client.config.ts`` is gone (it would initialise twice on webpack).
3. Every runtime builds its options through ``sentryInitOptions`` (the PII scrub, no tracing); no config calls
   ``Sentry.init`` with its own literal options.
4. ``lib/sentryConfig.ts`` returns null for an empty DSN and keeps ``beforeSend: scrubEvent``, ``tracesSampleRate: 0``.
5. The image still takes the browser DSN as a build arg from the ``dev`` variable ADMIN_PUBLIC_SENTRY_DSN.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend"


def _code(path: Path) -> str:
    """Source without comments, so a comment that names a call never satisfies a check."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_instrumentation_registers_server_and_edge_and_request_errors():
    src = _code(FE / "instrumentation.ts")
    assert re.search(r"export\s+async\s+function\s+register\s*\(", src)
    assert re.search(r'NEXT_RUNTIME\s*===\s*"nodejs"\)\s*await\s+import\("\./sentry\.server\.config"\)', src)
    assert re.search(r'NEXT_RUNTIME\s*===\s*"edge"\)\s*await\s+import\("\./sentry\.edge\.config"\)', src)
    assert re.search(r"export\s+const\s+onRequestError\s*=\s*Sentry\.captureRequestError\s*;", src)


def test_client_instrumentation_replaces_the_legacy_client_config():
    assert not (FE / "sentry.client.config.ts").exists()
    src = _code(FE / "instrumentation-client.ts")
    assert 'from "./lib/sentryConfig"' in src
    assert "process.env.NEXT_PUBLIC_SENTRY_DSN" in src
    assert re.search(r"if\s*\(options\)\s*Sentry\.init\(options\)\s*;", src)
    assert len(re.findall(r"Sentry\.init\(", src)) == 1


def test_every_runtime_uses_the_shared_options():
    for name in ("instrumentation-client.ts", "sentry.server.config.ts", "sentry.edge.config.ts"):
        src = _code(FE / name)
        assert "sentryInitOptions(" in src, name
        assert re.findall(r"Sentry\.init\(([^)]*)\)", src) == ["options"], name


def test_shared_options_skip_init_without_dsn_and_scrub():
    src = _code(FE / "lib" / "sentryConfig.ts")
    assert re.search(r"if\s*\(!dsn\)\s*return\s+null\s*;", src)
    assert "beforeSend: scrubEvent" in src
    assert "tracesSampleRate: 0," in src
    assert "userInfo: false" in src and "httpBodies: []" in src


def test_browser_dsn_is_a_build_arg_from_the_dev_variable():
    workflow = (ROOT / ".github" / "workflows" / "deploy-admin-dev.yml").read_text(encoding="utf-8")
    assert "PUBLIC_SENTRY_DSN: ${{ vars.ADMIN_PUBLIC_SENTRY_DSN }}" in workflow
    assert '--build-arg NEXT_PUBLIC_SENTRY_DSN="${PUBLIC_SENTRY_DSN}"' in workflow
    dockerfile = (FE / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG NEXT_PUBLIC_SENTRY_DSN=""' in dockerfile

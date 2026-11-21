"""AA-1155 T11 test helper: the AI budget and AI gateway routers need a verified Cognito session
(api/core/cognito_session.require_cognito_session). Tests of those routes that drive the app with the admin secret and an
x-admin-role (to test the role rules, the scope or the route itself) assume a verified session with this fixture; the
refusal without one is proved in tests/unit/test_aa1155_ai_controls_cognito.py."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

from api.core.cognito_session import require_cognito_session

ASSUMED_IDENTITY = SimpleNamespace(auth="cognito", admin_id="00000000-0000-4000-8000-00000000c0de")


@contextmanager
def cognito_session_assumed(app) -> Iterator[None]:
    """Within the block every request of ``app`` passes require_cognito_session (dependency override)."""
    app.dependency_overrides[require_cognito_session] = lambda: ASSUMED_IDENTITY
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_cognito_session, None)

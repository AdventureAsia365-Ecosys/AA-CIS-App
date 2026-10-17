"""Sentry for the API process. The implementation is in shared/sentry_init.py (AA-870), shared with
`python -m worker`, which must not import the `api` package."""
from shared.sentry_init import (  # noqa: F401  (re-exported for api.main and tests)
    SAFE_HEADERS,
    _get_dsn,
    _redact,
    before_breadcrumb,
    before_send,
    init_sentry,
    release,
)

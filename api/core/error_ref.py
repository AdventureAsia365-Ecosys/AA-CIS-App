"""AA-912 — client-safe error text plus a correlation ref (CodeQL py/stack-trace-exposure).

An endpoint that catches an exception returns `public_error(...)` — a generic message carrying a
short ref — and the exception detail and traceback go only to the server log under that same ref.
boto's AccessDenied text names the role ARN and account id, and DB errors name schemas and SQL;
neither may reach an API response (public repo, admin UI).
"""
import uuid

import structlog

logger = structlog.get_logger()


def new_error_ref() -> str:
    """A short random id that ties a client-facing error to its server log line."""
    return uuid.uuid4().hex[:12]


def log_exception_ref(event: str, **fields) -> str:
    """Call inside an `except` block: logs the active exception with its traceback under a fresh
    ref and returns the ref."""
    ref = new_error_ref()
    logger.exception(event, error_ref=ref, **fields)
    return ref


def public_error(what: str, ref: str) -> str:
    """The generic text a client sees: '<what> failed (ref <ref>); details in server logs'."""
    return f"{what} failed (ref {ref}); details in server logs"

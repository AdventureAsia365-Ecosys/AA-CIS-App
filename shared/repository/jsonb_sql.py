"""SQL fragments for JSONB columns whose contract is "a JSON array".

``acp_shared.content_piece.gate_ledger`` (and ``flags``, ``repair_log``, ``discarded_attempts``) is a JSONB array by
contract (mig 115, DEFAULT '[]'), but no CHECK enforces the shape. ``jsonb_array_elements`` on one object-shaped row
raises "cannot extract elements from an object" and fails the whole query, so one bad row took down the weekly
writing-memory KPI job (S282, Sentry AA-CIS-API-1F). Unnest such a column through :func:`json_array` instead.
"""
from __future__ import annotations

import re

_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


def json_array(column: str) -> str:
    """``column`` when it holds a JSON array, else an empty array: safe to pass to ``jsonb_array_elements``.

    ``column`` is a (qualified) column name written in code, never user input; anything else is refused.
    """
    if not _COLUMN.match(column):
        raise ValueError(f"not a column name: {column!r}")
    return f"(CASE WHEN jsonb_typeof({column}) = 'array' THEN {column} ELSE '[]'::jsonb END)"

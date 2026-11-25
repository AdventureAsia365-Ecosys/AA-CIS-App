"""AA-1142 follow-up (S293; CIS 494) — the settings of the external checks of the Ecosystem status page, on
Platform › Settings (area "ops").

The checks run from two vantage points (``acp_shared.ops_external_check.source``, CIS 493):

- ``worker``: the CIS worker's monitoring job ``ops_external_check`` (services/jobs/ops_external_check_job.py), every
  ``cis.ops.external_check_interval_min`` minutes, against ``cis.ops.external_check_targets``, each step waiting at
  most ``cis.ops.external_check_timeout_s``;
- ``github``: the root repo workflow External checks (GitHub Actions), copied by the 5-minute status sampler.

``cis.ops.external_check_sources`` lists the sources in use: a source left out is not run (worker) or not copied
(github), and the page ignores its rows.

``shared.settings.get`` serves the last snapshot and the compiled default before the first refresh. A read that still
fails (a key not compiled yet, a broken definition) returns the seeded default here, so the sampler and the job never
stop on a settings read. :func:`check_targets` validates the target list (the settings reader only knows it is JSON):
a bad entry is skipped and named, never checked. Pure apart from the reads.
"""
from __future__ import annotations

import re
from typing import Any

import structlog

from shared import settings

SOURCES_KEY = "cis.ops.external_check_sources"
INTERVAL_MIN_KEY = "cis.ops.external_check_interval_min"
TIMEOUT_S_KEY = "cis.ops.external_check_timeout_s"
TARGETS_KEY = "cis.ops.external_check_targets"

ALL = (SOURCES_KEY, INTERVAL_MIN_KEY, TIMEOUT_S_KEY, TARGETS_KEY)

SOURCES = ("worker", "github")
APPS = ("cis", "tripplanner", "booking", "crm", "shared")
MODES = ("health", "tls")
_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_PATH = re.compile(r"^/[\x21-\x7e]{0,500}$")

logger = structlog.get_logger()
_warned: set[str] = set()


def _seeded(key: str) -> Any:
    """The CIS 494 seed value of a key (the compiled default); None when not even that can be read."""
    try:
        return settings.definition(key).default_value
    except Exception:  # noqa: BLE001 — the caller falls back to its own seed
        return None


def _read(key: str, fallback: Any) -> Any:
    try:
        return settings.get(key)
    except Exception as e:  # noqa: BLE001 — a settings read never stops the sampler or the job
        if key not in _warned:
            _warned.add(key)
            logger.warning("ops_external_setting_unreadable", key=key, error=f"{type(e).__name__}: {e}"[:200])
        seeded = _seeded(key)
        return fallback if seeded is None else seeded


def sources() -> tuple[str, ...]:
    """The sources in use (cis.ops.external_check_sources), in the fixed order worker, github; unknown names dropped."""
    raw = _read(SOURCES_KEY, list(SOURCES))
    listed = set(raw) if isinstance(raw, list) else set(SOURCES)
    return tuple(s for s in SOURCES if s in listed)


def interval_min() -> int:
    """Minutes between two worker runs (cis.ops.external_check_interval_min), at least 1."""
    try:
        return max(1, int(_read(INTERVAL_MIN_KEY, 15)))
    except (TypeError, ValueError):
        return 15


def timeout_s() -> float:
    """Seconds one step of one target may take (cis.ops.external_check_timeout_s), at least 1."""
    try:
        return float(max(1, int(_read(TIMEOUT_S_KEY, 10))))
    except (TypeError, ValueError):
        return 10.0


def _problem(t: Any) -> str | None:
    """Why one target entry cannot be checked; None when it can. Pure."""
    if not isinstance(t, dict):
        return "not an object"
    if not (isinstance(t.get("id"), str) and _TARGET.match(t["id"])):
        return "bad id"
    if not (isinstance(t.get("host"), str) and _HOST.match(t["host"])):
        return "bad host"
    if t.get("app") not in APPS:
        return "unknown app"
    if t.get("mode") not in MODES:
        return "mode must be health or tls"
    if t["mode"] == "health":
        if not (isinstance(t.get("path"), str) and _PATH.match(t["path"])):
            return "a health target needs a path starting with /"
        expect = t.get("expect")
        if not (isinstance(expect, list) and expect and all(
                isinstance(c, int) and not isinstance(c, bool) and 100 <= c <= 599 for c in expect)):
            return "expect must be a list of HTTP statuses"
    return None


def check_targets(raw: Any) -> tuple[list[dict], list[dict]]:
    """(targets to check, skipped entries with the reason) of a target list; a duplicate id keeps the first. Pure."""
    if not isinstance(raw, list):
        return [], [{"id": None, "problem": "the target list is not a list"}]
    ok: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for t in raw:
        problem = _problem(t)
        tid = t.get("id") if isinstance(t, dict) and isinstance(t.get("id"), str) else None
        if problem is None and tid in seen:
            problem = "duplicate id"
        if problem:
            skipped.append({"id": (tid or "")[:60] or None, "problem": problem})
            continue
        seen.add(tid)
        entry = {"id": t["id"], "app": t["app"], "host": t["host"], "mode": t["mode"]}
        if t["mode"] == "health":
            entry.update(path=t["path"], expect=[int(c) for c in t["expect"]])
        ok.append(entry)
    return ok, skipped


def targets() -> tuple[list[dict], list[dict]]:
    """The targets in force (cis.ops.external_check_targets), checked: (targets, skipped)."""
    return check_targets(_read(TARGETS_KEY, []))

"""Settings reader (S279 thresholds lane L1; root audit docs/audits/2026-11-13-S279-session/thresholds-audit/ 5.2).

One small reader for the admin-editable settings registry (``<schema>.setting_definition`` + ``<schema>.setting_value``,
CIS migration 407). Canonical in AA-CIS-App ``shared/settings/reader.py``; written so AA-Partner and AA-TripPlanner-Web
can vendor a byte copy (standard library only, no relative import of CIS code, the database is any asyncpg-like pool).

Resolution of ``get(key, tenant_id=None)``:
  1. the definition's ``env_override`` env var, when set and valid (an emergency override for one release; logged as a
     warning once per process and shown as a badge on the admin page);
  2. the approved value of the tenant (``tenant_id`` given, ``scope_kind`` platform+tenant) whose ``effective_from`` has
     come;
  3. the approved platform value whose ``effective_from`` has come;
  4. the definition's ``default_value`` (the compiled defaults, generated from the migration seed; CI keeps them equal).

Cache: one snapshot of every approved value of the app, refreshed when older than the smallest ``cache_s`` of the
definitions (30 s), by ``refresh()`` (``aget`` calls it; a background task can too). ``get`` never touches the database:
it reads the snapshot, so sync code (a LangGraph node, a dataclass default) can call it.

Safety:
  - a database error (or the tables not migrated yet) keeps the last good snapshot; with none, the defaults;
  - a stored value that breaks its definition (type, bounds, allowed values) is refused: the default is served and the
    ``alert`` callback runs once per (key, version) so an operator hears about it;
  - an env value that breaks its definition is ignored (warning), never served.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

try:  # structlog when the app has it (CIS, CRM, TripPlanner); the standard library otherwise
    import structlog

    _log = structlog.get_logger("settings")
    _STRUCTURED = True
except Exception:  # pragma: no cover - a vendored copy without structlog
    _log = logging.getLogger("settings")
    _STRUCTURED = False

READER_VERSION = "1.0.0"
NUMERIC_TYPES = ("int", "numeric", "duration_s", "usd", "pct")
WHOLE_TYPES = ("int", "duration_s")
VALUE_TYPES = NUMERIC_TYPES + ("bool", "enum", "list", "json")
SOURCES = ("env", "tenant", "platform", "default")
REFRESH_TIMEOUT_S = 3.0  # technical-constant: one snapshot query; a slower DB keeps the last good snapshot


def _emit(level: str, event: str, **kw: Any) -> None:
    if _STRUCTURED:
        getattr(_log, level)(event, **kw)
    else:  # pragma: no cover
        getattr(_log, level)("%s %s", event, kw)


@dataclass(frozen=True)
class Definition:
    key: str
    app: str
    area: str
    label: str
    description: str
    value_type: str
    default_value: Any
    unit: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allowed_values: Optional[tuple] = None
    scope_kind: str = "platform"
    impact: str = "medium"
    approval: str = "maker_checker"
    evidence_required: bool = False
    env_override: Optional[str] = None
    cache_s: int = 30
    enforced: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Definition":
        allowed = d.get("allowed_values")
        return cls(
            key=d["key"], app=d["app"], area=d["area"], label=d["label"], description=d["description"],
            value_type=d["value_type"], default_value=d["default_value"], unit=d.get("unit"),
            min_value=None if d.get("min_value") is None else float(d["min_value"]),
            max_value=None if d.get("max_value") is None else float(d["max_value"]),
            allowed_values=None if allowed is None else tuple(allowed),
            scope_kind=d.get("scope_kind") or "platform", impact=d.get("impact") or "medium",
            approval=d.get("approval") or "maker_checker", evidence_required=bool(d.get("evidence_required")),
            env_override=d.get("env_override"), cache_s=int(d.get("cache_s") or 30),
            enforced=d.get("enforced", True) is not False,
        )

    @property
    def tenant_scoped(self) -> bool:
        return self.scope_kind == "platform+tenant"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _bounds_problem(d: Definition, n: float) -> Optional[str]:
    if d.min_value is not None and n < d.min_value:
        return f"value is below the minimum {_fmt(d.min_value)}"
    if d.max_value is not None and n > d.max_value:
        return f"value is above the maximum {_fmt(d.max_value)}"
    return None


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def validate(d: Definition, value: Any) -> Optional[str]:
    """Why ``value`` (JSON-decoded) does not fit ``d``; None when it fits. The same rules as the SQL function
    ``shared.setting_value_problem`` (CIS 407), so the API answers 422 with the reason before the trigger would."""
    t = d.value_type
    if t in NUMERIC_TYPES:
        if not _is_number(value):
            return "value must be a number"
        if t in WHOLE_TYPES and float(value) != int(value):
            return "value must be a whole number"
        return _bounds_problem(d, float(value))
    if t == "bool":
        return None if isinstance(value, bool) else "value must be true or false"
    if t == "enum":
        if not isinstance(value, str) or value not in (d.allowed_values or ()):
            return f"value must be one of {list(d.allowed_values or ())}"
        return None
    if t == "list":
        if not isinstance(value, list):
            return "value must be a list"
        if d.allowed_values is not None:
            for item in value:
                if item not in d.allowed_values:
                    return f"list item {json.dumps(item)} is not allowed"
        return None
    if t == "json":
        if not isinstance(value, (dict, list)):
            return "value must be a JSON object or list"
        if isinstance(value, dict):
            for item in value.values():
                if not _is_number(item):
                    if d.min_value is not None or d.max_value is not None:
                        return "every entry must be a number"
                    continue
                problem = _bounds_problem(d, float(item))
                if problem:
                    lo = _fmt(d.min_value) if d.min_value is not None else "-"
                    hi = _fmt(d.max_value) if d.max_value is not None else "-"
                    return f"entry {json.dumps(item)} is outside {lo}..{hi}"
        return None
    return f"unknown value type {t}"


def coerce(d: Definition, value: Any) -> Any:
    """A validated JSON value as the Python type a caller uses: int for int / duration_s, float for numeric / usd /
    pct, a fresh copy for list / json (callers may mutate it)."""
    if d.value_type in WHOLE_TYPES:
        return int(value)
    if d.value_type in NUMERIC_TYPES:
        return float(value)
    if d.value_type in ("list", "json"):
        return json.loads(json.dumps(value))
    return value


def parse_text(d: Definition, raw: str) -> tuple[Any, Optional[str]]:
    """An env var (or form) text as a value of ``d``: (value, None) or (None, why not)."""
    text = (raw or "").strip()
    t = d.value_type
    try:
        if t in WHOLE_TYPES:
            value: Any = int(text)
        elif t in NUMERIC_TYPES:
            value = float(text)
        elif t == "bool":
            low = text.lower()
            if low not in ("true", "false", "1", "0", "on", "off", "yes", "no"):
                return None, "value must be true or false"
            value = low in ("true", "1", "on", "yes")
        elif t == "enum":
            value = text
        else:
            value = json.loads(text)
    except (TypeError, ValueError):
        return None, f"not a {t} value"
    problem = validate(d, value)
    return (None, problem) if problem else (value, None)


@dataclass
class Resolution:
    key: str
    value: Any
    source: str                       # env | tenant | platform | default
    version: Optional[int] = None     # the approved row's version (tenant / platform)
    env_name: Optional[str] = None    # the env var that overrides it (source env)
    env_ignored: Optional[str] = None  # why a set env var was ignored
    refused: Optional[str] = None     # why a stored value was refused (out of bounds)


@dataclass
class _Row:
    value: Any
    version: int
    effective_from: Any = None


AlertFn = Callable[[str, str, Any, Optional[int]], Awaitable[None]]

SNAPSHOT_SQL = """
    SELECT DISTINCT ON (v.key, v.tenant_id)
           v.key, v.tenant_id::text AS tenant_id, v.value::text AS value_text, v.version, v.effective_from
      FROM {schema}.setting_value v
     WHERE v.status = 'approved' AND v.effective_from <= now() AND v.key LIKE $1
     ORDER BY v.key, v.tenant_id, v.effective_from DESC, v.version DESC
"""


@dataclass
class SettingsReader:
    """The process-wide reader of one app's settings (see the module docstring)."""

    definitions: Iterable[Any]
    key_prefix: str = "cis."
    schema: str = "shared"
    alert: Optional[AlertFn] = None
    clock: Callable[[], float] = time.monotonic
    environ: Any = None
    _defs: dict = field(default_factory=dict, init=False)
    _snapshot: Optional[dict] = field(default=None, init=False)
    _loaded_at: float = field(default=-1e18, init=False)
    _pool: Any = field(default=None, init=False)
    _lock: Optional[asyncio.Lock] = field(default=None, init=False)
    _warned: set = field(default_factory=set, init=False)
    _alerted: set = field(default_factory=set, init=False)
    _refused: dict = field(default_factory=dict, init=False)
    last_error: Optional[str] = field(default=None, init=False)

    def __post_init__(self) -> None:
        for d in self.definitions:
            defn = d if isinstance(d, Definition) else Definition.from_dict(d)
            problem = validate(defn, defn.default_value)
            if problem:  # a broken compiled default must fail the build, never serve
                raise ValueError(f"setting {defn.key}: default {defn.default_value!r}: {problem}")
            self._defs[defn.key] = defn
        self.max_age_s = min((d.cache_s for d in self._defs.values()), default=30)

    # ── definitions ──────────────────────────────────────────────────────────────────────────────────────────────
    def definition(self, key: str) -> Definition:
        try:
            return self._defs[key]
        except KeyError:
            raise KeyError(f"unknown setting {key!r} (not in the compiled defaults)") from None

    def keys(self) -> list[str]:
        return sorted(self._defs)

    # ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _env(self) -> Any:
        return os.environ if self.environ is None else self.environ

    def env_value(self, d: Definition) -> tuple[Any, Optional[str], bool]:
        """(value, why ignored, is set) of the definition's env override."""
        if not d.env_override:
            return None, None, False
        raw = self._env().get(d.env_override)
        if raw is None or not str(raw).strip():
            return None, None, False
        value, problem = parse_text(d, str(raw))
        warn_key = (d.key, "env", str(raw))
        if warn_key not in self._warned:
            self._warned.add(warn_key)
            if problem:
                _emit("warning", "setting_env_override_ignored", key=d.key, env=d.env_override, problem=problem)
            else:
                _emit("warning", "setting_env_override_in_force", key=d.key, env=d.env_override, value=value)
        return (None, problem, True) if problem else (value, None, True)

    def resolve(self, key: str, tenant_id: Optional[str] = None) -> Resolution:
        d = self.definition(key)
        value, ignored, _set = self.env_value(d)
        if ignored is None and _set:
            return Resolution(key, coerce(d, value), "env", env_name=d.env_override)
        snap = self._snapshot or {}
        refused = self._refused.get(key)
        if tenant_id is not None and d.tenant_scoped:
            row = snap.get((key, str(tenant_id)))
            if row is not None:
                return Resolution(key, coerce(d, row.value), "tenant", version=row.version, env_ignored=ignored)
        row = snap.get((key, None))
        if row is not None:
            return Resolution(key, coerce(d, row.value), "platform", version=row.version, env_ignored=ignored)
        return Resolution(key, coerce(d, d.default_value), "default", env_ignored=ignored, refused=refused)

    def get(self, key: str, tenant_id: Optional[str] = None) -> Any:
        """The value in force (sync; reads the last snapshot, never the database)."""
        return self.resolve(key, tenant_id).value

    async def aget(self, key: str, tenant_id: Optional[str] = None) -> Any:
        """``get`` after a refresh when the snapshot is older than the cache window."""
        await self.refresh()
        return self.get(key, tenant_id)

    # ── refresh ──────────────────────────────────────────────────────────────────────────────────────────────────
    def install(self, pool: Any) -> None:
        """Give the reader the pool it reads through (its own connection, never the caller's transaction)."""
        self._pool = pool

    def stale(self) -> bool:
        return self.clock() - self._loaded_at >= self.max_age_s

    def invalidate(self) -> None:
        """Make the next ``aget`` / ``refresh`` read the database (after an approval in this process)."""
        self._loaded_at = -1e18

    async def refresh(self, force: bool = False, pool: Any = None) -> bool:
        """Re-read every approved value when stale (or ``force``). True when the snapshot is fresh after the call;
        False when it kept the last good one (no pool, DB error, timeout)."""
        pool = pool or self._pool
        if pool is None:
            return False
        if not force and not self.stale():
            return True
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not force and not self.stale():
                return True
            try:
                rows = await asyncio.wait_for(self._fetch(pool), timeout=REFRESH_TIMEOUT_S)
            except Exception as exc:  # keep the last good snapshot; with none the defaults stay in force
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self._loaded_at = self.clock()  # retry after one cache window, not on every call
                _emit("warning", "settings_refresh_failed", error=self.last_error,
                      kept="last_good" if self._snapshot is not None else "defaults")
                return False
            snap, refused = {}, {}
            for r in rows:
                d = self._defs.get(r["key"])
                if d is None:
                    continue  # a key a newer migration added: this code does not read it yet
                try:
                    value = json.loads(r["value_text"])
                except (TypeError, ValueError):
                    value, problem = None, "stored value is not JSON"
                else:
                    problem = validate(d, value)
                tenant = r["tenant_id"]
                if tenant is not None and not d.tenant_scoped:
                    problem = problem or f"a tenant value of a {d.scope_kind} setting"
                if problem:
                    refused[d.key] = problem
                    await self._alert_once(d.key, problem, r["value_text"], r["version"])
                    continue
                snap[(d.key, tenant)] = _Row(value=value, version=int(r["version"]),
                                             effective_from=r["effective_from"])
            self._snapshot, self._refused = snap, refused
            self._loaded_at = self.clock()
            self.last_error = None
            return True

    async def _fetch(self, pool: Any) -> list:
        async with pool.acquire() as conn:
            return list(await conn.fetch(SNAPSHOT_SQL.format(schema=self.schema), self.key_prefix + "%"))

    async def _alert_once(self, key: str, problem: str, raw: Any, version: Optional[int]) -> None:
        mark = (key, version)
        if mark in self._alerted:
            return
        self._alerted.add(mark)
        _emit("error", "setting_value_refused", key=key, problem=problem, version=version)
        if self.alert is None:
            return
        try:
            await self.alert(key, problem, raw, version)
        except Exception as exc:  # an alert failure never breaks a read
            _emit("warning", "setting_alert_failed", key=key, error=repr(exc)[:200])

    # ── background refresh ───────────────────────────────────────────────────────────────────────────────────────
    async def run_refresher(self, interval_s: Optional[float] = None) -> None:
        """Refresh every ``interval_s`` (default the cache window) until cancelled."""
        while True:
            await self.refresh(force=True)
            await asyncio.sleep(interval_s or self.max_age_s)


__all__ = ["Definition", "NUMERIC_TYPES", "READER_VERSION", "Resolution", "SNAPSHOT_SQL", "SOURCES", "SettingsReader",
           "VALUE_TYPES", "coerce", "parse_text", "validate"]

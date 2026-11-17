"""The in-process caches of the LLM gateway (``shared.llm_client.*``) back to their import-time (cold) state.

Why (S283, nightly random order): every ``shared.llm_client`` registry reader keeps a process-wide cache, and on a
failed reload it keeps the last good copy (``connections._load``: "a DB outage keeps today's paths working");
``invalidate()`` only clears the timestamps, never the rows. A module that lets the readers reach the test DB
(test_aa1042_guide_db / test_aa1042_route_control_db patch ``asyncpg.connect`` to drop ``ssl``) therefore left the
registry rows of ITS database copy in the process for every later module: CI's own ``DATABASE_URL`` is refused
(``ssl="require"`` on a non-SSL postgres service), so the next reload fails and the stale rows stay.
test_aa1002_migration_304 then saw acc1 (CIS 406) in ``catalog.serving_accounts()`` and failed whenever one of those
modules ran before it.

The conftest gives every module a fresh copy of the replayed database; ``reset()`` gives it a cold gateway too, so the
caches never describe another module's database. Generic on purpose: a new cache global in ``shared.llm_client`` is
covered without editing a list (tests/integration/test_s283_llm_cache_reset.py checks the coverage).
"""
import copy
import importlib
import logging
import pkgutil
import re
import types

import shared.llm_client as _pkg

# Lower-case private module globals of these types are caches or state (constants are UPPER_CASE).
_CACHE_NAME = re.compile(r"_[a-z][a-z0-9_]*")
_CACHE_TYPES = (dict, list, set, frozenset, tuple, float, int, str, bool, type(None))

_snapshot: dict[str, dict[str, object]] = {}      # module name -> {global name: import-time value}
logger = logging.getLogger(__name__)


def _cache_globals(mod: types.ModuleType) -> dict[str, object]:
    return {name: value for name, value in vars(mod).items()
            if _CACHE_NAME.fullmatch(name) and type(value) in _CACHE_TYPES}


def snapshot() -> dict[str, dict[str, object]]:
    """Import every ``shared.llm_client`` module and record its cache globals. Call before the first test (the
    modules are then cold); later calls keep the first record."""
    if _snapshot:
        return _snapshot
    for info in pkgutil.walk_packages(_pkg.__path__, prefix=_pkg.__name__ + "."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001 — a module the suite cannot import has no state to reset
            logger.warning("llm cache snapshot: cannot import %s: %s", info.name, e)
            continue
        _snapshot[info.name] = {k: copy.copy(v) for k, v in _cache_globals(mod).items()}
    _snapshot[_pkg.__name__] = {k: copy.copy(v) for k, v in _cache_globals(_pkg).items()}
    return _snapshot


def reset() -> None:
    """Put every recorded cache global back to its import-time value. A container of the same type is refilled in
    place (a reference another module holds stays valid), anything else is rebound."""
    for mod_name, saved in snapshot().items():
        mod = importlib.import_module(mod_name)
        for name, cold in saved.items():
            current = getattr(mod, name, None)
            if type(current) is type(cold) and isinstance(cold, (dict, set)):
                current.clear()
                current.update(cold)
            elif type(current) is type(cold) and isinstance(cold, list):
                current[:] = cold
            else:
                setattr(mod, name, copy.copy(cold))

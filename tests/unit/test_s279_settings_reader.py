"""S279 thresholds L1 — the settings reader (shared/settings/reader.py, the vendorable part; audit section 5.2).

Resolution order (env override -> approved tenant row -> approved platform row -> default), validation (the same rules
as the SQL function of CIS 407), the cache window, the last-good snapshot on a DB error, the refusal of a stored value
that breaks its definition (default served, one alert per key and version), an invalid env value ignored, and the CIS
binding (shared/settings: the compiled defaults load and every key starts with "cis."). No DB: a fake pool."""
import json

import pytest

from shared import settings
from shared.settings.reader import Definition, SettingsReader, coerce, parse_text, validate

TENANT = "00000000-0000-4000-8000-00000000a279"

DEFS = [
    {"key": "cis.zz.cap", "app": "cis", "area": "zz", "label": "Zz cap", "description": "A test cap description.",
     "value_type": "int", "unit": "calls", "min_value": 1, "max_value": 10, "allowed_values": None,
     "default_value": 5, "scope_kind": "platform+tenant", "impact": "high", "approval": "maker_checker",
     "evidence_required": False, "env_override": "ZZ_S279_CAP", "cache_s": 30, "enforced": True},
    {"key": "cis.zz.mode", "app": "cis", "area": "zz", "label": "Zz mode", "description": "A test mode description.",
     "value_type": "enum", "allowed_values": ["soft", "hard"], "default_value": "soft"},
    {"key": "cis.zz.price", "app": "cis", "area": "zz", "label": "Zz price", "description": "A test price table.",
     "value_type": "json", "min_value": 0, "max_value": 1, "default_value": {"a": 0.5}},
    {"key": "cis.zz.share", "app": "cis", "area": "zz", "label": "Zz share", "description": "A test share setting.",
     "value_type": "numeric", "min_value": 0.8, "max_value": 1.0, "default_value": 0.9, "cache_s": 60},
]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Pool:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *a):
                return False
        return _Ctx()

    async def fetch(self, sql, *args):
        self.calls += 1
        assert "status = 'approved'" in sql and "effective_from <= now()" in sql and args == ("cis.%",)
        if self.error:
            raise self.error
        return self.rows


def _row(key, value, version=1, tenant=None):
    return {"key": key, "tenant_id": tenant, "value_text": json.dumps(value), "version": version,
            "effective_from": None}


def _reader(env=None, alert=None):
    return SettingsReader(DEFS, environ=env or {}, clock=_Clock(), alert=alert)


# ── validation ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value,ok", [
    ("cis.zz.cap", 1, True), ("cis.zz.cap", 10, True), ("cis.zz.cap", 0, False), ("cis.zz.cap", 11, False),
    ("cis.zz.cap", 2.5, False), ("cis.zz.cap", "3", False), ("cis.zz.cap", True, False),
    ("cis.zz.mode", "hard", True), ("cis.zz.mode", "block", False),
    ("cis.zz.price", {"a": 0.2, "b": 1}, True), ("cis.zz.price", {"a": 2}, False), ("cis.zz.price", {"a": "x"}, False),
    ("cis.zz.price", 3, False), ("cis.zz.share", 0.85, True), ("cis.zz.share", 0.7, False),
])
def test_validate(key, value, ok):
    d = Definition.from_dict(next(x for x in DEFS if x["key"] == key))
    assert (validate(d, value) is None) is ok


def test_a_broken_compiled_default_fails_at_load():
    bad = dict(DEFS[0], default_value=50)
    with pytest.raises(ValueError):
        SettingsReader([bad])


def test_parse_text_and_coerce():
    d = Definition.from_dict(DEFS[0])
    assert parse_text(d, " 7 ") == (7, None)
    assert parse_text(d, "abc")[0] is None and parse_text(d, "99")[1]
    b = Definition.from_dict({**DEFS[1], "value_type": "bool", "allowed_values": None, "default_value": False})
    assert parse_text(b, "on") == (True, None) and parse_text(b, "maybe")[1]
    j = Definition.from_dict(DEFS[2])
    value = coerce(j, {"a": 0.5})
    value["a"] = 9                                      # a copy: the caller may change it
    assert coerce(j, {"a": 0.5}) == {"a": 0.5} and coerce(Definition.from_dict(DEFS[3]), 1) == 1.0


# ── resolution ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolution_order_env_tenant_platform_default():
    env = {}
    r = SettingsReader(DEFS, environ=env, clock=_Clock())
    assert r.resolve("cis.zz.cap").source == "default" and r.get("cis.zz.cap") == 5
    pool = _Pool([_row("cis.zz.cap", 7, 2), _row("cis.zz.cap", 9, 1, tenant=TENANT)])
    assert await r.refresh(pool=pool) is True
    assert (r.get("cis.zz.cap"), r.resolve("cis.zz.cap").source, r.resolve("cis.zz.cap").version) == (7, "platform", 2)
    assert (r.get("cis.zz.cap", TENANT), r.resolve("cis.zz.cap", TENANT).source) == (9, "tenant")
    assert r.get("cis.zz.cap", "00000000-0000-4000-8000-000000000000") == 7     # another tenant: the platform row
    env["ZZ_S279_CAP"] = "3"
    res = r.resolve("cis.zz.cap", TENANT)
    assert (res.value, res.source, res.env_name) == (3, "env", "ZZ_S279_CAP")
    env["ZZ_S279_CAP"] = "300"                           # out of bounds: ignored, never served
    res = r.resolve("cis.zz.cap")
    assert (res.value, res.source) == (7, "platform") and "maximum" in res.env_ignored


def test_unknown_key_is_a_key_error():
    with pytest.raises(KeyError):
        _reader().get("cis.zz.nope")


@pytest.mark.asyncio
async def test_a_tenant_value_of_a_platform_setting_is_refused():
    alerts = []

    async def alert(key, problem, raw, version):
        alerts.append((key, version))
    r = _reader(alert=alert)
    await r.refresh(pool=_Pool([_row("cis.zz.mode", "hard", 1, tenant=TENANT)]))
    assert r.get("cis.zz.mode", TENANT) == "soft" and alerts == [("cis.zz.mode", 1)]


# ── cache, last good, refusal ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_window_is_the_smallest_cache_s():
    r = _reader()
    pool = _Pool([_row("cis.zz.cap", 7)])
    assert r.max_age_s == 30
    await r.refresh(pool=pool)
    await r.refresh(pool=pool)                            # fresh: no second read
    assert pool.calls == 1
    r.clock.t += 31
    pool.rows = [_row("cis.zz.cap", 8, 2)]
    r.install(pool)
    assert await r.aget("cis.zz.cap") == 8 and pool.calls == 2
    r.invalidate()
    pool.rows = [_row("cis.zz.cap", 4, 3)]
    assert await r.aget("cis.zz.cap") == 4


@pytest.mark.asyncio
async def test_a_db_error_keeps_the_last_good_snapshot_then_retries_after_a_window():
    r = _reader()
    await r.refresh(pool=_Pool([_row("cis.zz.cap", 7)]))
    broken = _Pool(error=OSError("connection refused"))
    assert await r.refresh(force=True, pool=broken) is False
    assert r.get("cis.zz.cap") == 7 and "connection refused" in r.last_error
    assert await r.refresh(pool=broken) is True           # within the window: no hammering of a broken DB
    assert broken.calls == 1
    fresh = _reader()
    assert await fresh.refresh(pool=broken) is False and fresh.get("cis.zz.cap") == 5   # no snapshot: the default


@pytest.mark.asyncio
async def test_no_pool_serves_the_defaults():
    r = _reader()
    assert await r.refresh() is False and await r.aget("cis.zz.mode") == "soft"


@pytest.mark.asyncio
async def test_an_out_of_bounds_stored_value_is_refused_and_alerted_once():
    alerts = []

    async def alert(key, problem, raw, version):
        alerts.append((key, problem, version))
    r = _reader(alert=alert)
    pool = _Pool([_row("cis.zz.cap", 99, 4), _row("cis.zz.share", 0.95, 2)])
    await r.refresh(pool=pool)
    res = r.resolve("cis.zz.cap")
    assert (res.value, res.source) == (5, "default") and "maximum" in res.refused
    assert r.get("cis.zz.share") == 0.95
    await r.refresh(force=True, pool=pool)
    assert alerts == [("cis.zz.cap", "value is above the maximum 10", 4)]
    pool.rows = [{"key": "cis.zz.cap", "tenant_id": None, "value_text": "not json", "version": 5,
                  "effective_from": None}]
    await r.refresh(force=True, pool=pool)
    assert alerts[-1][2] == 5 and r.get("cis.zz.cap") == 5


@pytest.mark.asyncio
async def test_an_alert_failure_never_breaks_a_read():
    async def alert(*a):
        raise RuntimeError("mail down")
    r = _reader(alert=alert)
    await r.refresh(pool=_Pool([_row("cis.zz.cap", 99)]))
    assert r.get("cis.zz.cap") == 5


@pytest.mark.asyncio
async def test_a_key_a_newer_migration_added_is_ignored():
    r = _reader()
    await r.refresh(pool=_Pool([_row("cis.zz.future", 1), _row("cis.zz.cap", 6)]))
    assert r.get("cis.zz.cap") == 6


# ── the CIS binding ──────────────────────────────────────────────────────────────────────────────────────────────

def test_cis_binding_loads_the_compiled_defaults():
    keys = settings.READER.keys()
    assert keys and all(k.startswith(settings.KEY_PREFIX) for k in keys)
    # CIS 414 (S279 L5): the TripPlanner keys live in the same table, so the binding reads every key of it
    assert settings.KEY_PREFIX == "" and {settings.definition(k).app for k in keys} == {"cis", "tripplanner"}
    assert settings.get("cis.publish.daily_limit_default") == 20
    assert settings.definition("cis.content.trash_retention_days").enforced is False


def test_start_refresher_is_off_under_test(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert settings.start_refresher(object()) is None
    settings.install(None)


def test_the_reader_is_standard_library_only():
    # vendorable by AA-Partner and AA-TripPlanner-Web: no import of CIS code (structlog is optional)
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(settings.__file__).with_name("reader.py").read_text())
    mods = {(n.module or "") if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not {m for m in mods if m.split(".")[0] in ("shared", "services", "api")}, mods

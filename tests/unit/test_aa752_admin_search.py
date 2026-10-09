# tests/unit/test_aa752_admin_search.py
# AA-752 — unit tests for the global admin search (the backend of the Cmd-K palette).
#
# The router builds its SQL + params with pure helpers so the query shape, parameters, LIKE
# escaping and caps are testable without a database. We also exercise the endpoint's short-q
# guard (q shorter than MIN_Q_LEN returns an empty bundle and never touches the DB).

import asyncio

import pytest

from api.routers import admin_search as mod


# escape_like

def test_escape_like_escapes_wildcards_and_backslash():
    # Backslash first so the %/_ escapes are not themselves re-escaped.
    assert mod.escape_like("a_b%c") == "a\\_b\\%c"
    assert mod.escape_like("back\\slash") == "back\\\\slash"
    assert mod.escape_like("plain") == "plain"


# _tours_query

def test_tours_query_shape_params_and_cap():
    sql, params = mod._tours_query("Kyoto", mod.TOURS_CAP)
    assert "silver_aa_internal.raw_tours" in sql
    assert "LEFT JOIN gold_aa_internal.published_tours" in sql
    assert "rt.src_name ILIKE $1" in sql
    assert "pt.aa_name ILIKE $1" in sql
    assert "rt.deleted_at IS NULL" in sql
    assert "ESCAPE '\\'" in sql
    assert "LIMIT $2" in sql
    assert params == ["%Kyoto%", mod.TOURS_CAP]


def test_tours_query_escapes_user_wildcards():
    _, params = mod._tours_query("a_b%c", 8)
    assert params[0] == "%a\\_b\\%c%"


# _tenants_query

def test_tenants_query_shape_params_and_excludes_master_sentinel():
    sql, params = mod._tenants_query("acme", mod.TENANTS_CAP)
    assert "shared.tenants" in sql
    assert "name ILIKE $1" in sql
    assert "slug ILIKE $1" in sql
    assert "tenant_id <> $3::uuid" in sql
    assert "LIMIT $2" in sql
    assert params == ["%acme%", mod.TENANTS_CAP, mod._MASTER_TENANT_ID]


# _jobs_query

def test_jobs_query_id_prefix_or_kind_substring():
    sql, params = mod._jobs_query("a3", mod.JOBS_CAP)
    assert "shared.job" in sql
    assert "id::text ILIKE $1" in sql
    assert "kind ILIKE $2" in sql
    assert "LIMIT $3" in sql
    assert params == ["a3%", "%a3%", mod.JOBS_CAP]


def test_jobs_query_escapes_user_wildcards():
    _, params = mod._jobs_query("x_y", 5)
    assert params[0] == "x\\_y%"
    assert params[1] == "%x\\_y%"


# caps

def test_caps_are_the_documented_values():
    assert mod.TOURS_CAP == 8
    assert mod.TENANTS_CAP == 5
    assert mod.JOBS_CAP == 5


# short-q guard on the endpoint

class _FailPool:
    """Any DB access is a test failure — a short query must not hit the DB."""

    def acquire(self):  # pragma: no cover - must never be called
        raise AssertionError("short query must not touch the DB")


class _FakeRequest:
    def __init__(self, pool):
        self.app = type("App", (), {"state": type("S", (), {"pool": pool})()})()


@pytest.fixture(autouse=True)
def _stub_admin_secret(monkeypatch):
    monkeypatch.setattr(mod, "verify_admin_secret", lambda *_a, **_k: None)


@pytest.mark.parametrize("q", ["", " ", "a", "  x "])
def test_short_or_blank_q_returns_empty_without_db(q):
    req = _FakeRequest(_FailPool())
    out = asyncio.run(mod.admin_search(req, q=q, limit=0, x_admin_secret="x"))
    assert out == {"tours": [], "tenants": [], "jobs": []}


def test_two_char_q_is_long_enough():
    # A 2-char term passes the guard, so it tries to acquire a connection (the fail-pool proves the
    # guard let it through — a real DB is not needed for this assertion).
    req = _FakeRequest(_FailPool())
    with pytest.raises(AssertionError, match="must not touch the DB"):
        asyncio.run(mod.admin_search(req, q="ab", limit=0, x_admin_secret="x"))

"""AA-1142 follow-up (S293; CIS 493) — the settings of the worker's external checks: the readers serve the compiled
defaults, fall back to the seed when a read fails, and the target list is validated entry by entry (a bad entry is
skipped and named, never checked). Invented host names only."""
import pytest

from shared import ops_external_settings as xs
from shared import settings


def test_the_readers_serve_the_compiled_defaults():
    assert xs.sources() == ("worker", "github")
    assert xs.interval_min() == 15 and xs.timeout_s() == 10.0
    ok, skipped = xs.targets()
    assert skipped == [] and len(ok) >= 1
    assert {t["mode"] for t in ok} <= {"health", "tls"}
    assert all(t["path"].startswith("/") and t["expect"] for t in ok if t["mode"] == "health")


def test_every_key_is_compiled_and_enforced():
    for key in xs.ALL:
        d = settings.definition(key)
        assert d.app == "cis" and d.area == "ops" and d.enforced, key


def test_a_failing_read_falls_back_to_the_seed(monkeypatch):
    def boom(key):
        raise RuntimeError("zz no snapshot")

    monkeypatch.setattr(xs.settings, "get", boom)
    assert xs.sources() == ("worker", "github") and xs.interval_min() == 15 and xs.timeout_s() == 10.0
    assert xs.targets()[0]


@pytest.mark.parametrize("value, expected", [
    (["github"], ("github",)), (["github", "worker"], ("worker", "github")), ([], ()), (["zz"], ()),
    ("worker", ("worker", "github")),
])
def test_sources_keep_a_fixed_order_and_drop_unknown_names(monkeypatch, value, expected):
    monkeypatch.setattr(xs.settings, "get", lambda key: value)
    assert xs.sources() == expected


GOOD = {"id": "zz-api", "app": "cis", "host": "api.zz-example.test", "mode": "health", "path": "/health",
        "expect": [200]}


@pytest.mark.parametrize("entry, problem", [
    ("zz", "not an object"),
    ({**GOOD, "id": "ZZ"}, "bad id"),
    ({**GOOD, "host": "zz host"}, "bad host"),
    ({**GOOD, "app": "zz"}, "unknown app"),
    ({**GOOD, "mode": "ping"}, "mode must be health or tls"),
    ({**GOOD, "path": "health"}, "a health target needs a path starting with /"),
    ({**GOOD, "expect": []}, "expect must be a list of HTTP statuses"),
    ({**GOOD, "expect": [True]}, "expect must be a list of HTTP statuses"),
    ({**GOOD, "expect": [700]}, "expect must be a list of HTTP statuses"),
])
def test_a_bad_target_is_skipped_and_named(entry, problem):
    ok, skipped = xs.check_targets([GOOD, entry])
    assert ok == [GOOD] and [s["problem"] for s in skipped] == [problem]


def test_a_tls_target_needs_no_path_and_a_duplicate_keeps_the_first():
    tls = {"id": "zz-sign-in", "app": "shared", "host": "auth.zz-example.test", "mode": "tls", "path": 5}
    ok, skipped = xs.check_targets([GOOD, tls, {**GOOD, "host": "other.zz-example.test"}])
    assert ok == [GOOD, {"id": "zz-sign-in", "app": "shared", "host": "auth.zz-example.test", "mode": "tls"}]
    assert skipped == [{"id": "zz-api", "problem": "duplicate id"}]


def test_a_list_that_is_not_a_list_checks_nothing():
    assert xs.check_targets({"zz": 1}) == ([], [{"id": None, "problem": "the target list is not a list"}])

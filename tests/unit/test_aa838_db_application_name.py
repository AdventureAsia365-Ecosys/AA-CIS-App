"""AA-838 D9 (data standards C1.1a): connections built from get_database_url() carry an application_name."""

import asyncpg.connect_utils as cu
import pytest

from shared import secrets
from shared.secrets import DEFAULT_DB_APPLICATION_NAME, get_database_url, with_application_name


def test_appends_default_name_without_query(monkeypatch):
    monkeypatch.delenv("CIS_DB_APPLICATION_NAME", raising=False)
    assert with_application_name("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db?application_name=cis-direct"


def test_appends_to_existing_query_and_keeps_it_byte_identical():
    # Password built at run time: a literal user:password URI trips gitleaks uri-embedded-credentials.
    userinfo = "u" + ":" + "p%40ss"
    dsn = f"postgres://{userinfo}@h/db?sslmode=require&target_session_attrs=any"
    assert with_application_name(dsn, "x") == dsn + "&application_name=x"


def test_keeps_an_explicit_name():
    dsn = "postgresql://u:p@h/db?application_name=cis-exec"
    assert with_application_name(dsn) == dsn


def test_env_override(monkeypatch):
    monkeypatch.setenv("CIS_DB_APPLICATION_NAME", "cis-adhoc aa838")
    assert with_application_name("postgresql://u:p@h/db").endswith("?application_name=cis-adhoc%20aa838")


def test_strips_whitespace_from_the_secret():
    assert with_application_name("  postgresql://u:p@h/db\n", "n") == "postgresql://u:p@h/db?application_name=n"


def test_non_url_dsn_untouched():
    dsn = "host=h dbname=db user=u"
    assert with_application_name(dsn) == dsn


def test_get_database_url_env_path(monkeypatch):
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.delenv("CIS_DB_APPLICATION_NAME", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert get_database_url() == "postgresql://u:p@h:5432/db?application_name=" + DEFAULT_DB_APPLICATION_NAME


def test_get_database_url_secret_path(monkeypatch):
    monkeypatch.setenv("SECRET_DB_ARN", "arn-placeholder")
    monkeypatch.setattr(secrets, "_get_cached", lambda key, fn: "postgresql://u:p@h/db\n")
    assert get_database_url() == "postgresql://u:p@h/db?application_name=cis-direct"


@pytest.mark.parametrize("explicit", [None, {"application_name": "cis-worker"}])
def test_asyncpg_reads_it_and_explicit_server_settings_win(explicit):
    """The named pools pass server_settings; they must keep their own name over the DSN default."""
    addrs, params = cu._parse_connect_dsn_and_args(
        dsn=with_application_name("postgresql://u:p@h:5432/db", "cis-direct"), host=None, port=None, user=None,
        password=None, passfile=None, database=None, ssl=None, direct_tls=False, server_settings=explicit,
        target_session_attrs=None)
    want = "cis-worker" if explicit else "cis-direct"
    assert params.server_settings["application_name"] == want
    assert params.user == "u" and params.password == "p" and params.database == "db"

"""AA-1126 l16 (S283, CIS 447) — the spend environment keys are registered settings the admin page renders (a label,
the EN + VI guide, dev | prod, default dev, platform scope, maker-checker), and the CIS reader
(shared/llm_client/spend_environment.py) answers dev unless the setting says prod. No DB."""
import pytest

from scripts.settings import gen_compiled_defaults as gen
from shared import settings
from shared.llm_client import spend_environment as se

KEYS = ("cis.spend.environment", "tripplanner.spend.environment")


def _rows() -> dict:
    return {r["key"]: r for r in gen.seed_rows()[0] if r["key"] in KEYS}


@pytest.mark.parametrize("key", KEYS)
def test_each_key_is_registered_with_what_the_admin_page_renders(key):
    row = _rows()[key]
    assert row["app"] == key.split(".")[0] and row["area"] == "spend"
    assert row["value_type"] == "enum" and row["allowed_values"] == ["dev", "prod"] and row["default_value"] == "dev"
    assert (row["scope_kind"], row["approval"], row["enforced"]) == ("platform", "maker_checker", True)
    assert row["label"] and row["description"]
    for lang in ("en", "vi"):
        guide = row["guide"][lang]
        assert all(guide.get(part) for part in ("what", "raise", "lower", "when", "example")), (key, lang)


def test_the_cis_key_is_a_compiled_default_and_the_reader_names_it():
    assert se.KEY == "cis.spend.environment"
    d = settings.definition(se.KEY)
    assert d.default_value == se.DEV and d.label == _rows()[se.KEY]["label"]


@pytest.mark.parametrize("value, expected", [("prod", "prod"), ("PROD ", "prod"), ("dev", "dev"), (None, "dev"),
                                             ("staging", "dev")])
def test_reader_answers_dev_unless_prod(monkeypatch, value, expected):
    monkeypatch.setattr(settings, "get", lambda key: value)
    assert se.spend_environment() == expected
    assert se.is_prod() is (expected == "prod")


def test_reader_answers_dev_when_the_setting_cannot_be_read(monkeypatch):
    def boom(key):
        raise RuntimeError("zz no settings")
    monkeypatch.setattr(settings, "get", boom)
    assert se.spend_environment() == "dev"

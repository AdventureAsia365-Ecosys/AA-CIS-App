"""AA-642 S8 — migration 323 (TripPlanner gateway contract views), static checks. No DB.

- Contract, not expand (ADR 0017): no "migrate: expand" header, because it grants catalog_owner columns of existing
  tables (relacl). Flipping it to expand would let the Infra pipeline auto-apply a file its post-check refuses.
- The catalog recipe: CREATE on schema catalog granted and revoked, views created as catalog_owner, consumer grants
  given by the views' owner, never RESET ROLE.
- No REVOKE of TripPlanner's grandfathered SELECTs this session (design D10: after a cycle on the views).
- The views and columns are the ones shared/aa_llm/contract.py names (the integration test checks the live columns).
- The conftest replay skips 323 (the integration test applies it the DB Migrate way).
"""
import re
from pathlib import Path

from shared.aa_llm import contract

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "api" / "migrations" / "323_llm_contract_views.sql").read_text(encoding="utf-8")
CODE = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))


def test_contract_class_header():
    assert "migrate: expand" not in SQL
    assert "Class: CONTRACT" in SQL


def test_catalog_recipe_order():
    grant_create = CODE.index("GRANT CREATE ON SCHEMA catalog TO catalog_owner")
    as_owner = CODE.index("SET LOCAL ROLE catalog_owner")
    views = [CODE.index(f"CREATE OR REPLACE VIEW {v}") for v in (contract.GATEWAY_CONFIG_VIEW, contract.APP_SPEND_VIEW)]
    consumer = [CODE.index(f"GRANT SELECT ON {v} TO tripplanner") for v in (contract.GATEWAY_CONFIG_VIEW,
                                                                             contract.APP_SPEND_VIEW)]
    back = CODE.index("SET LOCAL ROLE cis_owner")
    revoke = CODE.index("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")
    assert grant_create < as_owner < min(views) and max(views) < min(consumer) and max(consumer) < back < revoke
    assert "RESET ROLE" not in CODE and "SECURITY DEFINER" not in CODE.upper()


def test_no_revoke_of_the_grandfathered_selects():
    assert not re.search(r"REVOKE\s+SELECT", CODE, re.I)


def test_views_and_columns_match_the_core_contract():
    for col in contract.GATEWAY_CONFIG_COLUMNS + contract.APP_SPEND_COLUMNS:
        assert re.search(rf"\b{col}\b", CODE), col
    assert ",".join(contract.APP_SPEND_COLUMNS) in SQL   # the closing check's expected list


def test_conftest_replay_skips_323():
    conftest = (ROOT / "tests" / "integration" / "conftest.py").read_text(encoding="utf-8")
    assert "and not f.startswith('323')" in conftest

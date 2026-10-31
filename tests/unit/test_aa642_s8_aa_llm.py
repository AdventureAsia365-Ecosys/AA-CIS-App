"""AA-642 S8 (root ADR 0009 decision 13): the shared core ``shared/aa_llm``.

- Pure and vendorable: standard library and relative imports only (no ``shared.secrets`` / ``cost_guard`` /
  ``aws_client``, no provider SDK, no DB driver), so the byte copies in AA-TripPlanner-Web and AA-Partner import it
  under any package name.
- ``MANIFEST.json`` is fresh (the sync and every consumer check the copy against it) and ``check_copy`` finds an
  edited, added or missing file and a lock that names another manifest.
- ``ledger.insert_sql`` / ``LedgerRow.args``: one placeholder per column, the right casts, values clamped to the
  migration 319 CHECKs.
- ``budget.decide``: fail closed, both caps, near-cap ratio, the TripPlanner dict shape.
- ``contract``: the SELECTs of the CIS migration 323 views name their columns (the views are tested with 323).
No DB, no AWS, no vendor.
"""
import ast
import json
import shutil
import sys
from pathlib import Path

import pytest

from shared.aa_llm import budget, check_copy, contract, ledger, pricing

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "shared" / "aa_llm"


# ── purity ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _imports(path: Path) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(0, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module or ""))
    return out


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_imports_only_the_standard_library_and_itself(path):
    stdlib = set(sys.stdlib_module_names)
    for level, module in _imports(path):
        if level > 0:
            continue  # relative: another module of the core
        top = module.split(".")[0]
        assert top in stdlib or top == "__future__", f"{path.name} imports {module!r} (the core must stay pure)"


def test_core_files_are_python_or_the_manifest():
    names = {p.name for p in CORE.iterdir() if p.is_file() and not p.name.startswith(".")}
    assert names == {*(p.name for p in CORE.glob("*.py")), "MANIFEST.json"}
    assert not [p for p in CORE.iterdir() if p.is_dir() and p.name != "__pycache__"], "no sub-packages yet"


# ── manifest and integrity ─────────────────────────────────────────────────────────────────────────────────────────

def test_manifest_is_fresh():
    assert (CORE / "MANIFEST.json").read_text(encoding="utf-8") == check_copy.manifest_text(CORE), (
        "run: python3 shared/aa_llm/check_copy.py --write")
    assert check_copy.problems(CORE) == []


def test_manifest_hashes_sit_on_their_own_line():
    # gitleaks generic-api-key reads a hash next to a key with "token" / "key" in it as a secret (lesson S253).
    for line in (CORE / "MANIFEST.json").read_text(encoding="utf-8").splitlines():
        if '"sha256"' in line:
            assert line.strip().startswith('"sha256":'), line


@pytest.fixture
def copy_dir(tmp_path):
    dest = tmp_path / "aa_llm"
    shutil.copytree(CORE, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def test_check_copy_finds_an_edited_added_and_missing_file(copy_dir):
    assert check_copy.problems(copy_dir) == []
    (copy_dir / "budget.py").write_text("# edited\n", encoding="utf-8")
    (copy_dir / "extra.py").write_text("", encoding="utf-8")
    (copy_dir / "ports.py").unlink()
    found = check_copy.problems(copy_dir)
    assert any(f.startswith("budget.py: sha256 differs") for f in found)
    assert any(f.startswith("extra.py: not listed") for f in found)
    assert any(f.startswith("ports.py: listed") for f in found)


def test_check_copy_ignores_caches_and_dot_files(copy_dir):
    (copy_dir / "__pycache__").mkdir(exist_ok=True)
    (copy_dir / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    (copy_dir / ".ruff_cache").mkdir()
    (copy_dir / ".ruff_cache" / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
    assert check_copy.problems(copy_dir) == []


def test_check_copy_reads_the_lock(copy_dir, tmp_path):
    lock = tmp_path / "aa_llm.lock.json"
    lock.write_text(json.dumps({"manifest_sha256": check_copy.sha256(copy_dir / "MANIFEST.json")}), encoding="utf-8")
    assert check_copy.problems(copy_dir, lock) == []
    lock.write_text(json.dumps({"manifest_sha256": "0" * 64}), encoding="utf-8")
    assert any("manifest_sha256 does not match" in f for f in check_copy.problems(copy_dir, lock))
    lock.write_text("not json", encoding="utf-8")
    assert any("cannot be read" in f for f in check_copy.problems(copy_dir, lock))


def test_check_copy_without_a_manifest(copy_dir):
    (copy_dir / "MANIFEST.json").unlink()
    assert check_copy.problems(copy_dir) == [f"MANIFEST.json is missing in {copy_dir}"]


# ── ledger ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_insert_sql_has_one_placeholder_per_column():
    sql = ledger.insert_sql()
    assert sql.startswith("INSERT INTO shared.llm_call_log (tenant_id, stage, role,")
    assert sql.count("$") == len(ledger.COLUMNS) == 35
    assert "$1::uuid" in sql and "$8::jsonb" in sql and "$27::uuid" in sql
    with pytest.raises(ValueError):
        ledger.insert_sql("llm_call_log; DROP TABLE x")


def test_ledger_row_args_follow_the_columns_and_clamp_values():
    row = ledger.LedgerRow(app="tripplanner", stage="tp_search_embed", role="embed", model="embed-x",
                           tokens_in=12, tokens_out=0, cost_usd=0.000001, quality_signal={"cache": "miss"},
                           tenant_id="None", account="acc2", provider="bedrock-native", cache_read_tokens=-1,
                           cache_write_tokens=True, pricing_mode="guess", status="ok", latency_ms=41, attempt_no=1)
    args = dict(zip(ledger.COLUMNS, row.args()))
    assert len(row.args()) == len(ledger.COLUMNS)
    assert args["tenant_id"] is None
    assert json.loads(args["quality_signal"]) == {"cache": "miss"}
    assert (args["cache_read_tokens"], args["cache_write_tokens"], args["pricing_mode"]) == (None, None, None)
    assert (args["app"], args["status"], args["latency_ms"], args["attempt_no"]) == ("tripplanner", "ok", 41, 1)
    assert args["is_shadow"] is False and args["is_experiment"] is False


def test_ledger_row_failed_attempt():
    row = ledger.LedgerRow(app="tripplanner", stage="tp_compose", role="writer", model="m", cost_usd=0.0,
                           stop_reason="error", status="refused", error_class="access_denied",
                           pricing_mode="catalog")
    args = dict(zip(ledger.COLUMNS, row.args()))
    assert (args["status"], args["error_class"], args["pricing_mode"]) == ("refused", "access_denied", "catalog")


def test_derive_status():
    assert ledger.derive_status() == "ok"
    assert ledger.derive_status(stop_reason="max_tokens") == "truncated"
    assert ledger.derive_status(quality_signal={"billed_unusable": True}) == "billed_unusable"
    assert ledger.derive_status("error", stop_reason="max_tokens") == "error"
    assert ledger.derive_status("nope") == "ok"


def test_ledger_roles_match_migration_319():
    text = (ROOT / "api" / "migrations" / "319_llm_ledger_v2.sql").read_text(encoding="utf-8")
    for role in ledger.ROLES:
        assert f"'{role}'" in text, role


# ── budget ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_budget_fails_closed_when_spend_is_unreadable():
    d = budget.decide(None, 5.0)
    assert (d.allowed, d.exhausted, d.reason, d.spent_usd) == (False, True, "spend_unreadable", None)
    assert d.as_dict() == {"spent_usd": None, "cap_usd": 5.0, "exhausted": True, "reason": "spend_unreadable",
                           "near_cap": False}
    assert budget.decide(1.0, 5.0, stage="s", stage_cap=1.0, stage_spent=None).reason == "spend_unreadable"


@pytest.mark.parametrize("spent,cap,allowed,near,reason", [
    (0.0, 5.0, True, False, None),
    (3.99, 5.0, True, False, None),
    (4.0, 5.0, True, True, None),
    (5.0, 5.0, False, True, "cap_reached"),
    (7.25, 5.0, False, True, "cap_reached"),
])
def test_budget_total_cap(spent, cap, allowed, near, reason):
    d = budget.decide(spent, cap)
    assert (d.allowed, d.near_cap, d.reason) == (allowed, near, reason)
    assert "stage" not in d.as_dict()


def test_budget_stage_cap_applies_with_the_total():
    d = budget.decide(1.0, 5.0, stage="tp_compose", stage_spent=0.9, stage_cap=1.0)
    assert (d.allowed, d.near_cap) == (True, True)
    assert d.as_dict() == {"spent_usd": 1.0, "cap_usd": 5.0, "stage": "tp_compose", "stage_spent_usd": 0.9,
                           "stage_cap_usd": 1.0, "exhausted": False, "reason": None, "near_cap": True}
    d = budget.decide(1.0, 5.0, stage="tp_compose", stage_spent=1.0, stage_cap=1.0)
    assert (d.allowed, d.reason) == (False, "cap_reached")


def test_budget_threshold():
    assert budget.threshold(1.0, 5.0) is None
    assert budget.threshold(4.0, 5.0) == "near"
    assert budget.threshold(5.0, 5.0) == "cap"
    assert budget.threshold(0.5, 1.0, near_ratio=0.5) == "near"


# ── pricing (core-only behaviour; parity with CIS is in test_aa642_s8_aa_llm_parity.py) ───────────────────────────

def test_quote_reports_an_estimate_and_never_raises_on_the_callback():
    seen = []
    q = pricing.quote("no-such-model", pricing.Usage(input_tokens=1000), on_estimate=seen.append)
    assert q.estimated and seen == ["no-such-model"]

    def boom(_):
        raise RuntimeError("logger down")

    assert pricing.quote("no-such-model", pricing.Usage(input_tokens=1), on_estimate=boom).estimated


def test_override_reads_a_mapping_too():
    rates = {"in": 1.0, "out": 1.0}
    assert pricing.apply_override(rates, {"price_in_override": 2.0}) is True
    assert rates["in"] == 0.002


# ── contract (CIS migration 323; the views themselves are tested with the migration) ──────────────────────────────────

def test_contract_selects_name_their_columns():
    assert "*" not in contract.GATEWAY_CONFIG_SQL and "*" not in contract.APP_SPEND_SQL
    assert contract.GATEWAY_CONFIG_SQL.count("$") == 1 and contract.APP_SPEND_SQL.count("$") == 1


@pytest.mark.parametrize("error,want", [
    ("An error occurred (AccessDeniedException) when calling the Converse operation", ("refused", "access_denied")),
    ("insufficient_quota", ("refused", "quota_exhausted")),
    ("ThrottlingException: Too many requests", ("refused", "throttled")),
    ("read timeout on endpoint", ("error", "other")),
    (None, ("error", "other")),
])
def test_failed_outcome(error, want):
    assert ledger.failed_outcome(error) == want

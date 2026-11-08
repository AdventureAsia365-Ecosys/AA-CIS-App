"""AA-1052 W6 — permanent change control of the writing path (services/quality/change_control.py).

The two CI gates (this file runs in the required "Unit Tests" job):

* the lock (services/quality/change_control.lock.json) matches the tree: a writing-path component that moved
  without a new lock entry and its evidence (eval run, parity test or break-glass reason) fails here;
* a CIS migration above the baseline that writes a gated stage's route config carries '-- eval:' or
  '-- break-glass:' in its leading comments.

The seeded-regression cases below copy the component files into a temp tree and change one thing each (a meta band
widened in the policy, a prompt version bumped, a route-table row, a pass line, the contract mode): every one fails
the check until it is accepted with evidence, and the revert passes again.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from services.quality import change_control as cc

FILE_COMPONENTS = ("writing_policy", "gates", "prompt_registry", "route_table", "final_contract")
RUN = "0b5e2a6c-6f1d-4c39-9d0e-1c2a3b4c5d6e"


# ── the real tree ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_lock_matches_the_tree():
    problems = cc.check()
    assert problems == [], "\n".join(problems)


def test_no_new_route_migration_lacks_its_evidence_header():
    problems = cc.migration_problems()
    assert problems == [], "\n".join(problems)


def test_the_lock_covers_every_component_and_names_its_issue():
    lock = cc.load_lock()
    assert lock["version"] == cc.LOCK_VERSION and set(lock["components"]) == set(cc.COMPONENTS)
    for name, e in lock["components"].items():
        assert cc.evidence_problem(e["evidence"]) is None, name
        assert e["refs"].startswith("AA-"), name


def test_the_gated_stages_are_the_writer_paths_of_the_release_fingerprint():
    from services.writing_memory import config_fp
    prefixes = {p for ps in config_fp.STAGE_PREFIXES.values() for p in ps} | set(config_fp.EVAL_STAGES)
    assert prefixes == set(cc.GATED_STAGE_PREFIXES)


def test_the_policy_component_is_the_compiled_policy_sha():
    from services.quality import writing_policy
    assert cc.current(names=["writing_policy"])["writing_policy"] == writing_policy.policy_sha()


def test_the_383_break_glass_file_is_detected_and_carries_its_header():
    sql = (cc.REPO_ROOT / cc.MIGRATIONS_DIR / "383_llm_w0_remove_uncalibrated_rungs.sql").read_text()
    assert cc.migration_needs_header(sql) and cc.has_evidence_header(sql)


def test_older_route_migrations_are_grandfathered():
    sql = (cc.REPO_ROOT / cc.MIGRATIONS_DIR / "366_llm_family_fallback_chains.sql").read_text()
    assert cc.migration_needs_header(sql) and not cc.has_evidence_header(sql)   # detected, but 366 <= baseline


# ── seeded regressions on a copied tree ───────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for rel in (cc.POLICY_FILE, *cc.GATE_FILES, cc.PROMPT_MANIFEST, cc.ROUTE_TABLE_FILE, cc.FINAL_CONTRACT_FILE):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(cc.REPO_ROOT / rel, tmp_path / rel)
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/test_replay_parity.py").write_text("# a parity test\n")
    (tmp_path / cc.MIGRATIONS_DIR).mkdir(parents=True)
    return tmp_path


def _lock(root: Path) -> dict:
    return {"version": cc.LOCK_VERSION,
            "components": {n: {"sha": s, "evidence": "break-glass: seeded test lock", "refs": "AA-1052"}
                           for n, s in cc.current(root, FILE_COMPONENTS).items()}}


def _sub(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, (rel, old)
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def _changed(root: Path, lock: dict) -> list[str]:
    return [p.split(" ", 1)[0] for p in cc.check(root, lock, FILE_COMPONENTS) if " changed " in p]


def test_the_copied_tree_passes_its_own_lock(tree):
    assert cc.check(tree, _lock(tree), FILE_COMPONENTS) == []


@pytest.mark.parametrize("component,rel,old,new", [
    ("writing_policy", cc.POLICY_FILE, "meta: {min: 140, max: 155}", "meta: {min: 140, max: 165}"),
    ("route_table", cc.ROUTE_TABLE_FILE, "{gate: F2_lexicon, action: rewrite}",
     "{gate: F2_lexicon, action: repair_sentence, parse: quoted}"),
    ("gates", "services/quality/thresholds.yaml", "pass_line:", "pass_line_widened:"),
    ("final_contract", cc.FINAL_CONTRACT_FILE, 'MODE = MODE_REPORT', 'MODE = "enforce"'),
])
def test_a_seeded_regression_fails_until_accepted_and_its_revert_passes(tree, component, rel, old, new):
    lock = _lock(tree)
    _sub(tree, rel, old, new)
    assert _changed(tree, lock) == [component]
    msg = next(p for p in cc.check(tree, lock, FILE_COMPONENTS) if p.startswith(component))
    assert "--accept " + component in msg and "eval:" in msg
    _sub(tree, rel, new, old)                                    # the revert passes again
    assert cc.check(tree, lock, FILE_COMPONENTS) == []


def test_a_policy_comment_is_not_a_change(tree):
    """policy_sha hashes the loaded YAML: a comment (like the synthetic examples, FIXTURE_KEYS) is not a change."""
    lock = _lock(tree)
    p = tree / cc.POLICY_FILE
    p.write_text(p.read_text(encoding="utf-8") + "\n# a comment changes nothing\n", encoding="utf-8")
    assert cc.check(tree, lock, FILE_COMPONENTS) == []


def test_a_prompt_version_bump_needs_evidence(tree):
    lock = _lock(tree)
    path = tree / cc.PROMPT_MANIFEST
    manifest = json.loads(path.read_text())
    manifest["prompts"][0]["version"] = manifest["prompts"][0]["version"] + "-bumped"
    path.write_text(json.dumps(manifest))
    assert _changed(tree, lock) == ["prompt_registry"]


def test_accept_records_the_sha_with_its_evidence(tree):
    lock_path = tree / "lock.json"
    lock_path.write_text(json.dumps(_lock(tree)))
    _sub(tree, cc.POLICY_FILE, "meta: {min: 140, max: 155}", "meta: {min: 140, max: 165}")
    with pytest.raises(ValueError, match="uuid"):
        cc.accept("writing_policy", "eval:not-a-run", "AA-1", root=tree, path=lock_path)
    with pytest.raises(ValueError, match="unknown component"):
        cc.accept("prompts", f"eval:{RUN}", "AA-1", root=tree, path=lock_path)
    cc.accept("writing_policy", f"eval:{RUN}", "AA-1", root=tree, path=lock_path)
    lock = json.loads(lock_path.read_text())
    assert lock["components"]["writing_policy"]["evidence"] == f"eval:{RUN}"
    assert cc.check(tree, lock, FILE_COMPONENTS) == []


def test_a_stale_or_broken_lock_is_reported(tree):
    lock = _lock(tree)
    lock["components"]["gates"]["evidence"] = "trust me"
    lock["components"]["mystery"] = {"sha": "x", "evidence": "break-glass: whatever it is", "refs": "AA-1"}
    del lock["components"]["route_table"]
    lock["version"] = "cc-v0"
    problems = cc.check(tree, lock, FILE_COMPONENTS)
    assert any(p.startswith("gates: evidence must be") for p in problems)
    assert any("unknown component 'mystery'" in p for p in problems)
    assert any(p.startswith("route_table: no lock entry") for p in problems)
    assert any(p.startswith("lock version") for p in problems)


@pytest.mark.parametrize("evidence,ok", [
    (f"eval:{RUN}", True), (f"eval: {RUN}", True), ("eval:123", False),
    ("parity:tests/unit/test_replay_parity.py", True), ("parity:tests/unit/missing.py", False),
    ("parity:services/quality/lineage.py", False),
    ("break-glass: incident 12, judge route down", True), ("break-glass: short", False),
    ("", False), (None, False), ("because", False),
])
def test_evidence_kinds(tree, evidence, ok):
    assert (cc.evidence_problem(evidence, tree) is None) is ok


def test_route_defaults_moves_with_a_gated_stage_only(monkeypatch):
    from shared.llm_client import role_config
    before = cc.current(names=["route_defaults"])["route_defaults"]
    gated = next(s for s in role_config.SAFE_DEFAULTS if cc.is_gated_stage(s))
    other = next((s for s in role_config.SAFE_DEFAULTS if not cc.is_gated_stage(s)), None)
    patched = dict(role_config.SAFE_DEFAULTS)
    if other is not None:
        patched[other] = replace(patched[other], model_id="another-model")
        monkeypatch.setattr(role_config, "SAFE_DEFAULTS", patched)
        assert cc.current(names=["route_defaults"])["route_defaults"] == before
    patched[gated] = replace(patched[gated], fallback_model_ids=("another-model",))
    monkeypatch.setattr(role_config, "SAFE_DEFAULTS", patched)
    assert cc.current(names=["route_defaults"])["route_defaults"] != before


# ── the route-migration lint ──────────────────────────────────────────────────────────────────────────────────────

ROUTE_SQL = """BEGIN;
UPDATE shared.llm_role_config SET fallback_model_ids = ARRAY['model-b'] WHERE stage = 't9_write';
INSERT INTO shared.schema_versions (version, description) VALUES ('{n}', 'x') ON CONFLICT DO NOTHING;
COMMIT;
"""


def _mig(tree: Path, name: str, text: str) -> None:
    (tree / cc.MIGRATIONS_DIR / name).write_text(text, encoding="utf-8")


def test_a_synthetic_route_migration_without_a_header_fails(tree):
    _mig(tree, "391_route_change.sql", "-- Why: a new fallback.\n" + ROUTE_SQL.format(n=391))
    problems = cc.migration_problems(tree)
    assert len(problems) == 1 and "391_route_change.sql" in problems[0] and "llm_role_config" in problems[0]


@pytest.mark.parametrize("header", [f"-- eval: {RUN}", "-- break-glass: judge route down since 09:00, incident 7"])
def test_an_eval_or_break_glass_header_passes(tree, header):
    _mig(tree, "391_route_change.sql", f"{header}\n-- Why: a new fallback.\n" + ROUTE_SQL.format(n=391))
    assert cc.migration_problems(tree) == []


def test_a_header_after_the_first_statement_does_not_count(tree):
    _mig(tree, "391_route_change.sql", ROUTE_SQL.format(n=391) + f"-- eval: {RUN}\n")
    assert cc.migration_problems(tree)


@pytest.mark.parametrize("sql,needs", [
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage = 'trip_narration';", False),
    ("UPDATE shared.stage_model_params SET effort = 'low' WHERE stage = 's1_generate';", True),
    ("insert into shared.stage_model_params (stage, model_key) values ('t2_generate', 'm');", True),
    ("UPDATE shared.llm_model_deployment SET priority = 2 WHERE model_key = 'm';", True),
    ("UPDATE shared.llm_route_settings SET order_mode = 'aws_first';", True),
    ("INSERT INTO shared.prompt_version (stage, version) VALUES ('t9_write', 'v3');", False),
    ("SELECT stage FROM shared.llm_role_config WHERE stage = 't9_write';", False),
    ("-- UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage = 't9_write';", False),
    ("UPDATE shared.writing_memory_release SET status = 'active';", True),
    # fails closed for the stage-scoped tables: exempt only when every selected stage is a non-gated literal
    ("UPDATE shared.llm_role_config SET model='x' WHERE stage LIKE 't9_%';", True),
    ("UPDATE shared.llm_role_config SET model='x' WHERE role='writer';", True),
    ("UPDATE shared.llm_role_config r SET model_id = 'm' FROM shared.llm_model_catalog c "
     "WHERE r.role = 'judge' AND c.model_key = 'm';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage LIKE 't%';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage LIKE 's1\\_%';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage LIKE 'w%';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage LIKE 'trip\\_%';", False),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage ~ '^trip';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage <> 'trip_narration';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage = 'trip_narration' OR role = 'writer';", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage IN "
     "(SELECT stage FROM shared.llm_role_config WHERE role = 'writer');", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage IN ('trip_narration', 't2_generate');", True),
    ("UPDATE shared.llm_role_config SET model_id = 'm' WHERE stage IN ('trip_narration', 'geo_label');", False),
    ("UPDATE shared.llm_role_config r SET model_id = 'm' WHERE r.stage = 'trip_narration' AND r.role = 'writer';",
     False),
    ("INSERT INTO shared.llm_role_config (stage, role, model_id) VALUES ('trip_narration', 'writer', 'm') "
     "ON CONFLICT (stage, role) DO UPDATE SET model_id = EXCLUDED.model_id;", False),
    ("INSERT INTO shared.llm_role_config (stage, role, model_id) "
     "SELECT stage, 'judge', 'm' FROM shared.llm_role_config WHERE role = 'writer';", True),
    ("DELETE FROM shared.llm_role_config WHERE stage = 'trip_narration';", False),
    ("TRUNCATE shared.stage_model_params;", True),
    # one gated statement among exempt ones is enough
    ("DELETE FROM shared.llm_role_config WHERE stage = 'trip_narration';\n"
     "UPDATE shared.llm_role_config SET model_id = 'm' WHERE role = 'validate';", True),
])
def test_what_needs_a_header(sql, needs):
    assert bool(cc.migration_needs_header(sql)) is needs


@pytest.mark.parametrize("number", ["331", "332", "333"])
def test_the_past_role_wide_route_migrations_would_need_a_header(number):
    """CIS 331-333 moved the judge / validate routes of every stage by role (no stage literal): grandfathered by the
    baseline, but the same file above it needs the header."""
    [path] = sorted((cc.REPO_ROOT / cc.MIGRATIONS_DIR).glob(f"{number}_*.sql"))
    assert "llm_role_config" in cc.migration_needs_header(path.read_text(encoding="utf-8"))


def test_baseline_and_unnumbered_files_are_skipped(tree):
    _mig(tree, f"{cc.MIGRATION_BASELINE}_old.sql", ROUTE_SQL.format(n=cc.MIGRATION_BASELINE))
    _mig(tree, "README.sql", ROUTE_SQL.format(n=0))
    assert cc.migration_problems(tree) == []


# ── the CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_cli_check_passes_on_the_tree(capsys):
    assert cc.main([]) == 0
    assert "0 problem(s)" in capsys.readouterr().out


def test_cli_show_and_refusals(capsys):
    assert cc.main(["--show"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(cc.COMPONENTS)
    assert cc.main(["--accept", "writing_policy", "--evidence", "because"]) == 2
    assert "refused" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cc.main(["--accept", "writing_policy"])

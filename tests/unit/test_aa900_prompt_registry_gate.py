"""AA-900 — the prompt registry gate: a prompt change without a version bump fails CI.

The first test IS the gate (scripts/prompt_registry_check.py on the repository's manifest). The others prove the gate
catches what it must on a throwaway copy (invented prompts in a temporary tree), and that the runtime lookup writes a
version only when the deployed source and the database register agree.
"""
import json
import textwrap
from pathlib import Path

import pytest

from scripts import prompt_registry_check as gate
from shared.llm_client import prompt_registry
from shared.llm_client.role_config import SAFE_DEFAULTS

REPO = Path(__file__).resolve().parents[2]


def test_the_repository_manifest_matches_every_prompt_source():
    found = gate.problems(prompt_registry.load_manifest())
    assert found == [], "\n".join(found)


def test_every_manifest_stage_is_a_gateway_stage_or_a_registered_prompt():
    manifest = prompt_registry.load_manifest()
    register = (REPO / "docs" / "llm" / "prompt-register.md").read_text(encoding="utf-8")
    for stage in manifest:
        assert stage in SAFE_DEFAULTS or f"`{stage}`" in register, stage


# ── on a throwaway tree ────────────────────────────────────────────────────────────────────────────────────────────

PROMPTS_V1 = textwrap.dedent('''
    """Invented prompts for the gate test."""
    import os

    SYSTEM_PROMPT = "You describe the floating market of Lake Varra in two sentences."


    def build_user_prompt(day: int) -> str:
        return f"Day {day}: the boat leaves at dawn."


    UNRELATED = os.environ.get("X", "y")
''')


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "svc").mkdir()
    src = tmp_path / "svc" / "prompts.py"
    src.write_text(PROMPTS_V1, encoding="utf-8")
    sha = prompt_registry.prompt_hash_of("svc/prompts.py", ["SYSTEM_PROMPT", "build_user_prompt"], tmp_path)
    manifest = {"prompts": [{"stage": "t9_varra", "version": "v1", "source_path": "svc/prompts.py",
                             "symbols": ["SYSTEM_PROMPT", "build_user_prompt"], "prompt_sha256": sha}]}
    path = tmp_path / "prompt_versions.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path, src, path


def _problems(root, path):
    return gate.problems(prompt_registry.load_manifest(path), root)


def test_editing_other_code_in_the_file_is_not_a_prompt_change(tree):
    root, src, path = tree
    src.write_text(PROMPTS_V1.replace('"X", "y"', '"Z", "w"') + "\nEXTRA = 1\n", encoding="utf-8")
    assert _problems(root, path) == []


def test_a_prompt_change_without_a_bump_fails_and_names_the_fix(tree):
    root, src, path = tree
    src.write_text(PROMPTS_V1.replace("two sentences", "three sentences"), encoding="utf-8")
    found = _problems(root, path)
    assert len(found) == 1 and "t9_varra: the prompt in svc/prompts.py" in found[0] and "--bump" in found[0]


def test_a_changed_prompt_function_fails_too(tree):
    root, src, path = tree
    src.write_text(PROMPTS_V1.replace("leaves at dawn", "leaves at noon"), encoding="utf-8")
    assert len(_problems(root, path)) == 1


def test_bump_appends_a_new_version_and_the_gate_passes(tree):
    root, src, path = tree
    src.write_text(PROMPTS_V1.replace("two sentences", "three sentences"), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert gate.bump(raw, root) == ["t9_varra"]
    assert [e["version"] for e in raw["prompts"]] == ["v1", "v2"]
    assert raw["prompts"][0]["prompt_sha256"] != raw["prompts"][1]["prompt_sha256"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert _problems(root, path) == []
    assert gate.bump(json.loads(path.read_text(encoding="utf-8")), root) == []


def test_a_missing_symbol_or_file_fails(tree):
    root, src, path = tree
    src.write_text(PROMPTS_V1.replace("SYSTEM_PROMPT =", "SYSTEM_TEXT ="), encoding="utf-8")
    assert "no module-level symbol SYSTEM_PROMPT" in _problems(root, path)[0]
    src.unlink()
    assert "no such file" in _problems(root, path)[0]


def test_structure_rules(tree):
    root, src, path = tree
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["prompts"].append({**raw["prompts"][0]})                          # same version, same hash again
    raw["prompts"].append({**raw["prompts"][0], "version": "bad version!", "source_path": "../etc/x.py"})
    path.write_text(json.dumps(raw), encoding="utf-8")
    found = "\n".join(_problems(root, path))
    assert "version listed twice" in found and "same prompt_sha256" in found
    assert "version does not match" in found and "is not a repository path" in found


def test_next_version():
    assert gate.next_version("v1") == "v2" and gate.next_version("v9") == "v10" and gate.next_version("rc") == "rc.2"


def test_sql_registers_every_row_without_prompt_text(tree):
    root, src, path = tree
    out = gate.sql(prompt_registry.load_manifest(path))
    assert out.startswith("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by)")
    assert "ON CONFLICT (stage, version) DO NOTHING" in out and "Lake Varra" not in out


def test_the_cli_exit_code(tree, capsys):
    root, src, path = tree
    assert gate.main(["--manifest", str(path)]) == 1      # its source is not in the repository: a problem
    assert gate.main([]) == 0                                     # the repository's own manifest is clean


# ── runtime lookup ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_runtime_version_needs_the_source_and_the_register_to_agree(tree, monkeypatch):
    root, src, path = tree
    sha = json.loads(path.read_text(encoding="utf-8"))["prompts"][0]["prompt_sha256"]
    monkeypatch.setattr(prompt_registry, "MANIFEST_PATH", path)
    monkeypatch.setattr(prompt_registry, "REPO_ROOT", root)
    real = prompt_registry.prompt_hash_of
    monkeypatch.setattr(prompt_registry, "prompt_hash_of", lambda p, s, r=root: real(p, s, root))
    monkeypatch.setattr(prompt_registry, "load_manifest", lambda p=path: gate.load_manifest(path))
    prompt_registry.invalidate()
    monkeypatch.setattr(prompt_registry, "loader", lambda: set())
    assert prompt_registry.version_for("t9_varra") is None                # not registered on this database
    prompt_registry.invalidate()
    monkeypatch.setattr(prompt_registry, "loader", lambda: {("t9_varra", "v1", sha)})
    assert prompt_registry.version_for("t9_varra") == "v1"
    assert prompt_registry.version_for("t9_other") is None
    prompt_registry.invalidate()
    src.write_text(PROMPTS_V1.replace("two sentences", "four sentences"), encoding="utf-8")
    assert prompt_registry.version_for("t9_varra") is None                # deployed source drifted


def test_an_unreadable_register_gives_no_version_and_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("relation shared.prompt_version does not exist")
    prompt_registry.invalidate()
    monkeypatch.setattr(prompt_registry, "loader", boom)
    stage = next(iter(prompt_registry.load_manifest()))
    assert prompt_registry.version_for(stage) is None
    assert prompt_registry.version_for(None) is None

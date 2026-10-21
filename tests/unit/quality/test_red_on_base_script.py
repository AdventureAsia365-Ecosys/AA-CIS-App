"""AA-925 (Q-2) — .github/scripts/q0_red_on_base.py against a throwaway git repo (no network, no app code).

The toy repo has a ``services.flag.VALUE`` and a corpus test that asserts each case's ``want`` equals it, so
a case is red on a base whose VALUE differs, green where it matches, and a corpus test that imports a module
the base lacks is a collection error (inconclusive, never red)."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / ".github" / "scripts" / "q0_red_on_base.py"

_CORPUS_TEST = '''
import json
from pathlib import Path
import pytest
import services.flag as flag
{extra_import}
CASES = [c for p in sorted((Path(__file__).parent / "cases").glob("*.json")) if not p.name.startswith("_")
         for c in json.loads(p.read_text())]

@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case):
    assert flag.VALUE == case["input"]["want"]
'''


def _load():
    spec = importlib.util.spec_from_file_location("q0_red_on_base", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "q0", "GIT_AUTHOR_EMAIL": "q0@example.com",
           "GIT_COMMITTER_NAME": "q0", "GIT_COMMITTER_EMAIL": "q0@example.com"}
    out = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _case(cid, want, expected="fire", **extra):
    return {"id": cid, "class_id": "other.toy", "severity": "block", "chain": "a1", "detector": "X",
            "expected": expected, "input": {"want": want}, **extra}


def _corpus(repo: Path, cases: list[dict], extra_import: str = "") -> None:
    _write(repo, "tests/unit/quality/cases/toy.json", json.dumps(cases))
    _write(repo, "tests/unit/quality/test_q0_corpus.py", _CORPUS_TEST.format(extra_import=extra_import))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    for rel in ("services/__init__.py", "tests/__init__.py", "tests/unit/__init__.py",
                "tests/unit/quality/__init__.py", "tests/unit/conftest.py"):
        _write(r, rel, "")
    _write(r, "services/flag.py", "VALUE = 1\n")
    return r


def _commit(repo, msg="c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _run(repo, base, monkeypatch, labels=()):
    monkeypatch.setenv("PR_LABELS", json.dumps(list(labels)))
    monkeypatch.setenv("RUNNER_TEMP", str(repo.parent))
    return _load().main(["--base", base, "--root", str(repo)])


def test_bootstrap_when_base_has_no_corpus(repo, monkeypatch):
    base = _commit(repo)
    _corpus(repo, [_case("toy-a", 1)])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 0


def test_new_case_red_on_base_passes(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1)])
    base = _commit(repo)
    _write(repo, "services/flag.py", "VALUE = 2\n")
    _corpus(repo, [_case("toy-a", 1, expected="fire"), _case("toy-b", 2)])
    # toy-a is unchanged; the head change of VALUE would turn it red at HEAD, irrelevant here
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 0


def test_new_case_green_on_base_fails(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1)])
    base = _commit(repo)
    _corpus(repo, [_case("toy-a", 1), _case("toy-b", 1)])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 1


def test_substring_ids_are_selected_by_node_id(repo, monkeypatch):
    """pytest -k would select 'toy-b-twin' with 'toy-b'; node ids keep them apart."""
    _corpus(repo, [_case("toy-a", 1), _case("toy-b-twin", 1)])
    base = _commit(repo)
    _write(repo, "services/flag.py", "VALUE = 2\n")
    _corpus(repo, [_case("toy-a", 1), _case("toy-b-twin", 1), _case("toy-b", 2)])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 0


def test_collection_error_on_base_is_inconclusive_not_red(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1)])
    base = _commit(repo)
    _write(repo, "services/newmod.py", "X = 1\n")
    _corpus(repo, [_case("toy-a", 1), _case("toy-b", 2)], extra_import="import services.newmod")
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 1


def test_relabel_needs_the_label(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1)])
    base = _commit(repo)
    _corpus(repo, [_case("toy-a", 1, expected="no_fire")])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 1
    assert _run(repo, base, monkeypatch, labels=["q0-relabel"]) == 0


def test_removed_case_needs_the_label(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1), _case("toy-b", 1)])
    base = _commit(repo)
    _corpus(repo, [_case("toy-a", 1)])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 1


def test_negative_twin_exemption_only_for_a_new_positive_case(repo, monkeypatch):
    _corpus(repo, [_case("toy-a", 1)])
    base = _commit(repo)
    _write(repo, "services/flag.py", "VALUE = 2\n")
    twin = _case("toy-b-ok", 1, expected="no_fire", red_on_base=False, twin_of="toy-b")
    _corpus(repo, [_case("toy-a", 1), _case("toy-b", 2), twin])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 0
    # a "twin" of an OLD case (or a positive case claiming the exemption) is not exempt
    _corpus(repo, [_case("toy-a", 1), _case("toy-c", 1, expected="no_fire", red_on_base=False, twin_of="toy-a")])
    _commit(repo)
    assert _run(repo, base, monkeypatch) == 1


def test_junit_reader_tells_failure_from_error(tmp_path):
    mod = _load()
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite><testcase name="test_case[a]"><failure/></testcase>'
                   '<testcase name="test_case[b]"><error/></testcase><testcase name="test_case[c]"/>'
                   '<testcase name="test_case[d]"><skipped/></testcase></testsuite></testsuites>')
    got = mod.junit_outcomes(str(xml), ["a", "b", "c", "d", "e"])
    assert got == {"a": "failure", "b": "error", "c": "passed", "d": "skipped", "e": "missing"}


def test_script_is_stdlib_only():
    import ast

    tree = ast.parse(SCRIPT.read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods

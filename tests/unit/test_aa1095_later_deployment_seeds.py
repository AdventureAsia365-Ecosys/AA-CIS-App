"""AA-1095 (S278) guard: every migration after the newest route replay test that seeds shared.llm_model_deployment rows
is in tests/integration/_later_deployments.LATER_DEPLOYMENT_SEEDS, and every route replay test sets those rows aside.

Why: the replay tests of 366 / 376 / 383 re-run the file's chain rules (attempt_cap counts every deployment row of a
routed model, on or off) after the full replay. Migration 406 seeded acc1 deployments of the same models and 26 replay
tests went red (S278). The seeds between 366 and 383 (367, 376) were there when the replay tests were written and
those tests already pass with them (376's test resets its own rows). The helper is read as text (AST), so this unit
test needs no psycopg2 and no database.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"
HELPER = REPO / "tests" / "integration" / "_later_deployments.py"
SEED_RE = re.compile(r"\bINSERT\s+INTO\s+shared\.llm_model_deployment\b", re.I)
FILE_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def _helper_dicts() -> tuple[dict, dict]:
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    found: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = ast.literal_eval(node.value)
    return found["LATER_DEPLOYMENT_SEEDS"], found["ROUTE_REPLAY_TESTS"]


def seed_problems(seeds: dict, replays: dict, migrations: dict[int, str]) -> list[str]:
    """``migrations``: number -> SQL text. A seeding file after the newest replay that is not listed (or whose listed
    actor does not appear in it) is a problem."""
    newest = max(replays)
    out = []
    for number, sql in sorted(migrations.items()):
        if number <= newest or not SEED_RE.search(sql):
            continue
        actor = seeds.get(number)
        if actor is None:
            out.append(f"{number}: seeds llm_model_deployment rows and is not in LATER_DEPLOYMENT_SEEDS")
        elif f"'{actor}'" not in sql:
            out.append(f"{number}: its listed actor {actor!r} is not the updated_by the file writes")
    return out


def _migrations() -> dict[int, str]:
    out = {}
    for path in MIGRATIONS.glob("*.sql"):
        m = FILE_RE.match(path.name)
        if m:
            out[int(m.group(1))] = path.read_text(encoding="utf-8")
    return out


def test_every_later_deployment_seed_is_listed_with_its_actor():
    seeds, replays = _helper_dicts()
    assert seed_problems(seeds, replays, _migrations()) == []


def test_every_route_replay_test_sets_the_later_seeds_aside_for_its_own_number():
    _, replays = _helper_dicts()
    for number, module in replays.items():
        text = (REPO / module).read_text(encoding="utf-8")
        assert f"set_later_deployments_aside(c, {number})" in text, module


def test_a_missing_or_wrong_seed_entry_is_reported():
    replays = {366: "x", 383: "y"}
    sql = "INSERT INTO shared.llm_model_deployment (model_key) SELECT 'm' -- actor 'migration-500-x'"
    assert seed_problems({}, replays, {500: sql}) == [
        "500: seeds llm_model_deployment rows and is not in LATER_DEPLOYMENT_SEEDS"]
    assert seed_problems({500: "migration-500-y"}, replays, {500: sql}) == [
        "500: its listed actor 'migration-500-y' is not the updated_by the file writes"]
    assert seed_problems({500: "migration-500-x"}, replays, {500: sql}) == []
    assert seed_problems({}, replays, {376: sql}) == []        # not after the newest replay: not counted


def test_the_helper_lists_only_actors_after_the_replayed_number():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_later_deployments", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.later_seed_actors(366) == ["migration-406-aa1095"]
    assert mod.later_seed_actors(406) == []

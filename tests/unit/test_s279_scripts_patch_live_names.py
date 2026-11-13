"""S279 lane L2 review (AA-1096) — a script that switches a production module by assigning or patching one of its
names must name something the module still defines.

When a module constant becomes a call-time reader (S279 L2: ``brand_fit._REPEAT_BAND`` -> ``brand_fit._repeat_band()``),
``module.CONST = x`` in a measurement script still runs without an error and silently changes nothing (the paid
--single-call measurement repeated its judge calls). This guard parses every script under ``scripts/`` and, for each
``alias.NAME = ...`` / ``patch.object(alias, "NAME", ...)`` / ``setattr(alias, "NAME", ...)`` on a module imported from
``services`` / ``shared`` / ``api``, checks that the module has ``NAME``. No script is run."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("services", "shared", "api")


def _aliases(tree: ast.AST) -> dict[str, str]:
    """alias -> dotted module, for modules imported from the app packages anywhere in the script."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in PACKAGES and a.asname:
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in PACKAGES:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _patched(tree: ast.AST, aliases: dict[str, str]) -> list[tuple[str, str, int]]:
    """(module, name, line) of every name a script assigns or patches on an app module alias."""
    hits = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in aliases:
                    hits.append((aliases[t.value.id], t.attr, node.lineno))
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            obj, attr = node.args[0], node.args[1]
            if (name in ("object", "setattr") and isinstance(obj, ast.Name) and obj.id in aliases
                    and isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                hits.append((aliases[obj.id], attr.value, node.lineno))
    return hits


def _cases() -> list[tuple[str, str, str, int]]:
    out = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        out += [(str(path.relative_to(ROOT)), mod, name, line) for mod, name, line in _patched(tree, aliases)]
    return out


CASES = _cases()


def test_the_guard_sees_the_known_switches():
    assert any(c[0] == "scripts/aa645/brandfit_measure.py" and c[2] == "_repeat_band" for c in CASES)


@pytest.mark.parametrize("script,module,name,line", CASES, ids=[f"{c[0]}:{c[3]}" for c in CASES])
def test_a_script_patches_only_names_the_module_still_defines(script, module, name, line):
    try:
        mod = importlib.import_module(module)
    except ImportError:                      # ``from pkg import function``: the alias is not a module
        parent, _, attr = module.rpartition(".")
        mod = getattr(importlib.import_module(parent), attr)
    assert hasattr(mod, name), f"{script}:{line} sets {module}.{name}, which the module no longer defines"


@pytest.mark.parametrize("env,gate,background", [
    ({}, "brand_fit_mission", False),                                   # unset: the pre-move code defaults
    ({"T2_JUDGE_GATE": "brand_fit", "T3_BACKGROUND_FACTS": "on"}, "brand_fit", True),
    ({"T2_JUDGE_GATE": " Brand_Fit_Mission ", "T3_BACKGROUND_FACTS": "0"}, "brand_fit_mission", False),
])
def test_the_t2_replay_arms_drive_the_settings_that_replaced_their_env_switches(env, gate, background):
    from scripts.quality import replay_aa1049_t2 as rp
    from shared import settings
    with rp._serve_arm_settings(env):
        assert settings.get("cis.quality_modes.t2_judge_gate") == gate
        assert settings.get("cis.quality_modes.t3_background_facts") is background

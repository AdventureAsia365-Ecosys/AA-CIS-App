"""S276 lesson guard (AA-1024): ``pytest.mark.parametrize`` arguments and ids must be the same in every process.

pytest-xdist imports every test module again in each worker and compares the collected test ids; a parametrize value
or id built at import time from ``uuid4()``, ``time.time()``, ``datetime.now()`` or ``random.*`` differs per worker, so
the run stops with "Different tests were collected between gw0 and gw1" and the CI shard goes red (S276, #1426).

This lint parses every ``tests/**/*.py`` (AST only, nothing is imported) and fails when such a value reaches a
parametrize decorator (``pytest.mark.parametrize(...)`` incl. ``ids=``), a ``pytest.param(...)`` or a
``pytest.fixture(params=..., ids=...)``: either the call itself, or a module / class level name assigned from one (also
through other names: ``TAG = uuid4().hex`` then ``NAME = f"x-{TAG}"``). No allowlist: build such a value inside the
test (or a fixture) instead, or use a fixed literal.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"

# Calls whose result differs between two imports of the same module (dotted names after import aliases resolve).
NONDETERMINISTIC = frozenset({
    "uuid.uuid1", "uuid.uuid4",
    "time.time", "time.time_ns", "time.monotonic", "time.monotonic_ns", "time.perf_counter", "time.perf_counter_ns",
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today",
    "os.urandom", "os.getpid",
})
NONDETERMINISTIC_MODULES = ("random.", "secrets.")


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted origin for the module's imports (`import uuid as u`, `from datetime import datetime`)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[(a.asname or a.name).split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def _bad_call(node: ast.Call, aliases: dict[str, str]) -> str | None:
    name = _dotted(node.func, aliases)
    if not name:
        return None
    if name == "random.Random" and (node.args or node.keywords):
        return None  # a seeded generator is deterministic
    if name in NONDETERMINISTIC or name.startswith(NONDETERMINISTIC_MODULES):
        return f"{name}()"
    return None


def _import_time_nodes(expr: ast.AST):
    """ast.walk without the bodies of lambdas: a lambda runs when the test calls it, not at import."""
    todo = [expr]
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, ast.Lambda):
            todo.extend(node.args.defaults + [d for d in node.args.kw_defaults if d is not None])
            continue
        todo.extend(ast.iter_child_nodes(node))


def _reason(expr: ast.AST, aliases: dict[str, str], tainted: dict[str, str]) -> str | None:
    """Why `expr` differs per import: a nondeterministic call in it, or a tainted name it reads."""
    for node in _import_time_nodes(expr):
        if isinstance(node, ast.Call):
            why = _bad_call(node, aliases)
            if why:
                return why
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in tainted:
            return f"{node.id} (= {tainted[node.id]})"
    return None


def _targets(node: ast.stmt) -> list[str]:
    names: list[str] = []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.append(n.id)
    return names


def _is_pytest(node: ast.AST, aliases: dict[str, str], *paths: str) -> bool:
    return _dotted(node, aliases) in paths


def _decorator_args(dec: ast.AST, aliases: dict[str, str]) -> list[ast.AST]:
    """The import-time argument expressions of a parametrize / fixture(params=) decorator, else []."""
    if not isinstance(dec, ast.Call):
        return []
    if _is_pytest(dec.func, aliases, "pytest.mark.parametrize"):
        return list(dec.args[1:]) + [k.value for k in dec.keywords if k.arg != "argnames"]
    if _is_pytest(dec.func, aliases, "pytest.fixture"):
        return [k.value for k in dec.keywords if k.arg in ("params", "ids")]
    return []


def scan_source(source: str, filename: str = "<test>") -> list[str]:
    """Offenders in one test module: "<file>:<line> <what>"."""
    tree = ast.parse(source, filename=filename)
    aliases = _aliases(tree)
    found: list[str] = []

    def scope(body: list[ast.stmt], tainted: dict[str, str]) -> None:
        tainted = dict(tainted)
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and stmt.value is not None:
                why = _reason(stmt.value, aliases, tainted)
                for name in _targets(stmt):
                    if why:
                        tainted[name] = why
                    elif not isinstance(stmt, ast.AugAssign):
                        tainted.pop(name, None)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for dec in stmt.decorator_list:
                    for arg in _decorator_args(dec, aliases):
                        why = _reason(arg, aliases, tainted)
                        if why:
                            found.append(f"{filename}:{dec.lineno} @{_dotted(dec.func, aliases)} on {stmt.name} "
                                         f"uses {why}")
            if isinstance(stmt, ast.ClassDef):
                scope(stmt.body, tainted)
            elif isinstance(stmt, (ast.If, ast.Try, ast.With)):
                for block in ("body", "orelse", "finalbody"):
                    scope(getattr(stmt, block, []) or [], tainted)
            # module-level pytest.param(...) lists (CASES = [pytest.param(...)]) feed parametrize by name: a
            # nondeterministic id= or value there taints the name above, so the decorator check reports it.
    scope(tree.body, {})
    return found


def scan_tree(root: Path = TESTS) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out += scan_source(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return out


# ── the lint ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_no_import_time_random_value_reaches_parametrize():
    offenders = scan_tree()
    assert not offenders, (
        "parametrize values / ids must be identical in every xdist worker (S276: 'Different tests were collected'). "
        "Build the random value inside the test or a fixture, or use a fixed literal:\n  " + "\n  ".join(offenders))


# ── the lint's own cases (invented sources) ───────────────────────────────────────────────────────────────────────

BAD = {
    "uuid in values": "import uuid\nimport pytest\n"
                      "@pytest.mark.parametrize('x', [uuid.uuid4()])\ndef test_a(x): pass\n",
    "module name from uuid": "import uuid\nimport pytest\nLANTERN = str(uuid.uuid4())\n"
                             "@pytest.mark.parametrize('x', [LANTERN, 'b'])\ndef test_a(x): pass\n",
    "derived name": "from uuid import uuid4\nimport pytest\nTAG = uuid4().hex[:6]\nNAME = f'lantern-{TAG}'\n"
                    "@pytest.mark.parametrize('x', [NAME])\ndef test_a(x): pass\n",
    "ids": "import time\nimport pytest\nSTAMP = time.time()\n"
           "@pytest.mark.parametrize('x', [1, 2], ids=[f'a{STAMP}', 'b'])\ndef test_a(x): pass\n",
    "datetime.now": "from datetime import datetime, timezone\nimport pytest\n"
                    "@pytest.mark.parametrize('when', [datetime.now(timezone.utc)])\ndef test_a(when): pass\n",
    "module datetime": "import datetime as dt\nimport pytest\nTODAY = dt.date.today()\n"
                       "@pytest.mark.parametrize('d', [TODAY])\ndef test_a(d): pass\n",
    "random": "import random\nimport pytest\nN = random.randint(1, 9)\n"
              "@pytest.mark.parametrize('n', range(N))\ndef test_a(n): pass\n",
    "from random": "from random import choice as pick\nimport pytest\n"
                   "@pytest.mark.parametrize('n', [pick('ab')])\ndef test_a(n): pass\n",
    "pytest.param id": "import uuid\nimport pytest\nCASES = [pytest.param(1, id=uuid.uuid4().hex)]\n"
                       "@pytest.mark.parametrize('n', CASES)\ndef test_a(n): pass\n",
    "class scope": "import uuid\nimport pytest\nclass TestLantern:\n    KEY = uuid.uuid4()\n"
                   "    @pytest.mark.parametrize('k', [KEY])\n    def test_a(self, k): pass\n",
    "fixture params": "import uuid\nimport pytest\n"
                      "@pytest.fixture(params=[uuid.uuid4()])\ndef lantern(request): return request.param\n",
    "lambda default": "import uuid\nimport pytest\n"
                      "@pytest.mark.parametrize('make', [lambda k=uuid.uuid4(): k])\ndef test_a(make): pass\n",
    "alias import": "import pytest as pt\nimport uuid\n"
                    "@pt.mark.parametrize('x', [uuid.uuid4()])\ndef test_a(x): pass\n",
}

GOOD = {
    "uuid inside the test": "import uuid\nimport pytest\n@pytest.mark.parametrize('x', [1, 2])\n"
                            "def test_a(x):\n    assert uuid.uuid4()\n",
    "module uuid not in parametrize": "import uuid\nimport pytest\nTENANT = str(uuid.uuid4())\n"
                                      "@pytest.mark.parametrize('x', ['a', 'b'])\ndef test_a(x):\n    assert TENANT\n",
    "fixed UUID literal": "import uuid\nimport pytest\nKEY = uuid.UUID('00000000-0000-4000-8000-000000000001')\n"
                          "@pytest.mark.parametrize('k', [KEY])\ndef test_a(k): pass\n",
    "seeded Random": "import random\nimport pytest\nR = random.Random(7)\nN = [R.randint(1, 9) for _ in range(3)]\n"
                     "@pytest.mark.parametrize('n', [1])\ndef test_a(n): pass\n",
    "reassigned to a literal": "import uuid\nimport pytest\nX = uuid.uuid4()\nX = 'fixed'\n"
                               "@pytest.mark.parametrize('x', [X])\ndef test_a(x): pass\n",
    "timedelta and fixed datetime": "from datetime import datetime, timedelta\nimport pytest\n"
                                    "T0 = datetime(2026, 1, 2)\n"
                                    "@pytest.mark.parametrize('t', [T0, T0 + timedelta(days=1)])\n"
                                    "def test_a(t): pass\n",
    "argnames string only": "import pytest\n@pytest.mark.parametrize('time,uuid', [(1, 2)])\n"
                            "def test_a(time, uuid): pass\n",
    "lambda body runs in the test": "import uuid\nimport pytest\n"
                                    "@pytest.mark.parametrize('make', [lambda d: str(uuid.uuid4())])\n"
                                    "def test_a(make): pass\n",
    "fixture without params": "import uuid\nimport pytest\n@pytest.fixture\ndef lantern():\n"
                              "    return uuid.uuid4()\n",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_lint_flags(name):
    found = scan_source(BAD[name], name)
    assert len(found) == 1, found


@pytest.mark.parametrize("name", sorted(GOOD))
def test_lint_passes(name):
    assert scan_source(GOOD[name], name) == []


def test_message_names_the_file_line_and_source():
    (msg,) = scan_source(BAD["derived name"], "tests/unit/test_lantern.py")
    assert msg.startswith("tests/unit/test_lantern.py:5 @pytest.mark.parametrize on test_a uses NAME")
    assert "uuid.uuid4()" in msg

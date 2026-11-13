"""AA-1097 code-health ratchets (.github/scripts/code_health.py): complexity and file size may not get worse.

- compare(): a new function / file over the limit fails, a baseline entry may not grow, a stale entry (gone, or back
  under the limit) and an improved one fail until `update` shrinks the baseline; `update` never adds or raises.
- compare_with_base(): against the base commit the baseline only shrinks; a function moved to another file keeps its
  entry (same qualified name, score not higher); a split baseline file's parts may not total more than the file had.
- Python qualified names (Class.method, outer.inner, "#2" for a repeated name) and, when the frontend's node_modules
  are installed, the TypeScript names from code_health_eslint.mjs.
- End to end in a throwaway git repository: init -> check ok; a grown file and a new over-limit function fail;
  a baseline entry added after the base fails `check --base`; a raised limit fails.
The same file is vendored in every app repo (AA-CIS-App, AA-Booking, AA-Partner, AA-TripPlanner-Web); unittest only, so
it runs under pytest and under `python3 -m unittest`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".github" / "scripts" / "code_health.py").is_file():
            return parent
    raise RuntimeError("code_health.py not found above this test")


REPO = _repo_root()
_spec = importlib.util.spec_from_file_location("code_health", REPO / ".github" / "scripts" / "code_health.py")
ch = importlib.util.module_from_spec(_spec)
sys.modules["code_health"] = ch
_spec.loader.exec_module(ch)


def _ruff_available() -> bool:
    try:
        ch.find_ruff()
    except ch.ToolMissing:
        return False
    return True


def _frontend_with_eslint() -> Path | None:
    config = json.loads((REPO / ch.CONFIG_PATH).read_text(encoding="utf-8"))
    for d in config.get("typescript", {}).get("dirs", []):
        if (REPO / d / "node_modules" / "eslint").is_dir() and shutil.which("node"):
            return REPO / d
    return None


class CompareTest(unittest.TestCase):
    def test_new_function_over_the_limit_fails(self):
        problems, _ = ch.compare("python", {"a.py::f": 12, "a.py::g": 3}, 10, {})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("NEW    a.py::f"), problems)

    def test_function_at_the_limit_passes(self):
        problems, shrunk = ch.compare("python", {"a.py::f": 10}, 10, {})
        self.assertEqual((problems, shrunk), ([], {}))

    def test_baseline_entry_may_not_grow(self):
        problems, shrunk = ch.compare("typescript", {"web/a.tsx::Page": 21}, 10, {"web/a.tsx::Page": 20})
        self.assertTrue(problems[0].startswith("WORSE  web/a.tsx::Page"), problems)
        self.assertEqual(shrunk, {"web/a.tsx::Page": 20})  # update never raises

    def test_stale_and_improved_entries_fail_until_update(self):
        current = {"a.py::gone_under": 4, "a.py::better": 15}
        baseline = {"a.py::gone_under": 14, "a.py::better": 18, "b.py::deleted": 30}
        problems, shrunk = ch.compare("python", current, 10, baseline)
        kinds = sorted(p.split()[0] for p in problems)
        self.assertEqual(kinds, ["STALE", "STALE", "TIGHT"])
        self.assertEqual(shrunk, {"a.py::better": 15})

    def test_files_new_and_grown(self):
        problems, _ = ch.compare("files", {"big.py": 900, "new.ts": 801, "ok.ts": 800}, 800, {"big.py": 850})
        self.assertEqual(sorted(p.split()[0] for p in problems), ["NEW", "WORSE"])


class CompareWithBaseTest(unittest.TestCase):
    def test_added_entry_fails(self):
        problems = ch.compare_with_base("python", {"a.py::f": 12, "a.py::g": 11}, {"a.py::f": 12})
        self.assertEqual(len(problems), 1)
        self.assertIn("ADDED  a.py::g", problems[0])

    def test_raised_entry_fails(self):
        problems = ch.compare_with_base("files", {"a.py": 900}, {"a.py": 850})
        self.assertTrue(problems[0].startswith("RAISED a.py"), problems)

    def test_removed_and_lowered_entries_pass(self):
        self.assertEqual(ch.compare_with_base("python", {"a.py::f": 11}, {"a.py::f": 12, "a.py::g": 20}), [])

    def test_moved_function_keeps_its_entry(self):
        base = {"routers/big.py::list_items": 18}
        self.assertEqual(ch.compare_with_base("python", {"routers/items.py::list_items": 18}, base), [])
        worse = ch.compare_with_base("python", {"routers/items.py::list_items": 19}, base)
        self.assertEqual(len(worse), 1)

    def test_one_removed_entry_justifies_one_move(self):
        base = {"big.py::handler": 15}
        head = {"a.py::handler": 15, "b.py::handler": 15}
        self.assertEqual(len(ch.compare_with_base("python", head, base)), 1)

    def test_split_file_parts_within_the_removed_total(self):
        base = {"routers/big.py": 3000}
        self.assertEqual(ch.compare_with_base("files", {"routers/a.py": 1600, "routers/b.py": 1300}, base), [])
        self.assertEqual(len(ch.compare_with_base("files", {"routers/a.py": 1600, "routers/b.py": 1500}, base)), 1)


class NamesTest(unittest.TestCase):
    def test_python_qualnames(self):
        source = textwrap.dedent(
            """
            class Store:
                def load(self):
                    def inner():
                        return 1
                    return inner

                @property
                def size(self):
                    return 1

                @size.setter
                def size(self, value):
                    pass

            if True:
                async def top():
                    pass
            """
        )
        names = sorted(ch.python_qualnames(source).values())
        self.assertEqual(names, ["Store.load", "Store.load.inner", "Store.size", "Store.size#2", "top"])

    def test_typescript_qualnames(self):
        frontend = _frontend_with_eslint()
        if frontend is None:
            self.skipTest("no frontend node_modules with eslint (CI runs this in the frontend job)")
        with tempfile.TemporaryDirectory(dir=frontend) as tmp:
            Path(tmp, "sample.tsx").write_text(
                textwrap.dedent(
                    """
                    export function Panel({ rows }: { rows: string[] }) {
                      const pick = (x: string) => (x ? 1 : 2);
                      return <div onClick={() => pick("a")}>{rows.map((r) => r)}{rows.map((r) => r)}</div>;
                    }
                    export class Store {
                      load(a?: number) {
                        if (a) return 1;
                        return a ?? 2;
                      }
                    }
                    export default () => 1;
                    """
                ),
                encoding="utf-8",
            )
            rel = Path(tmp).relative_to(frontend).as_posix() + "/sample.tsx"
            run = subprocess.run(["node", str(REPO / ch.ESLINT_SCRIPT), str(frontend)], input=json.dumps([rel]),
                                 capture_output=True, text=True, check=True)
        out = json.loads(run.stdout)
        self.assertEqual(out["errors"], [])
        scores = {f["name"]: f["score"] for f in out["functions"]}
        self.assertEqual(scores, {"Panel": 1, "Panel.pick": 2, "Panel.onClick": 1, "Panel.map()": 1,
                                  "Panel.map()#2": 1, "Store.load": 3, "default": 1})


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".github").mkdir()
        self.git("init", "-q", "-b", "main")
        self.config = {
            "files": {"max_lines": 5, "include": ["*.py"], "exempt": [{"glob": "gen/*", "reason": "generated"}]},
        }
        if _ruff_available():
            self.config["python"] = {"max_complexity": 3, "include": ["*.py"], "exclude": []}
        self.write_config()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=env)

    def write_config(self):
        (self.root / ch.CONFIG_PATH).write_text(json.dumps(self.config), encoding="utf-8")

    def run_cli(self, *args) -> int:
        with open(os.devnull, "w") as sink:
            stdout = sys.stdout
            sys.stdout = sink
            try:
                return ch.main([*args, "--root", str(self.root)])
            finally:
                sys.stdout = stdout

    def baseline(self) -> dict:
        return json.loads((self.root / ch.BASELINE_PATH).read_text(encoding="utf-8"))

    def test_file_size_ratchet(self):
        (self.root / "big.py").write_text("x = 1\n" * 7, encoding="utf-8")
        (self.root / "gen").mkdir()
        (self.root / "gen" / "table.py").write_text("x = 1\n" * 50, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "files"), 1)  # big.py is new and over the limit
        self.assertEqual(self.run_cli("init", "--kind", "files"), 0)
        self.assertEqual(self.baseline()["files"], {"big.py": 7})  # the exempt file is not listed
        self.assertEqual(self.run_cli("check", "--kind", "files"), 0)
        (self.root / "big.py").write_text("x = 1\n" * 8, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "files"), 1)  # grew
        (self.root / "big.py").write_text("x = 1\n" * 6, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "files"), 1)  # improved: baseline must shrink
        self.assertEqual(self.run_cli("update", "--kind", "files"), 0)
        self.assertEqual(self.baseline()["files"], {"big.py": 6})
        (self.root / "new.py").write_text("x = 1\n" * 9, encoding="utf-8")
        self.assertEqual(self.run_cli("update", "--kind", "files"), 1)  # update refuses to add
        self.assertNotIn("new.py", self.baseline()["files"])

    def test_baseline_only_shrinks_against_the_base(self):
        (self.root / "big.py").write_text("x = 1\n" * 7, encoding="utf-8")
        self.run_cli("init", "--kind", "files")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        (self.root / "other.py").write_text("x = 1\n" * 9, encoding="utf-8")
        self.run_cli("init", "--kind", "files")  # someone regenerates the baseline to hide other.py
        self.assertEqual(self.run_cli("check", "--kind", "files"), 0)
        self.assertEqual(self.run_cli("check", "--kind", "files", "--base", "HEAD"), 1)

    def test_raised_limit_fails_against_the_base(self):
        self.run_cli("init", "--kind", "files")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.config["files"]["max_lines"] = 50
        self.write_config()
        self.assertEqual(self.run_cli("check", "--kind", "files", "--base", "HEAD"), 1)

    def test_exemption_needs_a_reason(self):
        self.config["files"]["exempt"] = [{"glob": "gen/*"}]
        self.write_config()
        self.assertEqual(self.run_cli("check", "--kind", "files"), 1)

    def test_python_complexity_ratchet(self):
        if "python" not in self.config:
            self.skipTest("ruff is not installed")
        branchy = "def f(a, b, c):\n    if a:\n        return 1\n    if b:\n        return 2\n    if c:\n        return 3\n"
        simple = "def g():\n    return 0\n"
        (self.root / "m.py").write_text(simple, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "python"), 0)
        (self.root / "m.py").write_text(simple + branchy, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "python"), 1)  # new function over the limit
        self.run_cli("init", "--kind", "python")
        self.assertEqual(self.baseline()["python"], {"m.py::f": 4})
        self.assertEqual(self.run_cli("check", "--kind", "python"), 0)
        worse = branchy + "    if a and b:\n        return 4\n"
        (self.root / "m.py").write_text(simple + worse, encoding="utf-8")
        self.assertEqual(self.run_cli("check", "--kind", "python"), 1)  # a baseline function may not get worse


if __name__ == "__main__":
    unittest.main()

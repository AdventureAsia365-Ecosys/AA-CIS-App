"""CI "Migration preflight" (AA-993 M4b, root ADR 0017 d1): ``.github/scripts/migration_preflight.py``.

Pins the refusals (copied file edited or deleted, number not main max + 1, a contract or destructive file together
with a readiness raise to it, a raise with no file, an expand header the classify evidence contradicts, a runner lint
error) and the verdict the author reads (expand = auto-applied, otherwise a person applies it). ``main()`` runs for
real on a throw-away git repository, once with stand-ins for the pinned AA-CIS-Infra tools. The file is identical in
AA-CIS-App, AA-Booking and AA-Partner; so is this test."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _repo() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / ".github" / "scripts" / "migration_preflight.py").is_file():
            return p
    raise RuntimeError("migration_preflight.py not found above this test")


REPO = _repo()
sys.path.insert(0, str(REPO / ".github" / "scripts"))
mp = importlib.import_module("migration_preflight")

EXPAND = "-- migrate: expand\nBEGIN;\nALTER TABLE booking.t ADD COLUMN IF NOT EXISTS n text;\nCOMMIT;\n"
CONTRACT = "BEGIN;\nALTER TABLE booking.t ALTER COLUMN n SET NOT NULL;\nCOMMIT;\n"
DESTRUCTIVE = "-- migrate: destructive\nBEGIN;\nDROP TABLE booking.old;\nCOMMIT;\n"


def _rep(name: str, sql: str, **kw) -> mp.FileReport:
    rep = mp.FileReport(name, f"bk-{name[:3]}", header=mp.header_of(sql))
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


def _assess(
    added: dict, base_req: set, head_req: set, *, base=("001_a.sql", "002_b.sql"), changes=None, copies=None, ooo=False
) -> mp.Result:
    ch = changes or {"A": sorted(added), "M": [], "D": []}
    reports = {n: _rep(n, s) for n, s in added.items()}
    head = sorted(set(base) | set(added))
    return mp.assess(
        "booking", list(base), head, ch, base_req, head_req, reports, set(base) if copies is None else copies, ooo
    )


BASE_REQ = {"bk-001", "bk-002"}


def test_header_reading() -> None:
    assert mp.header_of(EXPAND) == "expand"
    assert mp.header_of(CONTRACT) == "none"
    assert mp.header_of(DESTRUCTIVE) == "destructive"
    assert mp.header_of("BEGIN;\n-- migrate: expand\n") == "none", "a directive after the first statement is ignored"
    assert mp.header_of("-- migrate: expand  -- note\nBEGIN;\n") == "expand"


def test_name_status() -> None:
    text = "A\tmigrations/003_x.sql\nM\tmigrations/002_b.sql\nD\tmigrations/001_a.sql\nA\tmigrations/README.md\n"
    assert mp.parse_name_status(text) == {"A": ["003_x.sql"], "M": ["002_b.sql"], "D": ["001_a.sql"]}


def test_expand_file_with_raise_passes_and_says_auto_apply() -> None:
    r = _assess({"003_x.sql": EXPAND}, BASE_REQ, BASE_REQ | {"bk-003"})
    assert r.errors == []
    assert r.files[0].klass == "expand" and "auto-applied" in mp.verdict_line(r.files[0])
    assert "**expand**" in mp.summary_markdown(r)


@pytest.mark.parametrize(("sql", "klass"), [(CONTRACT, "contract"), (DESTRUCTIVE, "destructive")])
def test_non_expand_file_with_raise_is_refused(sql: str, klass: str) -> None:
    r = _assess({"003_x.sql": sql}, BASE_REQ, BASE_REQ | {"bk-003"})
    assert any(f"is {klass}" in e and "MIG-4" in e for e in r.errors)


def test_contract_file_alone_passes_and_says_manual() -> None:
    r = _assess({"003_x.sql": CONTRACT}, BASE_REQ, BASE_REQ)
    assert r.errors == []
    assert "never auto-applied" in mp.verdict_line(r.files[0])
    assert any("not required by readiness" in n for n in r.notes)


def test_raise_without_a_file_is_refused() -> None:
    r = _assess({}, BASE_REQ, BASE_REQ | {"bk-007"})
    assert any("no migration file for it" in e for e in r.errors)


def test_raise_to_a_file_already_on_main_is_a_note() -> None:
    r = _assess({}, {"bk-001"}, BASE_REQ)
    assert r.errors == [] and any("already on main" in n for n in r.notes)


def test_classify_contradicting_the_header_is_refused() -> None:
    rep = _rep("003_x.sql", EXPAND, classify="contract", findings=["new policy on booking.t"])
    r = mp.assess(
        "booking",
        ["001_a.sql", "002_b.sql"],
        ["001_a.sql", "002_b.sql", "003_x.sql"],
        {"A": ["003_x.sql"]},
        BASE_REQ,
        BASE_REQ,
        {"003_x.sql": rep},
        set(),
        False,
    )
    assert any("classify replay" in e and "new policy" in e for e in r.errors)
    assert rep.klass == "contract"


def test_lint_error_is_refused_and_makes_it_contract() -> None:
    rep = _rep("003_x.sql", EXPAND, lint_errors=["line 3: CREATE OR REPLACE ..."])
    r = mp.assess(
        "booking",
        ["001_a.sql"],
        ["001_a.sql", "003_x.sql"],
        {"A": ["003_x.sql"]},
        set(),
        set(),
        {"003_x.sql": rep},
        set(),
        False,
    )
    assert rep.klass == "contract" and any("CREATE OR REPLACE" in e for e in r.errors)


@pytest.mark.parametrize(
    ("added", "ooo", "ok"),
    [
        (["003_x.sql"], False, True),
        (["003_x.sql", "004_y.sql"], False, True),
        (["004_y.sql"], False, False),  # gap
        (["002_dup.sql"], False, False),  # taken
        (["000_low.sql"], True, True),  # reserved lower number with the label
    ],
)
def test_numbers(added: list, ooo: bool, ok: bool) -> None:
    errors, _ = mp.number_errors(["001_a.sql", "002_b.sql"], added, ooo)
    assert (errors == []) is ok


def test_copied_files_are_immutable() -> None:
    errors, notes = mp.immutable_errors({"M": ["001_a.sql"], "D": ["002_b.sql"]}, {"001_a.sql", "002_b.sql"})
    assert len(errors) == 2 and notes == []
    errors, notes = mp.immutable_errors({"M": ["002_b.sql"]}, {"001_a.sql"})
    assert errors == [] and "not copied" in notes[0]
    errors, _ = mp.immutable_errors({"M": ["002_b.sql"]}, None)
    assert "counts as copied" in errors[0]


def test_classify_entries(tmp_path: Path) -> None:
    out = {
        "files": [
            {"file": "booking/003_x.sql", "header": "expand", "lint_errors": [], "findings": [], "error": ""},
            {"file": "booking/004_y.sql", "header": "expand", "lint_errors": [], "findings": ["policy"], "error": ""},
            {"file": "booking/005_z.sql", "header": "expand", "lint_errors": [], "findings": [], "error": "boom"},
            {"file": "cis/300_a.sql", "header": "expand", "lint_errors": [], "findings": ["x"], "error": ""},
        ]
    }
    path = tmp_path / "classify.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    got, note = mp.classify_entries(str(path), 14, "booking")
    assert got == {"003_x.sql": ("expand", []), "004_y.sql": ("contract", ["policy"])}
    assert note == ""
    assert mp.classify_entries("", None, "booking")[0] == {}


# ── main() on a real git repository ─────────────────────────────────────────────────────────────────────────────────

GIT = shutil.which("git")

READY = 'REQUIRED_SCHEMA_VERSIONS = ("bk-001", "bk-002")\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 (fixed git argv in a temp repo)
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if GIT is None:
        pytest.skip("needs git")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_a.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    (tmp_path / "migrations" / "002_b.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    (tmp_path / "db.py").write_text(READY, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _main(repo: Path, *extra: str) -> tuple:
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    summary = repo / "summary.md"
    code = mp.main(
        [
            "--app",
            "booking",
            "--migrations-dir",
            "migrations",
            "--readiness-file",
            "db.py",
            "--base",
            base,
            "--summary",
            str(summary),
            *extra,
        ]
    )
    return code, summary.read_text(encoding="utf-8")


def test_main_expand_with_raise_passes(repo: Path) -> None:
    (repo / "migrations" / "003_x.sql").write_text(EXPAND, encoding="utf-8")
    (repo / "db.py").write_text(READY.replace('"bk-002")', '"bk-002", "bk-003")'), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr")
    code, md = _main(repo)
    assert code == 0 and "**expand**" in md and "header only" in md


def test_main_contract_with_raise_and_an_edit_fail(repo: Path) -> None:
    (repo / "migrations" / "003_x.sql").write_text(CONTRACT, encoding="utf-8")
    (repo / "migrations" / "001_a.sql").write_text("BEGIN;\nSELECT 1;\nCOMMIT;\n", encoding="utf-8")
    (repo / "db.py").write_text(READY.replace('"bk-002")', '"bk-002", "bk-003")'), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr")
    code, md = _main(repo)
    assert code == 1
    assert "is contract" in md and "`001_a.sql`" in md and "immutable" in md


def test_main_uses_the_pinned_infra_tools(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    tools = tmp_path_factory.mktemp("infra")
    (tools / "db").mkdir()
    # Stand-ins with the real CLI shapes: --inspect prints one JSON line; the ownership lint prints FAIL lines.
    (tools / "db" / "run_migration.py").write_text(
        "import json, sys\n"
        "sql = open(sys.argv[2]).read()\n"
        "assert '/db/migrations/booking/' in sys.argv[2].replace(chr(92), '/')\n"
        "print(json.dumps({'expand': 'expand' in sql, 'destructive': False, 'no_transaction': False,\n"
        "                  'errors': ['line 2: lint says no'] if 'NOPE' in sql else [], 'warnings': []}))\n",
        encoding="utf-8",
    )
    (tools / "db" / "check_migration_ownership.py").write_text(
        "import sys\nprint('FAIL  ' + sys.argv[1] + ': writes crm.x') if 'crm.x' in open(sys.argv[1]).read() "
        "else None\nsys.exit(1 if 'crm.x' in open(sys.argv[1]).read() else 0)\n",
        encoding="utf-8",
    )
    copies = tmp_path_factory.mktemp("copies")
    for n in ("001_a.sql", "002_b.sql"):
        (copies / n).write_text("x", encoding="utf-8")
    (repo / "migrations" / "003_x.sql").write_text(
        EXPAND + "-- NOPE\nINSERT INTO crm.x VALUES (1);\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr")
    code, md = _main(repo, "--infra-tools", str(tools), "--infra-copies", str(copies))
    assert code == 1
    assert "lint says no" in md and "ownership: writes crm.x" in md and "header only" not in md

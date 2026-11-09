"""Dangling migration references (AA-1024 l22, S271 lessons guard G10): ``.github/scripts/migration_refs.py``.

A renumbered migration keeps its name, so the tests and docs that still name ``316_name`` after the file became
``314_name.sql`` are refused, on the lines the PR adds only; so is a reference to a file the PR removes. Another
owner's file, a fixture name and a line marked ``old-mig-ok`` pass. ``migration_preflight.main()`` refuses them too
(run on a throw-away git repository), also on a PR that touches no migration, and the CI step that runs it has no
``if:`` (the scope step only skips the Infra tools and the classify replay). The file is identical in AA-CIS-App,
AA-Booking and AA-Partner; so is this test (AA-TripPlanner-Web has the same script with a unittest copy of these
cases)."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _repo() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / ".github" / "scripts" / "migration_refs.py").is_file():
            return p
    raise RuntimeError("migration_refs.py not found above this test")


REPO = _repo()
sys.path.insert(0, str(REPO / ".github" / "scripts"))
mr = importlib.import_module("migration_refs")
mp = importlib.import_module("migration_preflight")

HEAD = ["300_base.sql", "314_widget_gate.sql", "315_gizmo_rows.sql"]
BASE = ["300_base.sql", "301_old_sprocket.sql"]


def _lines(*texts: str) -> list:
    return [("tests/test_x.py", i + 1, t) for i, t in enumerate(texts)]


def test_old_number_of_a_renumbered_file_is_refused() -> None:
    got = mr.dangling(_lines("path = MIG / '316_widget_gate.sql'", "see 316_widget_gate for the guard"), HEAD, BASE)
    assert len(got) == 2
    assert got[0].startswith("tests/test_x.py:1: `316_widget_gate.sql` names migration number 316")
    assert "314_widget_gate.sql" in got[0]


def test_removed_file_is_refused() -> None:
    got = mr.dangling(_lines("replays 301_old_sprocket.sql first"), HEAD, BASE)
    assert got == [
        "tests/test_x.py:1: `301_old_sprocket.sql` names 301_old_sprocket.sql, which this PR removes or renames"
    ]


def test_current_names_other_owners_fixtures_and_marked_lines_pass() -> None:
    assert (
        mr.dangling(
            _lines(
                "the file 314_widget_gate.sql and 315_gizmo_rows",
                "booking 032_trip_case_notes.sql is another owner's file",
                "fixture names 999_x and 300_y",
                "renumbered from 316_widget_gate  # old-mig-ok: history note",
                "test_aa1002_migration_316_widget_gate and aa316_widget_gate and 1_000_000",
                "MAX = 100_000",
            ),
            HEAD,
            BASE,
        )
        == []
    )


def test_added_lines_reads_only_added_lines_with_their_head_numbers() -> None:
    diff = "\n".join(
        [
            "diff --git a/tests/a.py b/tests/a.py",
            "index 1..2 100644",
            "--- a/tests/a.py",
            "+++ b/tests/a.py",
            "@@ -3,0 +4,2 @@ def f():",
            "+x = '316_widget_gate'",
            "+y = 1",
            "@@ -9 +11 @@",
            "-old = '316_widget_gate'",
            "+++ starts like a header but is content",
            "diff --git a/gone.py b/gone.py",
            "deleted file mode 100644",
            "--- a/gone.py",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-z = '316_widget_gate'",
        ]
    )
    assert mr.added_lines(diff) == [
        ("tests/a.py", 4, "x = '316_widget_gate'"),
        ("tests/a.py", 5, "y = 1"),
        ("tests/a.py", 11, "++ starts like a header but is content"),
    ]


GIT = shutil.which("git")


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
    (tmp_path / "db.py").write_text('REQUIRED_SCHEMA_VERSIONS = ("bk-001",)\n', encoding="utf-8")
    (tmp_path / "notes.md").write_text("history\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _preflight(repo: Path) -> tuple:
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    summary = repo / "summary.md"
    argv = ["--app", "booking", "--migrations-dir", "migrations", "--readiness-file", "db.py", "--base", base]
    code = mp.main([*argv, "--summary", str(summary)])
    return code, summary.read_text(encoding="utf-8"), mr.main(["--migrations-dir", "migrations", "--base", base])


def test_preflight_refuses_a_reference_left_behind_by_a_renumber(repo: Path) -> None:
    (repo / "migrations" / "002_widget_gate.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    (repo / "notes.md").write_text("history\nthe gate is in 004_widget_gate.sql\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "renumbered 004 -> 002, one reference left")
    code, md, refs = _preflight(repo)
    assert code == 1 and refs == 1
    assert "dangling migration reference: notes.md:2: `004_widget_gate.sql`" in md


def test_a_non_utf8_added_line_does_not_crash_the_check(repo: Path) -> None:
    """A Latin-1 byte in an added line (a fixture, a legacy export) used to raise UnicodeDecodeError in git()'s strict
    decode and crash the preflight; the line is read with U+FFFD and the tokens on it are still checked."""
    (repo / "migrations" / "002_widget_gate.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    (repo / "notes.md").write_bytes(b"history\ncaf\xe9 note: the gate is in 004_widget_gate.sql\n")
    (repo / "legacy.csv").write_bytes(b"name\nM\xfcller\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a Latin-1 line next to a stale reference")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    errors = mr.check("migrations", base)
    assert len(errors) == 1 and errors[0].startswith("notes.md:2: `004_widget_gate.sql`")
    code, md, refs = _preflight(repo)
    assert code == 1 and refs == 1
    assert "dangling migration reference: notes.md:2: `004_widget_gate.sql`" in md


def test_preflight_passes_once_the_reference_follows_the_file(repo: Path) -> None:
    (repo / "migrations" / "002_widget_gate.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    (repo / "notes.md").write_text("history\nthe gate is in 002_widget_gate.sql\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "renumbered with its references")
    code, md, refs = _preflight(repo)
    assert code == 0 and refs == 0
    assert "dangling" not in md


def test_a_pr_that_touches_no_migration_is_still_checked(repo: Path) -> None:
    """The renumber merged earlier (002_widget_gate.sql is on the base); a later PR changes only a doc and names the
    old number. No migration and no readiness key in the diff, and the preflight still refuses it."""
    (repo / "migrations" / "002_widget_gate.sql").write_text("BEGIN;\nCOMMIT;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the renumbered file, merged")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "notes.md").write_text("history\nthe gate is in 004_widget_gate.sql\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "a doc-only PR with the old number")
    assert _git(repo, "diff", "--name-only", base, "HEAD", "--", "migrations", "db.py") == ""
    summary = repo / "summary.md"
    argv = ["--app", "booking", "--migrations-dir", "migrations", "--readiness-file", "db.py", "--base", base]
    assert mp.main([*argv, "--summary", str(summary)]) == 1
    assert "dangling migration reference: notes.md:2: `004_widget_gate.sql`" in summary.read_text(encoding="utf-8")


def _job_block(text: str, job: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"  {job}:")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("  ")
            and not lines[i].startswith("   ")
            and lines[i].rstrip().endswith(":")
            and not lines[i].lstrip().startswith("#")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_preflight_verdict_step_runs_on_every_pr() -> None:
    """The scope step's "relevant" output skips the Infra tools and the classify replay only: the verdict step (which
    runs migration_refs) has no ``if:``, and the job has no path filter, so a doc-only PR is checked too."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = _job_block(ci, "migration-preflight")
    head, _, steps = job.partition("    steps:")
    assert [ln.strip() for ln in head.splitlines() if ln.strip().startswith("if:")] == [
        "if: github.event_name == 'pull_request' || github.event_name == 'merge_group'"
    ]
    verdict = steps[steps.index("- name: Preflight verdict") :]
    nxt = verdict.find("\n      - ", 1)
    verdict = verdict if nxt < 0 else verdict[:nxt]
    assert not any(ln.strip().startswith("if:") for ln in verdict.splitlines()), "the verdict step must always run"
    assert "python3 .github/scripts/migration_preflight.py" in verdict
    on = ci[ci.index("\non:") : ci.index("\njobs:")]
    assert "paths:" not in on and "paths-ignore:" not in on

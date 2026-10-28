"""AA-994 — user-visible text never carries an internal Linear issue ID (AA-123).

Admins, tenants and Ms. Thư read the pages; an issue ID tells them nothing. IDs stay in code comments, tests and
commit text. Two scans:

  1. Frontend (frontend/app, frontend/lib, frontend/components, .ts / .tsx, tests excluded): every string literal,
     template literal and JSX text, comments removed (tests/unit/ui_text_scan.py).
  2. Backend strings that reach a screen: every string constant of api/ (route summaries and descriptions are the
     /admin/api-docs page, HTTPException details, labels, notes, data coverage rows) and of the service modules
     whose text the admin pages show (gate ledger notes, erasure checklist, writing-memory reasons). Docstrings,
     log calls, argparse help, SQL comments and code-only tables (CODE_ONLY_TABLES) are not shown and are not
     scanned.

The tool-facing kit lint messages (frontend/app/_kit/core/eslint-kit.mjs) are for developers and are not scanned.
"""
import re
from pathlib import Path

from tests.unit.ui_text_scan import python_ui_strings, ts_hits

REPO = Path(__file__).resolve().parents[2]
ISSUE_ID = re.compile(r"(?<![A-Za-z0-9])AA-[0-9]+")

FRONTEND_ROOTS = ("frontend/app", "frontend/lib", "frontend/components")
SKIPPED_DIRS = {"node_modules", ".next"}
# Service modules whose strings the admin pages render (the rest of services/ writes logs and prompts).
SERVICE_MODULES = (
    "services/acp_content_writing/service.py",      # gate ledger notes on Content quality
    "services/erasure/inventory.py",                # the data erasure checklist
    "services/jobs/erasure_job.py",                 # erasure approval refusals
    "services/writing_memory/l0.py",                # writing memory reasons
    "services/writing_memory/replay.py",
    "services/acp_shared/question_topup.py",        # top-up refusals on the tenant pages
    "services/content_generation/brand_audit_node.py",
    "services/tour_lifecycle/derivatives.py",       # the cancelled job's error on Jobs
    "services/tour_lifecycle/service.py",           # reviewer notes on the Review Queue
    "shared/jobs/registry.py",                      # retired kinds: the cancelled row's error on Jobs
)

# Module-level tables whose text no page renders: the machine-caller allowlist names who calls each route.
CODE_ONLY_TABLES = frozenset({"MACHINE_ROUTES"})


def _frontend_files():
    for root in FRONTEND_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            rel = path.relative_to(REPO)
            if (path.is_file() and path.suffix in {".ts", ".tsx"} and ".test." not in path.name
                    and not SKIPPED_DIRS & set(rel.parts)):
                yield path


def _backend_files():
    for path in sorted((REPO / "api").rglob("*.py")):
        if "migrations" not in path.relative_to(REPO).parts:
            yield path
    for rel in SERVICE_MODULES:
        yield REPO / rel


def test_frontend_text_has_no_issue_id():
    found = [hit for path in _frontend_files() for hit in ts_hits(path, ISSUE_ID, REPO)]
    assert not found, ("User-visible text carries an internal issue ID (AA-994). Say what it means instead, or "
                       "move the ID into a code comment:\n" + "\n".join(found))


def test_backend_text_shown_in_the_admin_has_no_issue_id():
    found = [f"{path.relative_to(REPO)}:{no}: {text.strip()[:120]!r}"
             for path in _backend_files() for no, text in python_ui_strings(path, CODE_ONLY_TABLES)
             if ISSUE_ID.search(text)]
    assert not found, ("A string the admin can show carries an internal issue ID (AA-994). Say what it means "
                       "instead, or move the ID into a code comment:\n" + "\n".join(found))


def test_the_scans_see_the_code():
    # A path refactor must not turn the guard into a no-op.
    fe = {p.relative_to(REPO).as_posix() for p in _frontend_files()}
    assert "frontend/app/admin/cost-ops/page.tsx" in fe and "frontend/lib/crmConsole.ts" in fe
    assert not any(".test." in p for p in fe)
    be = {p.relative_to(REPO).as_posix() for p in _backend_files()}
    assert "api/routers/admin_llm_ops.py" in be and not any("/migrations/" in p for p in be)
    assert all((REPO / rel).is_file() for rel in SERVICE_MODULES)


def test_the_lexer_keeps_text_and_drops_comments(tmp_path):
    f = tmp_path / "x.tsx"
    f.write_text(
        "// AA-1 a comment\n"
        "const a = 1; /* AA-2\n spans lines */ const b = \"AA-3 in a string\";\n"
        "{/* AA-4 a JSX comment */}\n"
        "<p>Don't drop AA-5 in JSX text</p>\n"
        "const t = `x ${y} AA-6`;\n"
        "const ok = 'AA-' + n; // AA-7\n",
        encoding="utf-8")
    hits = ts_hits(f, ISSUE_ID, tmp_path)
    assert [h.split(":")[1] for h in hits] == ["3", "5", "6"], hits
    assert all("AA-1" not in h and "AA-4" not in h and "AA-7" not in h for h in hits)


def test_python_scan_skips_what_is_never_shown(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        '"""AA-1 module docstring."""\n'
        "import logging\n"
        "logger = logging.getLogger()\n"
        "def g():\n"
        '    """AA-2 docstring."""\n'
        '    logger.info("AA-3 log line", extra="AA-4")\n'
        '    sql = """SELECT 1 -- AA-5 sql comment\n"""\n'
        '    return {"detail": "AA-6 shown"}\n',
        encoding="utf-8")
    shown = [t for _, t in python_ui_strings(f) if ISSUE_ID.search(t)]
    assert shown == ["AA-6 shown"]

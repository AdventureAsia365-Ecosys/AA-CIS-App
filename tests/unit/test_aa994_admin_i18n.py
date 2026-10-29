"""AA-994 — the admin UI has one language at a time: English by default, Vietnamese when the user picks it (S255).

Vietnamese text lives only in the dictionary files, never hard-coded in a component: `messages.ts` /
`<name>.messages.ts` (built with the kit's `defineMessages(en, vi)`) and, for structured content such as a Guide,
`<name>.vi.ts` next to its English `<name>.ts` (picked with `useLocalized({ en, vi })`).

Scan: every string literal, template literal and JSX text (comments removed, tests/unit/ui_text_scan.py) of
frontend/app, frontend/lib and frontend/components, .ts and .tsx, tests and dictionary files excluded. A line
counts when it has a Vietnamese letter: ă â đ ê ô ơ ư ĩ ũ or a tone-marked vowel of the Latin Extended Additional
block (ạ ả ấ … ỹ). The plain Latin-1 accents (é, à, ó, ñ) do not count on their own, so "Café" or "señor" pass;
â ê ô do (a French "hôtel" in UI text would be flagged).

The files that still carry Vietnamese are in tests/unit/aa994_vietnamese_allowlist.txt with their line count. The list
only shrinks: a new file fails, a higher count fails, and a lower count fails until the list is lowered to match.
"""
import re
from pathlib import Path

from tests.unit.ui_text_scan import strip_ts_comments

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("aa994_vietnamese_allowlist.txt")
FRONTEND_ROOTS = ("frontend/app", "frontend/lib", "frontend/components")
SKIPPED_DIRS = {"node_modules", ".next"}
# ă â đ ê ô ơ ư ĩ ũ (both cases) and U+1EA0–U+1EF9 (ạ ả ấ … ỹ).
VIETNAMESE = re.compile(r"[ăĂâÂđĐêÊôÔơƠưƯĩĨũŨ\u1EA0-\u1EF9]")
# The ceiling of the list (4 files / 5 lines at the guard's first commit, lowered when step B landed); it may only go
# down (edit it down with the list).
BASELINE_FILES = 1
BASELINE_LINES = 2


def is_dictionary(path: Path) -> bool:
    return path.name == "messages.ts" or path.name.endswith((".messages.ts", ".vi.ts"))


def frontend_files():
    for root in FRONTEND_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            rel = path.relative_to(REPO)
            if (path.is_file() and path.suffix in {".ts", ".tsx"} and ".test." not in path.name
                    and not SKIPPED_DIRS & set(rel.parts) and not is_dictionary(path)):
                yield path


def vietnamese_lines(path: Path) -> list[tuple[int, str]]:
    text = strip_ts_comments(path.read_text(encoding="utf-8", errors="replace"))
    return [(no, line.strip()) for no, line in enumerate(text.splitlines(), 1) if VIETNAMESE.search(line)]


def read_allowlist() -> dict[str, int]:
    out: dict[str, int] = {}
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, count = line.split()[:2]
        assert path not in out, f"{path} is listed twice in {ALLOWLIST.name}"
        out[path] = int(count)
    return out


def test_vietnamese_only_in_dictionaries():
    allowed = read_allowlist()
    problems = []
    for path in frontend_files():
        rel = path.relative_to(REPO).as_posix()
        hits = vietnamese_lines(path)
        limit = allowed.get(rel, 0)
        if len(hits) > limit:
            shown = "\n".join(f"    {rel}:{no}: {text[:120]}" for no, text in hits)
            problems.append(f"{rel}: {len(hits)} line(s) with Vietnamese, {limit} allowed. Move the text into the "
                            f"area's messages.ts (defineMessages(en, vi)) and render it with useT:\n{shown}")
    assert not problems, "Hard-coded Vietnamese in the admin UI (AA-994):\n" + "\n".join(problems)


def test_the_allowlist_only_shrinks():
    allowed = read_allowlist()
    stale = []
    for rel, count in allowed.items():
        path = REPO / rel
        actual = len(vietnamese_lines(path)) if path.is_file() else 0
        if actual < count:
            stale.append(f"{rel}: {actual} line(s) now, listed {count} — lower or drop the line in {ALLOWLIST.name}")
    assert not stale, "The Vietnamese allow-list is ahead of the code; shrink it:\n" + "\n".join(stale)
    assert len(allowed) <= BASELINE_FILES and sum(allowed.values()) <= BASELINE_LINES, (
        f"{ALLOWLIST.name} grew ({len(allowed)} files / {sum(allowed.values())} lines, ceiling {BASELINE_FILES} / "
        f"{BASELINE_LINES}). Put new Vietnamese text in a messages.ts dictionary instead.")


def test_dictionary_files_are_dictionaries():
    # The exemption is by file name, so a file that uses it must be a dictionary: a messages file builds its catalogue
    # with defineMessages(en, vi); a `<name>.vi.ts` file is the Vietnamese twin of an English `<name>.ts`.
    offenders = []
    for root in FRONTEND_ROOTS:
        for path in (REPO / root).rglob("*.ts"):
            if not is_dictionary(path) or SKIPPED_DIRS & set(path.relative_to(REPO).parts):
                continue
            rel = path.relative_to(REPO).as_posix()
            if path.name.endswith(".vi.ts"):
                if not path.with_name(path.name[:-len(".vi.ts")] + ".ts").is_file():
                    offenders.append(f"{rel}: no English twin {path.name[:-len('.vi.ts')]}.ts")
            elif "defineMessages(" not in strip_ts_comments(path.read_text(encoding="utf-8")):
                offenders.append(f"{rel}: not built with defineMessages(en, vi)")
    assert not offenders, "Misnamed dictionary files:\n" + "\n".join(offenders)


def test_the_scan_sees_the_code():
    files = {p.relative_to(REPO).as_posix() for p in frontend_files()}
    assert "frontend/app/admin/cost-ops/page.tsx" in files and "frontend/app/_kit/Topbar.tsx" in files
    assert "frontend/app/_kit/shell.messages.ts" not in files      # a dictionary
    assert not any(".test." in f for f in files)
    assert (REPO / "frontend/app/_kit/shell.messages.ts").is_file()


def test_the_detector(tmp_path):
    f = tmp_path / "x.tsx"
    f.write_text(
        "// Tiếng Việt in a comment\n"
        "const a = \"Đăng xuất\";\n"
        "{/* ghi chú */}\n"
        "<p>Không có thông báo</p>\n"
        "const b = `Café, señor, à la carte`;\n"
        "const c = 'Hello';\n",
        encoding="utf-8")
    assert [no for no, _ in vietnamese_lines(f)] == [2, 4]

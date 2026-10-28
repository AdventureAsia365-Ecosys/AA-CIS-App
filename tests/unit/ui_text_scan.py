"""Helpers for the user-visible-text guards (AA-994 issue IDs, AA-998 account keys).

`strip_ts_comments` removes // and /* */ comments (JSX `{/* */}` included) from TypeScript / TSX source and keeps
everything else — string literals, template literals, JSX text, code — with line numbers intact, so a guard can
search what a page can render. It is a small lexer, not a parser: a quote opens a string only where a string can
start (after an operator, a bracket or a keyword), so an apostrophe in JSX text ("don't") is not taken for one.
`//` inside JSX text (a bare URL) is read as a comment: the guard may miss text there, it never flags a comment.

`python_ui_strings` yields the string constants of a Python module that can reach a screen: docstrings, bare
string expressions, logger / structlog calls, argparse help, SQL `--` comments and named code-only tables are left
out.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator

_KEYWORDS = {"return", "case", "typeof", "in", "of", "else", "yield", "await", "void", "delete", "throw", "new",
             "export", "default", "from", "import", "instanceof", "extends"}
_OPENERS = set("([{,;:=!&|?+-*%^~<>}")


def _string_may_start(src: str, i: int) -> bool:
    k = i - 1
    while k >= 0 and src[k] in " \t\r\n":
        k -= 1
    if k < 0 or src[k] in _OPENERS:
        return True
    if src[k].isalnum() or src[k] in "_$":
        m = re.search(r"[A-Za-z_$][\w$]*$", src[:k + 1])
        return bool(m and m.group() in _KEYWORDS)
    return False


def strip_ts_comments(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nx = src[i + 1] if i + 1 < n else ""
        if c == "/" and nx == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nx == "*":
            j = src.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append(" " + "\n" * src.count("\n", i, end))
            i = end
            continue
        if c in "'\"" and _string_may_start(src, i):
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        if c == "`":
            j, depth = i + 1, 0
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if depth == 0 and ch == "`":
                    break
                if ch == "$" and src[j + 1:j + 2] == "{":
                    depth += 1
                    j += 2
                    continue
                if depth and ch == "}":
                    depth -= 1
                elif depth and ch == "{":
                    depth += 1
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def ts_hits(path: Path, pattern: re.Pattern, repo: Path) -> list[str]:
    text = strip_ts_comments(path.read_text(encoding="utf-8", errors="replace"))
    return [f"{path.relative_to(repo)}:{no}: {line.strip()[:140]}"
            for no, line in enumerate(text.splitlines(), 1) if pattern.search(line)]


_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "msg", "bind"}
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _skipped_nodes(tree: ast.AST, tables: frozenset[str]) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in tables for t in targets):
                skip.update(id(sub) for sub in ast.walk(node))   # a code-only table, never rendered
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skip.add(id(node.value))   # docstrings and bare string expressions
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            is_log = name in _LOG_METHODS and owner in {"logger", "log", "logging", "_log", "_logger", "structlog"}
            is_argparse = name in {"add_argument", "ArgumentParser"}
            if is_log or is_argparse:
                skip.update(id(sub) for sub in ast.walk(node))
    return skip


def python_ui_strings(path: Path, tables: frozenset[str] = frozenset()) -> Iterator[tuple[int, str]]:
    """`tables`: module-level names whose values are code-only (an allowlist, a routing table) and are skipped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _skipped_nodes(tree, tables)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, _SQL_COMMENT.sub("", node.value)

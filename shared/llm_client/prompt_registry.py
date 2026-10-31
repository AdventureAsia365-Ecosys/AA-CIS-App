"""AA-900 — the prompt register: which registered version of a stage's prompt this code runs.

Two halves, one hash:

- **The manifest** ``shared/llm_client/prompt_versions.json`` (in the repository and the image): per stage, its
  prompt versions in order, each with the source file and the symbols that hold the prompt (module-level constants
  and functions, as listed in ``docs/llm/prompt-register.md``) and ``prompt_sha256``. The hash is ``prompt_hash_of``:
  sha256 over the exact source text of those symbols (read with ``ast``, never imported), so editing other code in
  the file does not change it, and editing the prompt does. The CI gate
  ``tests/unit/test_aa900_prompt_registry_gate.py`` (``scripts/prompt_registry_check.py``) fails when the newest
  version of a stage no longer matches its source: a prompt change needs a version bump
  (``scripts/prompt_registry_check.py --bump``).
- **The database register** ``shared.prompt_version`` (migration 321): (stage, version, prompt_sha256, source_path),
  written by migration only (``scripts/prompt_registry_check.py --sql`` prints the rows a migration inserts). Its rows
  say which versions were registered on this database.

At run time ``version_for(stage)`` = the version of the stage's newest manifest entry when its hash still matches the
deployed source AND the database holds the same (stage, version, hash); else None. The gateway writes it to
``llm_call_log.prompt_version`` with every attempt (``client._scope_for``). With no rows (Dev today) every call gets
None: nothing changes. The database rows are read once per ``CACHE_TTL_SECONDS`` per process; a missing table or a
database error gives None for ``FAILURE_TTL_SECONDS``. Never raises.
"""
from __future__ import annotations

import ast
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().with_name("prompt_versions.json")
CACHE_TTL_SECONDS = 600.0
FAILURE_TTL_SECONDS = 300.0
_SELECT_SQL = "SELECT stage, version, prompt_sha256 FROM shared.prompt_version"
_SEPARATOR = "\n\x00\n"

_rows: Optional[set] = None          # {(stage, version, sha256)}
_rows_at = 0.0
_failed_at = 0.0
_current: dict[str, Optional[tuple[str, str]]] = {}     # stage -> (version, sha) of the deployed source, or None
_lock = threading.Lock()


class PromptSourceError(ValueError):
    """A manifest entry names a file or a symbol that does not exist."""


def _symbol_sources(tree: ast.Module, text: str) -> dict[str, str]:
    """The exact source text of each module-level assignment, annotated assignment, function and class, by name."""
    out: dict[str, str] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        if names:
            segment = ast.get_source_segment(text, node) or ""
            for name in names:
                out[name] = segment
    return out


def prompt_hash_of(source_path: str, symbols: list[str], root: Path = REPO_ROOT) -> str:
    """sha256 hex of the source text of ``symbols`` in ``source_path`` (repository-relative), in the given order,
    joined by a NUL line. Raises PromptSourceError for a missing file or symbol."""
    path = (root / source_path).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise PromptSourceError(f"{source_path}: no such file in the repository")
    text = path.read_text(encoding="utf-8")
    sources = _symbol_sources(ast.parse(text, filename=source_path), text)
    missing = [s for s in symbols if s not in sources]
    if missing:
        raise PromptSourceError(f"{source_path}: no module-level symbol {', '.join(missing)}")
    return hashlib.sha256(_SEPARATOR.join(sources[s] for s in symbols).encode("utf-8")).hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, list[dict]]:
    """{stage: [entry, ...]} in version order (oldest first). An entry: version, source_path, symbols, prompt_sha256."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[dict]] = {}
    for entry in data.get("prompts", []):
        out.setdefault(entry["stage"], []).append(entry)
    return out


def newest(manifest: dict[str, list[dict]], stage: str) -> Optional[dict]:
    entries = manifest.get(stage) or []
    return entries[-1] if entries else None


def _deployed(stage: str) -> Optional[tuple[str, str]]:
    """(version, sha256) of the stage's newest manifest entry when the deployed source still matches it."""
    if stage in _current:
        return _current[stage]
    found: Optional[tuple[str, str]] = None
    try:
        entry = newest(load_manifest(), stage)
        if entry is not None and prompt_hash_of(entry["source_path"], entry["symbols"]) == entry["prompt_sha256"]:
            found = (entry["version"], entry["prompt_sha256"])
        elif entry is not None:
            logger.warning("llm_prompt_source_drift", stage=stage, version=entry["version"],
                           hint="the deployed prompt differs from the manifest: no prompt_version on the ledger")
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("llm_prompt_manifest_read_failed", stage=stage, error=str(e)[:200])
    _current[stage] = found
    return found


async def _fetch_rows() -> set:
    from . import connections
    conn = await connections._connect()
    try:
        return {(r["stage"], r["version"], r["prompt_sha256"]) for r in await conn.fetch(_SELECT_SQL)}
    finally:
        await conn.close()


def _load_rows_sync() -> set:
    from . import connections
    return connections.run_sync(_fetch_rows)


# Tests replace it (conftest: no database in unit tests).
loader = _load_rows_sync


def _registered() -> set:
    global _rows, _rows_at, _failed_at
    now = time.monotonic()
    if _rows is not None and now - _rows_at < CACHE_TTL_SECONDS:
        return _rows
    if _failed_at and now - _failed_at < FAILURE_TTL_SECONDS:
        return _rows or set()
    with _lock:
        try:
            _rows, _rows_at, _failed_at = set(loader()), time.monotonic(), 0.0
        except Exception as e:  # noqa: BLE001 — before migration 321, or DB down: no versions for a while
            _failed_at = time.monotonic()
            logger.info("llm_prompt_register_unreadable", error=str(e)[:200])
    return _rows or set()


def version_for(stage: Optional[str]) -> Optional[str]:
    """The registered prompt version the deployed code sends for ``stage`` (module docstring), else None."""
    if not stage:
        return None
    try:
        deployed = _deployed(stage)
        if deployed is None:
            return None
        version, sha = deployed
        return version if (stage, version, sha) in _registered() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_prompt_version_failed", stage=stage, error=str(e)[:200])
        return None


def invalidate() -> None:
    global _rows, _rows_at, _failed_at
    _rows, _rows_at, _failed_at = None, 0.0, 0.0
    _current.clear()

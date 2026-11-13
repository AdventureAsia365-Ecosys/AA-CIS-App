"""AA-900 — the prompt register: which registered version of a stage's prompt this code runs.

Two halves, one hash:

- **The manifest** ``shared/llm_client/prompt_versions.json`` (in the repository and the image): per stage, its
  prompt versions in order, each with the source file and the symbols that hold the prompt (module-level constants
  and functions, as listed in ``docs/llm/prompt-register.md``) and ``prompt_sha256``. The hash is ``prompt_hash_of``:
  sha256 over the exact source text of those symbols (read with ``ast``, never imported), so editing other code in
  the file does not change it, and editing the prompt does (AA-1047: a prompt whose source FILE reads the writing
  policy, ``reads_policy``, also hashes the compiled policy_sha, so a policy edit is a prompt edit). The CI gate
  ``tests/unit/test_aa900_prompt_registry_gate.py`` (``scripts/prompt_registry_check.py``) fails when the newest
  version of a stage no longer matches its source: a prompt change needs a version bump
  (``scripts/prompt_registry_check.py --bump``).
- **The database register** ``shared.prompt_version`` (migration 321): (stage, version, prompt_sha256, source_path),
  written by migration only (``scripts/prompt_registry_check.py --sql`` prints the rows a migration inserts). Its rows
  say which versions were registered on this database.

At run time ``version_for(stage)`` = the version of the stage's newest manifest entry when its hash still matches the
deployed source AND the database holds the same (stage, version, hash); else None. S279 L2: a writer stage's version
carries '.s<8 hex>' while a quality setting its prompt renders differs from its seed (``settings_suffix``). The gateway
writes it to
``llm_call_log.prompt_version`` with every attempt (``client._scope_for``). With no rows (Dev today) every call gets
None: nothing changes. The database rows are read once per ``CACHE_TTL_SECONDS`` per process; a missing table or a
database error gives None for ``FAILURE_TTL_SECONDS``. Never raises.
"""
from __future__ import annotations

import ast
import functools
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
    joined = _SEPARATOR.join(sources[s] for s in symbols)
    if POLICY_REF in joined or reads_policy(source_path, root) or symbols_read_policy(source_path, symbols, root):
        # AA-1047: a prompt rendered from the writing policy (`_wp.fill` / `_wp.fragment`, a policy band constant)
        # changes when the policy does, so the policy_sha is part of its hash (a policy edit needs a version bump
        # like a code edit).
        joined += _SEPARATOR + "writing_policy:" + _policy_sha(root)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


POLICY_REF = "_wp."
POLICY_MODULE = "services.quality.writing_policy"
POLICY_COMPILED = "services/quality/writing_policy.compiled.json"
_IMPORT_DEPTH = 3


def _module_file(module: Optional[str], root: Path) -> Optional[Path]:
    if not module:
        return None
    base = root / module.replace(".", "/")
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _module_names(path: Path, root: Path) -> tuple[dict[str, ast.AST], set[str], dict[str, tuple[str, str]]]:
    """(module-level definitions by name, local names bound to the writing policy, other imports
    {local: (module, name)}) of one file. Imports anywhere in the file count (a function-level import too).

    AA-1084: memoised on the file's text (``_parse_module_names``). One stage hash parses the same handful of files
    many times over (each name the policy walk follows re-reads its module), and the unit conftest clears the
    per-stage cache before every test: that re-parsing was most of the gateway unit tests' time. Callers only read
    the result; never mutate it."""
    return _parse_module_names(str(path), str(root), path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=512)
def _parse_module_names(
    path_str: str, root_str: str, text: str
) -> tuple[dict[str, ast.AST], set[str], dict[str, tuple[str, str]]]:
    path, root = Path(path_str), Path(root_str)
    tree = ast.parse(text, filename=path_str)
    defs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:  # `SEO_META_MIN, SEO_META_MAX = _wp.band("meta")` binds both names
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        defs[n.id] = node
    policy: set[str] = set()
    imports: dict[str, tuple[str, str]] = {}
    package = list(path.resolve().relative_to(root.resolve()).with_suffix("").parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == POLICY_MODULE:
                    policy.add(a.asname or a.name.split(".")[0])
                elif a.asname:  # `import a.b.c as x`: x.CONST reads module a.b.c (S272 review 2)
                    imports[a.asname] = (a.name, MODULE_ALIAS)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = ".".join(package[:-node.level] + ([node.module] if node.module else []))
            for a in node.names:
                local = a.asname or a.name
                if module == POLICY_MODULE or (module == "services.quality" and a.name == "writing_policy"):
                    policy.add(local)
                elif _module_file(f"{module}.{a.name}" if module else a.name, root) is not None:
                    # `from pkg import mod [as x]`: a module alias, x.CONST reads pkg.mod (S272 review 2)
                    imports[local] = (f"{module}.{a.name}" if module else a.name, MODULE_ALIAS)
                else:
                    imports[local] = (module, a.name)
    return defs, policy, imports


MODULE_ALIAS = "<module>"


def _attr_reads_policy(module: str, attr: str, root: Path, depth: int, seen: set) -> bool:
    """AA-1047 (S272 review 2): `m.ATTR` through a module alias (`import x.y as m` / `from x import y as m`) reads the
    writing policy when ATTR is the policy module itself or a module-level constant of x.y whose value reads it."""
    other = _module_file(module, root)
    if other is None or depth > _IMPORT_DEPTH:
        return False
    defs, policy, _imports = _module_names(other, root)
    if attr in policy:
        return True
    target = defs.get(attr)
    if target is None or isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False  # a helper's output is behaviour, not prompt text (as for a `from M import f`)
    return _name_reads_policy(other, attr, root, depth, seen)


def _name_reads_policy(path: Path, name: str, root: Path, depth: int, seen: set) -> bool:
    """True when the module-level `name` of `path` reaches the writing policy through the names its definition uses
    (same file), or through a CONSTANT it imports from another repository module (up to `_IMPORT_DEPTH` hops)."""
    defs, policy, imports = _module_names(path, root)
    stack = [name]
    while stack:
        n = stack.pop()
        if (path, n) in seen:
            continue
        seen.add((path, n))
        if n in policy:
            return True
        if n in defs:
            for x in ast.walk(defs[n]):
                if isinstance(x, ast.Name):
                    stack.append(x.id)
                elif (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                      and imports.get(x.value.id, ("", ""))[1] == MODULE_ALIAS and depth < _IMPORT_DEPTH
                      and _attr_reads_policy(imports[x.value.id][0], x.attr, root, depth + 1, seen)):
                    return True  # `smu.SEO_META_MAX` through a module alias (S272 review 2)
        elif n in imports and depth < _IMPORT_DEPTH:
            module, orig = imports[n]
            if orig == MODULE_ALIAS:
                continue  # the alias's attribute reads are checked above, one by one
            other = _module_file(module, root)
            if other is None:
                continue
            target = _module_names(other, root)[0].get(orig)
            if target is None or isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # a helper's output is behaviour, not prompt text; only imported constants are followed
            if _name_reads_policy(other, orig, root, depth + 1, seen):
                return True
    return False


def reads_policy(source_path: str, root: Path = REPO_ROOT) -> bool:
    """AA-1047 (S272 review): True when the prompt source FILE reads the writing policy: it imports the policy module
    (or a name from it), or it imports a constant whose value comes from the policy (SEO_META_MIN from
    seo_meta_utils, FAQ_MIN_PAIRS from quality_gates). File level on purpose: a stage's prompt is often rendered from
    module constants and helpers the manifest does not list (`_BACKGROUND` in s1_writer_v2, the S1 flag-fix meta rules
    inside flag_fix_node), so a test on the hashed symbols' text alone missed four stages. A policy edit then bumps
    every such stage, the honest over-approximation (scripts/prompt_registry_check.py --bump, one migration)."""
    path = (root / source_path).resolve()
    try:
        _defs, policy, imports = _module_names(path, root)
    except (OSError, SyntaxError, ValueError):
        return False
    if policy:
        return True
    seen: set = set()
    try:
        # S272 review 2: a module-level constant that reads a policy-derived constant through a module alias
        # (`P = f"... {smu.SEO_META_MAX} ..."`); a helper function reading one is checked per hashed symbol in
        # prompt_hash_of (symbols_read_policy), as a file-level rule would flag every file that merely calls it
        if any(not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               and _name_reads_policy(path, name, root, 0, seen) for name, node in _defs.items()):
            return True
    except (OSError, SyntaxError, ValueError):
        pass
    for module, orig in imports.values():
        other = _module_file(module, root)
        if other is None or other.resolve() == path or orig == MODULE_ALIAS:
            continue
        try:
            target = _module_names(other, root)[0].get(orig)
            if target is not None and not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and _name_reads_policy(other, orig, root, 1, seen):
                return True
        except (OSError, SyntaxError, ValueError):
            continue
    return False


def symbols_read_policy(source_path: str, symbols: list[str], root: Path = REPO_ROOT) -> bool:
    """True when one of the hashed `symbols` (or a same-file name it uses, transitively) reads the writing policy,
    a constant derived from it, or such a constant through a module alias (S272 review 2)."""
    path = (root / source_path).resolve()
    seen: set = set()
    try:
        return any(_name_reads_policy(path, sym, root, 0, seen) for sym in symbols)
    except (OSError, SyntaxError, ValueError):
        return False


def _policy_sha(root: Path) -> str:
    """The compiled writing policy's policy_sha (read as data, never imported)."""
    path = root / POLICY_COMPILED
    if not path.is_file():
        raise PromptSourceError(f"{POLICY_COMPILED}: missing (a prompt names the writing policy)")
    return str(json.loads(path.read_text(encoding="utf-8"))["policy_sha"])


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
        if (stage, version, sha) not in _registered():
            return None
        return version + settings_suffix(stage)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_prompt_version_failed", stage=stage, error=str(e)[:200])
        return None


# S279 lane L2 (AA-1096): the writer prompts render quality settings at call time (the blog length floor, the SEO bands,
# the pass line). The registered version names the code; the suffix names the values: '' while every value a prompt
# renders is its seed, else '.s<8 hex>' (quality_settings.prompt_fp), so llm_call_log.prompt_version tells a call that
# ran with an approved change from one that did not ('v8.s1a2b3c4d' fits the ledger CHECK ^[A-Za-z0-9._-]{1,40}$).
_WRITER_PREFIXES = ("s1_", "t2_", "t3_", "t8_", "t9_", "t10_")


def settings_suffix(stage: str) -> str:
    """'.s<fp>' for a writer stage while a prompt-rendered quality setting differs from its seed, else ''. Never
    raises."""
    if not stage.startswith(_WRITER_PREFIXES):
        return ""
    try:
        from services.quality import quality_settings
        fp = quality_settings.prompt_fp()
    except Exception as e:  # noqa: BLE001 — the version stays the registered one
        logger.warning("llm_prompt_settings_fp_failed", stage=stage, error=str(e)[:200])
        return ""
    return f".s{fp}" if fp else ""


def invalidate() -> None:
    global _rows, _rows_at, _failed_at
    _rows, _rows_at, _failed_at = None, 0.0, 0.0
    _current.clear()

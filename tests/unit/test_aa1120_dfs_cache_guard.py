"""AA-1120 — the guard: no DataForSEO purchase bypasses the research cache (AST, no DB, no network).

1. Every call of a paid DataForSEO method (``research_cache.PAID_CLIENT_METHODS`` on any receiver, and
   ``StandardQueue.volumes`` / ``serps`` on a receiver named like a queue) in api/, services/, shared/ and the tracked
   scripts/ sits in a function listed in ``research_cache.DFS_CALL_SITES``. A new call site fails until it is listed
   with the cache that covers it.
2. A listed "research_cache" site's top-level function reads the store (a read method or the cache's windows) and
   writes what it buys back (a write method) on a receiver named like a cache.
3. No listed site is stale (each still holds a paid call).
4. Every ``StandardQueue(...)`` passes ``cache=`` (``cache=None`` only where the registry says why).
5. Inside the queue, ``volumes`` / ``serps`` read the cache before their first post.
The module that defines the paid methods (dataforseo_client.py) and the queue itself (dfs_queue.py) are the
implementation, not call sites."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from services.seo_intelligence import research_cache as rc

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("api", "services", "shared", "scripts")
IMPLEMENTATION = {"services/seo_intelligence/dataforseo_client.py", "services/seo_intelligence/dfs_queue.py"}


def _tracked() -> list[str]:
    """The tracked Python files of the scanned trees (scripts/ is gitignored: an untracked local script is not code
    CI sees). Falls back to the files on disk when git is not available."""
    try:
        out = subprocess.run(["git", "ls-files", "--", *SCANNED], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
        files = [f for f in out if f.endswith(".py")]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return [p.relative_to(ROOT).as_posix() for top in SCANNED for p in (ROOT / top).rglob("*.py")
            if "__pycache__" not in p.parts]


def _receiver(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def paid_call(node: ast.AST) -> str | None:
    """The paid method name when ``node`` is a paid DataForSEO call, else None."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    if attr in rc.PAID_CLIENT_METHODS:
        return attr
    if attr in rc.PAID_QUEUE_METHODS and "queue" in _receiver(node.func.value).lower():
        return attr
    return None


def sites(tree: ast.AST) -> dict[str, tuple[ast.AST, list[int]]]:
    """qualname of the innermost enclosing function -> (its top-level function node, the lines of its paid calls)."""
    out: dict[str, tuple[ast.AST, list[int]]] = {}

    def walk(node: ast.AST, stack: list[str], top: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, stack + [child.name], top or (child if not isinstance(child, ast.ClassDef) else None))
                continue
            if paid_call(child) and stack:
                key = ".".join(stack)
                out.setdefault(key, (top or child, []))[1].append(child.lineno)
            walk(child, stack, top)
    walk(tree, [], None)
    return out


def uses_cache(fn: ast.AST) -> tuple[bool, bool]:
    """(reads, writes) of the research cache inside ``fn``: attribute uses on a receiver named like a cache."""
    reads = writes = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and "cache" in _receiver(node.value).lower():
            reads = reads or node.attr in rc.CACHE_READS
            writes = writes or node.attr in rc.CACHE_WRITES
    return reads, writes


def _scan() -> tuple[dict[str, tuple[ast.AST, list[int]]], list[str]]:
    found: dict[str, tuple[ast.AST, list[int]]] = {}
    no_cache_kw: list[str] = []
    for rel in _tracked():
        path = ROOT / rel
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        if not any(m in src for m in rc.PAID_CLIENT_METHODS | {"StandardQueue("}) and ".volumes(" not in src \
                and ".serps(" not in src:
            continue
        tree = ast.parse(src)
        if rel not in IMPLEMENTATION:
            for key, val in sites(tree).items():
                found[f"{rel}::{key}"] = val
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and _receiver(node.func) == "StandardQueue"
                    and not any(k.arg == "cache" for k in node.keywords)):
                no_cache_kw.append(f"{rel}:{node.lineno}")
    return found, no_cache_kw


def test_every_paid_dataforseo_call_site_is_registered_with_its_cache():
    found, _ = _scan()
    unlisted = sorted(f"{k} (line {v[1][0]})" for k, v in found.items() if k not in rc.DFS_CALL_SITES)
    assert not unlisted, ("a DataForSEO purchase outside the research cache registry: route it through "
                          f"services/seo_intelligence/research_cache.py or list it in DFS_CALL_SITES: {unlisted}")


def test_research_cache_sites_read_the_store_and_write_back():
    found, _ = _scan()
    bad = []
    for key, how in rc.DFS_CALL_SITES.items():
        if how != "research_cache" or key not in found:
            continue
        reads, writes = uses_cache(found[key][0])
        if not (reads and writes):
            bad.append(f"{key} reads={reads} writes={writes}")
    assert not bad, f"research call sites that bypass the research cache: {bad}"


def test_no_stale_registry_entry():
    found, _ = _scan()
    stale = sorted(k for k in rc.DFS_CALL_SITES if k not in found)
    assert not stale, f"DFS_CALL_SITES entries with no paid call left (remove them): {stale}"


def test_every_standard_queue_names_its_cache():
    _, missing = _scan()
    assert not missing, f"StandardQueue(...) without cache= (AA-1120): {missing}"


def test_the_queue_reads_the_cache_before_its_first_post():
    tree = ast.parse((ROOT / "services/seo_intelligence/dfs_queue.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "StandardQueue")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)}
    for name, reader in (("volumes", "_cached_volumes"), ("serps", "_serps_from_cache")):
        calls = [n for n in ast.walk(methods[name]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        read = min(n.lineno for n in calls if n.func.attr == reader)
        post = min(n.lineno for n in calls if n.func.attr == "_post")
        assert read < post, f"StandardQueue.{name} posts before it reads the research cache"


def test_the_guard_catches_a_bypass():
    tree = ast.parse(
        "async def leaky(client, queue, cache):\n"
        "    await client._serp_advanced('zarvok lake', 2840, 'en')\n"
        "    await queue.volumes(['zarvok lake'], 2840, 'en')\n"
        "    await cache.volumes(['zarvok lake'], 'US')\n"
        "async def cached(client, cache):\n"
        "    hits = await cache.volumes(['zarvok lake'], 'US')\n"
        "    data = await client.fetch_volumes_bulk(['melnith gorge'], 2840, 'en')\n"
        "    await cache.put_volumes('US', data)\n")
    found = sites(tree)
    assert sorted(found) == ["cached", "leaky"] and found["leaky"][1] == [2, 3]   # cache.volumes is not a purchase
    assert uses_cache(found["leaky"][0]) == (True, False)
    assert uses_cache(found["cached"][0]) == (True, True)

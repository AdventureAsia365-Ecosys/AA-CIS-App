"""AA-1055 PR 1 (S273 design section 2, "one buyer") — only the research job builds a DataForSEO client.

- Every `DataForSEOClient(...)` in app code (api/, services/, shared/, scripts/) sits in an allow-listed module: the
  segment research job (the one buyer) and the admin SERP top-up the design keeps. The two S1 buy paths
  (`process_seo`, the `s1_seo_prefetch` job) are listed as PENDING: PR 3 of the design makes S1 read the store
  only and deletes them from the list. A free balance read (`DataForSEOClient().fetch_balance()`) is allowed
  anywhere: it costs nothing.
- An allow-listed module that no longer builds a client fails the test, so the list only shrinks.
- The tenant paths (T2, T7 Slate + Debate, T8, T9, the `/v1/*` routers) never import the DataForSEO client module and
  never call a buying entry point.
AST only: nothing is imported or called.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("api", "services", "shared", "scripts")
CLIENT = "DataForSEOClient"
CLIENT_MODULE = "dataforseo_client"

# module -> why it may build a buying client
BUYERS = {
    "services/acp_contract/segment_research.py": "the segment research job: the one DataForSEO buyer",
    "services/acp_shared/question_topup.py": "the admin SERP top-up (scripts/aa779_serp_topup.py), kept by the design",
    "scripts/aa1055/dfs_queue_trial.py": "the one-off Standard queue trial (1 volume + 1 SERP task, approved S273)",
}
# S1 still buys until PR 3 of the design (S1 reads the store only); remove each entry in that PR.
S1_PENDING = {
    "services/seo_intelligence/handler.py": "process_seo live buy (S1), PR 3 puts it behind S1_SEO_LIVE_BUY or removes it",
    "services/jobs/s1_seo_prefetch_job.py": "s1_seo_prefetch job (S1), PR 3 turns it into a free store read",
}
ALLOWED = {**BUYERS, **S1_PENDING}

# The entry points that spend DataForSEO money (directly or through the research / top-up / S1 paths).
BUYING_CALLS = frozenset({"process_seo", "run_segment_research", "research_batch", "run_topup", "prefetch",
                          "fetch_all", "_serp_advanced"})


def _py_files(dirs=SCAN_DIRS):
    for d in dirs:
        for path in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_client(func: ast.AST) -> bool:
    return (isinstance(func, ast.Name) and func.id == CLIENT) or (
        isinstance(func, ast.Attribute) and func.attr == CLIENT)


def client_constructions(tree: ast.AST) -> list[int]:
    """Line numbers of every `DataForSEOClient(...)` call except a free `DataForSEOClient(...).fetch_balance()`."""
    free = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fetch_balance"
                and isinstance(node.func.value, ast.Call) and _is_client(node.func.value.func)):
            free.add(id(node.func.value))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_client(node.func) and id(node) not in free]


def _construction_sites() -> dict[str, list[int]]:
    sites = {}
    for path in _py_files():
        lines = client_constructions(ast.parse(path.read_text(), filename=str(path)))
        if lines:
            sites[_rel(path)] = lines
    return sites


def test_only_the_research_job_builds_a_dataforseo_client():
    sites = _construction_sites()
    assert sites, "no DataForSEOClient construction found: the scan is broken"
    outside = {path: lines for path, lines in sites.items() if path not in ALLOWED}
    assert not outside, (
        f"DataForSEOClient built outside the research job: {outside}. Read the store "
        "(acp_contract.search_demand) instead; only the segment research job buys (AA-1055 design section 2).")


def test_the_allow_list_only_shrinks():
    sites = _construction_sites()
    stale = sorted(path for path in ALLOWED if path not in sites)
    assert not stale, f"these modules no longer build a DataForSEOClient: delete them from the allow-list: {stale}"


def test_the_scan_sees_a_construction_and_skips_a_free_balance_read():
    tree = ast.parse("from x import DataForSEOClient\n"
                     "a = DataForSEOClient(budget=b)\n"
                     "money = await_(DataForSEOClient().fetch_balance())\n"
                     "c = mod.DataForSEOClient(tenant_id=t)\n")
    assert client_constructions(tree) == [2, 4]


def _tenant_path_files():
    paths = list((ROOT / "api" / "routers").glob("v1_*.py"))
    paths += list((ROOT / "services" / "acp_content_writing").rglob("*.py"))
    paths += [p for p in (ROOT / "services" / "acp_shared").glob("*.py") if p.name != "question_topup.py"]
    paths += [ROOT / "services" / "jobs" / name for name in
              ("t2_rewrite_job.py", "t7_slate_job.py", "t8_angles_job.py", "t9_write_job.py")]
    paths += list((ROOT / "services" / "content_generation").glob("t2_*.py"))
    return sorted(p for p in paths if "__pycache__" not in p.parts)


def _called_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_tenant_paths_never_import_the_client_or_call_a_buyer():
    files = _tenant_path_files()
    assert len(files) > 20, "tenant path list looks empty: the globs are broken"
    found = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and CLIENT_MODULE in (node.module or "").split("."):
                found.append(f"{_rel(path)}:{node.lineno} imports {node.module}")
            elif isinstance(node, ast.Import) and any(CLIENT_MODULE in a.name.split(".") for a in node.names):
                found.append(f"{_rel(path)}:{node.lineno} imports the client module")
            elif isinstance(node, ast.Call) and _called_name(node.func) in BUYING_CALLS:
                found.append(f"{_rel(path)}:{node.lineno} calls {_called_name(node.func)}")
    assert not found, f"a tenant path reaches a DataForSEO buyer (AA-646 / AA-1055): {found}"

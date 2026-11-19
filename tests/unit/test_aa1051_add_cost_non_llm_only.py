"""AA-1051 follow-up (S286) — guard: ``JobContext.add_cost`` takes NON-LLM spend only.

Since AA-1078 the runner stamps ``shared.job.cost_usd`` = the handler's ``add_cost`` total + the job's
``shared.llm_call_log`` sum (every gateway call inside a job is tagged with its id, ``call_log.bind_job``). A handler
that also passed its LLM spend to ``add_cost`` counted it twice: the W5 proofs (writing-memory R2 / shadow jobs) showed
exactly twice the ledger on the Jobs page.

An AST scan of the app finds every ``<x>.add_cost(<arg>)`` call and every ``add_cost=<arg>`` keyword and compares them
with the allow-list below: each entry is a reviewed non-LLM spend with its own ledger
(DataForSEO -> shared.dfs_call_log, Geoapify -> geo.geocode_call, Cost Explorer requests). A new site fails here until
it is reviewed and listed; an LLM result's cost (``out.cost_usd``, ``usage["cost_usd"]``, ``draft.cost_usd``, ...)
never belongs on the list.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("api", "services", "shared", "worker.py")
REGISTRY = "shared/jobs/registry.py"          # defines add_cost

# (repo path, the argument as ast.unparse prints it) -> what the spend is
ALLOWED_NON_LLM = {
    ("services/jobs/cost_explorer_refresh_job.py", "summary['est_cost_usd']"): "Cost Explorer API requests",
    ("services/jobs/place_geocode_job.py", "cost"): "Geoapify geocoding credits (geo.geocode_call)",
    ("services/jobs/place_relocate_job.py", "cost"): "Geoapify geocoding credits (geo.geocode_call)",
    ("services/jobs/place_shape_job.py", "cost"): "Geoapify place-details credits (geo.geocode_call)",
    ("services/jobs/s1_seo_prefetch_job.py", "budget.run_spent"): "DataForSEO (dfs budget, shared.dfs_call_log)",
    ("services/jobs/segment_research_job.py", "dfs_budget.run_spent"): "DataForSEO (shared.dfs_call_log)",
    ("services/jobs/tour_paa_job.py", "budget.run_spent"): "DataForSEO (research budget, shared.dfs_call_log)",
}

# What an LLM / embedding result's cost looks like at a call site: an attribute or key named cost_usd.
LLM_COST_RE = re.compile(r"(\.cost_usd\b|\[['\"]cost_usd['\"]\]|usage|llm|resp\b|draft\b)")


def _files():
    for entry in SCANNED:
        p = ROOT / entry
        yield from ([p] if p.is_file() else sorted(p.rglob("*.py")))


def add_cost_sites() -> set[tuple[str, str]]:
    sites = set()
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == REGISTRY or "/migrations/" in rel:
            continue
        src = path.read_text(encoding="utf-8")
        if "add_cost" not in src:
            continue
        for node in ast.walk(ast.parse(src, filename=rel)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "add_cost":
                arg = ast.unparse(node.args[0]) if node.args else "<none>"
                sites.add((rel, arg))
            for kw in node.keywords:
                if kw.arg == "add_cost":
                    sites.add((rel, f"add_cost={ast.unparse(kw.value)}"))
    return sites


def test_add_cost_carries_only_reviewed_non_llm_spend():
    sites = add_cost_sites()
    llm_like = sorted(s for s in sites if s not in ALLOWED_NON_LLM and LLM_COST_RE.search(s[1]))
    assert llm_like == [], (
        "LLM / embedding spend passed to JobContext.add_cost: the job's llm_call_log rows already count it, so the "
        f"Jobs page shows it twice (AA-1051 follow-up). Drop the add_cost (keep the budget check): {llm_like}")
    unknown = sorted(sites - set(ALLOWED_NON_LLM))
    assert unknown == [], (
        "new JobContext.add_cost site: add_cost is for NON-LLM spend only (DataForSEO, geocoding, Cost Explorer). "
        f"If it is, add it to ALLOWED_NON_LLM with what it pays for: {unknown}")


def test_allow_list_has_no_stale_entries():
    assert sorted(set(ALLOWED_NON_LLM) - add_cost_sites()) == [], "remove allow-list entries whose site is gone"


def test_the_scan_catches_the_double_counting_shapes(tmp_path, monkeypatch):
    bad = tmp_path / "services" / "jobs" / "fake_job.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("async def run(ctx):\n"
                   "    out = await go()\n"
                   "    ctx.add_cost(out.cost_usd)\n"
                   "    ctx.add_cost(usage['cost_usd'])\n"
                   "    ctx.add_cost(draft.cost_usd)\n"
                   "    await fill(add_cost=ctx.add_cost)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    sites = add_cost_sites()
    assert sites == {("services/jobs/fake_job.py", "out.cost_usd"), ("services/jobs/fake_job.py", "usage['cost_usd']"),
                     ("services/jobs/fake_job.py", "draft.cost_usd"),
                     ("services/jobs/fake_job.py", "add_cost=ctx.add_cost")}
    assert all(LLM_COST_RE.search(a) or a.startswith("add_cost=") for _, a in sites)


def test_add_cost_documents_the_contract():
    from shared.jobs.registry import JobContext
    doc = JobContext.add_cost.__doc__ or ""
    assert "NON-LLM" in doc and "llm_call_log" in doc

"""S283 — every module starts with a cold LLM gateway (tests/integration/_llm_cache_reset.py).

The nightly random order failed test_aa1002_migration_304 whenever test_aa1042_guide_db or test_aa1042_route_control_db
ran before it: their registry rows (acc1, CIS 406) stayed in ``connections._connections`` for the rest of the process.
"""
import os
import pkgutil
import re

import _llm_cache_reset

import shared.llm_client as pkg
from shared.llm_client import catalog, connections, deployments


def _with_cache_state():
    """The gateway modules that keep an in-process registry cache (a load timestamp or an ``invalidate()``)."""
    root = os.path.dirname(pkg.__file__)
    out = set()
    for info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        rel = info.name[len(pkg.__name__) + 1:].replace(".", os.sep)
        path = os.path.join(root, rel, "__init__.py") if info.ispkg else os.path.join(root, rel + ".py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if re.search(r"^(_loaded_at|_failed_at)\b|^def invalidate\b", src, re.M):
            out.add(info.name)
    return out


def test_every_gateway_cache_module_is_recorded():
    recorded = _llm_cache_reset.snapshot()
    modules = _with_cache_state()
    assert {"shared.llm_client.connections", "shared.llm_client.catalog", "shared.llm_client.deployments"} <= modules
    missing = {m for m in modules if not recorded.get(m)}
    assert not missing, f"not importable at conftest time, so never reset between modules: {sorted(missing)}"


def test_reset_drops_the_last_good_registry_rows():
    acc1 = connections.ProviderConnection(connection_key="acc1", kind="bedrock", auth_mode="assume_role",
                                          label="t", data_scope="aws_only", enabled=False)
    connections._connections = {"acc1": acc1}                # what a module reading its DB copy leaves behind
    connections._loaded_at = connections._failed_at = 1.0
    catalog._models["zz-s283"] = object()
    deployments._by_model["zz-s283"] = []
    try:
        _llm_cache_reset.reset()
        assert connections._connections == {} and connections._loaded_at == connections._failed_at == 0.0
        assert "zz-s283" not in catalog._models and "zz-s283" not in deployments._by_model
        assert set(connections.BUILTIN_CONNECTIONS) >= {"acc3", "acc2"}   # constants (UPPER_CASE) are not caches
    finally:
        _llm_cache_reset.reset()

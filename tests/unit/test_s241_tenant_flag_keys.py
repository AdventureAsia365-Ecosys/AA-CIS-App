"""S241 D-12 -- the tenant-pool flag keys the code reads are exactly the documented ones (20 after D5b, wave 3 and
wave 4, S247).

Under D-12 a missing Redis key means ON (tenant pool). So a key nobody has seen before -- a new route's flag,
a renamed constant, an ad hoc string at a call site -- runs on the tenant pool from its first deploy with no
"false" in Redis to roll it back and no entry in the flag tooling (root
docs/audits/2026-10-16-S240-aa859/aa859_flag.py ALLOWED) to set one. This test makes that change loud: it
scans the production code (api/, services/, shared/, worker/) for every key handed to the flag reader and
compares the set with DOCUMENTED_FLAG_KEYS below. Adding a key means editing this list, the module constant,
ALL_TENANT_POOL_FLAGS and aa859_flag.py ALLOWED in the same PR, and deciding whether to `SET <key> false`
on Dev before the deploy.
"""
import ast
import re
from pathlib import Path

from api.core import aa544_tenant_pool as tp

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("api", "services", "shared", "worker")
MODULE = ROOT / "api" / "core" / "aa544_tenant_pool.py"

# The documented tenant-pool flags (CONTEXT.md, Tenant DB role section; docs/implementation-notes/
# S241-tenant-pool-fail-closed.md). Literal values on purpose: renaming a key's VALUE is a new, missing key.
DOCUMENTED_FLAG_KEYS = frozenset({
    "aa544:stage1:publish_pending_pool",     # GET /v1/publish-log/pending
    "aa544:stage5:get_piece_pool",           # GET /v1/content-writing/pieces/{id} (+ /export)
    "aa544:publish_log_list_pool",           # GET /v1/publish-log
    "aa544:integration_disconnect_pool",     # DELETE /v1/integrations/{wordpress,facebook}
    "aa817:content_piece_reads_pool",        # GET /v1/content-writing/requests/{id}/latest-piece
    "aa859:piece_edit_pool",                 # W2 PATCH /v1/content-writing/pieces/{id}
    "aa859:t2_version_write_pool",           # W6 PATCH /v1/tours/versions/{id}
    "aa859:angle_gate_decide_pool",          # W3 POST /v1/angle-gate/requests/{id}/choose, /reopen
    "aa859:unpublish_pool",                  # W4 DELETE /v1/publish-log/{id}
    "aa859:t2_version_reads_pool",           # W5 GET /v1/tours/pool, /my-versions, /versions/{id}
    "aa859:integration_connect_pool",        # W9 GET/POST /v1/integrations/{wordpress,facebook}, POST .../test
    "aa859:t2_version_detail_reads_pool",    # D5b GET /v1/tours/{id}/full (tenant), /{id}, /versions/{id}/export-docx
    "aa859:brand_identity_pool",             # wave 3 GET / POST /admin/brand-identity (tenant Bearer)
    "aa859:publish_pool",                    # wave 3 POST /v1/publish-log/{piece_id}/publish
    "aa859:angle_gate_read_pool",            # wave 4 GET /v1/angle-gate/requests/{id} (+ choose/reopen re-read)
    "aa859:slate_reads_pool",                # wave 4 GET /v1/slate
    "aa859:subject_decide_pool",             # wave 4 POST /v1/subjects/{id}/pick, /cut
    "aa859:trip_reallocation_pool",          # wave 4 GET .../trip-reallocation/suggest, POST .../confirm
    "aa859:marketplace_pool",                # wave 4 GET /v1/marketplace
    "aa859:reviews_pool",                    # wave 4 GET /v1/content-writing/reviews, .../requests/{id}/review
})

# The flag reader's entry points and the position of their flag-key argument.
_READERS = {"acquire_scoped_conn": 2, "_flag_enabled": 1, "_read_flag": 1}
_KEY_SHAPE = re.compile(r"^aa\d+:[a-z0-9_:]+_pool$")


def _sources():
    for top in SCANNED_DIRS:
        base = ROOT / top
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _callee(node: ast.Call):
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _flag_arg(node: ast.Call, index: int):
    for kw in node.keywords:
        if kw.arg == "flag_key":
            return kw.value
    return node.args[index] if len(node.args) > index else None


def _resolve(arg):
    """A flag argument is a module *_FLAG constant (by name or attribute) or a string literal."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    name = arg.id if isinstance(arg, ast.Name) else arg.attr if isinstance(arg, ast.Attribute) else None
    if name and name.endswith("_FLAG") and isinstance(getattr(tp, name, None), str):
        return getattr(tp, name)
    return None


def _call_site_keys():
    keys, unresolved, sites = set(), [], 0
    for path in _sources():
        if path == MODULE:
            continue  # the reader's own plumbing passes its `flag_key` parameter through
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) in _READERS:
                sites += 1
                arg = _flag_arg(node, _READERS[_callee(node)])
                key = _resolve(arg) if arg is not None else None
                if key is None:
                    unresolved.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                else:
                    keys.add(key)
    return keys, unresolved, sites


def test_documented_list_matches_the_module_inventory():
    assert len(DOCUMENTED_FLAG_KEYS) == 20
    assert set(tp.ALL_TENANT_POOL_FLAGS) == DOCUMENTED_FLAG_KEYS


def test_every_flag_read_in_the_code_is_a_documented_key():
    keys, unresolved, sites = _call_site_keys()
    assert sites >= len(DOCUMENTED_FLAG_KEYS)  # the scan found the call sites at all
    assert not unresolved, (
        "flag key not resolvable to a *_FLAG constant of api/core/aa544_tenant_pool.py or a literal: "
        f"{unresolved}")
    assert keys == DOCUMENTED_FLAG_KEYS, (
        f"read but not documented: {sorted(keys - DOCUMENTED_FLAG_KEYS)}; "
        f"documented but never read: {sorted(DOCUMENTED_FLAG_KEYS - keys)}")


def test_no_other_flag_shaped_key_literal_in_production_code():
    """Catches a flag read some other way (a direct redis.get("aa9xx:..._pool")), outside the scan above."""
    stray = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and _KEY_SHAPE.match(node.value) and node.value not in DOCUMENTED_FLAG_KEYS):
                stray.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.value!r}")
    assert not stray, stray

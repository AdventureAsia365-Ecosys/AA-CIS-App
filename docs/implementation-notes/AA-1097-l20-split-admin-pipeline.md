# AA-1097 (lane l20, S279): split api/routers/admin_pipeline.py into a package

Part 4 of AA-1097. The ratchets (lane l15) baselined `api/routers/admin_pipeline.py` at 5,813 lines and 57 routes.
This change moves it into `api/routers/admin_pipeline/`, 13 parts of at most 753 lines, with no behaviour change.

## Decisions

- **Parts follow the file's own route groups, contiguous and in source order.** The module was already laid out by
  domain, so each part is a run of consecutive top-level statements:
  `_shared` (router, logger, shared helpers and request models), `_engine` (`_execute_run_tour` and its retries),
  `_revalidate` (`_revalidate_tour`), then the route parts `runs`, `uploads`, `tours`, `review`, `versions`,
  `tour_detail`, `metrics`, `brands`, `brand_identity`, `billing`. Parts with a leading underscore hold no routes.
- **One router, not sub-routers.** Every route part decorates `_shared.router` (prefix `/admin`, tag
  `admin-pipeline`, `staff_policy_if_staff`). FastAPI 0.161 keeps an included router as a nested entry rather than
  copying its routes, so sub-routers would change the served route tree. With one router the route table (paths,
  methods, names, order, tags, dependencies) and `docs/api/openapi.json` are byte-identical. `__init__.py` imports the
  route parts in the original order; a route part may import only helper parts or earlier route parts (an import of a
  later part would register its routes first). The split script checks that.
- **Two helpers moved once to `_shared`.** `_derive_field_failures` (used by `_build_failure_summary`) and
  `_record_review_reason` (used by run-tour-async and the review routes) sat among the review routes but are needed
  by earlier parts. They moved to `_shared` once; no code was duplicated.
- **One namespace for patches.** About 70 test files patch names on `api.routers.admin_pipeline` (for example
  `_resolve_brand_rule`, `verify_admin_secret`, `_boto3`, `_is_publishable`). The package re-exports every top-level
  name of every part, and its module class (`_OneNamespace`) passes a set or delete of a name on to every part that
  holds the name. A patch therefore reaches every caller, as it did when they shared one module's globals, and the
  tests needed no change. Services and scripts that import helpers lazily (`from api.routers.admin_pipeline import
  _resolve_brand_rule`) keep working.
- **Route endpoints report the package as their module.** `__init__.py` sets `endpoint.__module__` to
  `api.routers.admin_pipeline` (the public module, like numpy's `set_module`). The staff route policy snapshot
  (aa882) and the per-router sample test key routes by that name, so both stay unchanged.
- **Function-local relative imports** (`from .jobs_repo import get_job`) became `from ..jobs_repo` because the parts
  sit one package deeper. This is the only edit inside a function body.

## Changed

- Removed `api/routers/admin_pipeline.py`; added `api/routers/admin_pipeline/{__init__,_shared,_engine,_revalidate,
  runs,uploads,tours,review,versions,tour_detail,metrics,brands,brand_identity,billing}.py`.
- Path-keyed registries now name the part that holds the function: `services/quality/final_contract.py` (EXITS),
  `shared/llm_client/attribution.py` (BINDERS), `services/quality/thresholds.yaml` (`used_by`),
  `services/quality/lint.py` (the A1 code-map scan covers every part; `_review_block` is read from `review.py`).
- `services/quality/change_control.lock.json`: the gates sha moved because `thresholds.yaml` changed (only the
  `used_by` paths). Evidence: `parity:tests/unit/test_aa926_equivalence.py` (the gate-outcome snapshot is unchanged).
- Tests that read the file by path now read the part: `test_aa1052_lineage`, `test_aa1052_source_fanout`,
  `test_aa1058_cost_attribution`, `test_aa642_model_tier_allowlist`, `test_aa642_s0_dfs_cap`,
  `test_aa927_llm_call_site_registry`; `test_aa859_brand_identity_pool` scans every part of the package.
- `.github/code-health-baseline.json`: the 5,813-line file entry is gone (every part is under 800 lines); the seven
  over-limit functions keep their scores under their new paths (a move, not a raise).
- New `tests/unit/test_aa1097_admin_pipeline_package.py` pins the one router, the route count and order, the public
  module name, the re-exports and the patch propagation.

## Tradeoffs

- **Namespace propagation vs. rewriting ~150 patch targets.** Rewriting every test to patch the part where the name
  is looked up would be explicit, but each test would have to know which part calls which helper, and a later move
  between parts would break them again. The propagating module keeps the old single-namespace contract in one
  documented place. A follow-up can move tests to the part paths and then drop it.
- **No git rename.** No part is more than 13% of the old file, so git does not detect a rename; `git log --follow`
  stops at this commit. Use `git blame -C -C` (or the old path at the parent commit) for history.
- **Comments elsewhere** that say `admin_pipeline.py` (docstrings, audits, old implementation notes) were left as
  they are: they name the router, which still exists as a package.

## Should know

- The split was generated by a script that assigns top-level statements to parts by anchor names, so it can be re-run
  on a newer `admin_pipeline.py` (for example after another PR edits it on main): put the new file back, run the
  script, delete the file. The script lives outside the repo (lane scratch); the anchors are the first statement of
  each part listed above.
- Another open lane edits `admin_pipeline.py` (`_publish_floor()` helpers for the quality settings),
  `services/quality/thresholds.yaml` and the change-control lock. Whichever merges second redoes its edit on the
  other's result: after this PR, those helpers live in `_shared.py`, `_engine.py` and `review.py`.
- **Re-run on main (S279, after #1480 quality settings).** That lane merged first, so this branch merged
  `origin/main` and re-ran the split script on main's `admin_pipeline.py` (the script still reproduces the first split
  byte for byte from the old file, so the parts carry no hand edits). Main's edits land as they were: the
  `threshold_reader` floors (`_publish_floor()`, `_low_quality_floor()`, `_score_band_low()`) and the fixed histogram
  edges in `_shared.py`, `a1.auto_upgrade` in `_engine.py`, `_low_quality_floor()` in `review.py`. `thresholds.yaml`
  is main's with the `used_by` paths pointed at the part (`a1.auto_upgrade` -> `_engine.py`, the others ->
  `_shared.py`); the gates sha was re-accepted with the same parity evidence. Proofs repeated against main: route
  table identical except the same two brand-identity dependency `__module__` values, `docs/api/openapi.json`
  regenerated byte-identical to main, quality lint 0 errors and 25 warnings (file count only), the 113 unit test files
  that import or patch `admin_pipeline` (aa882 route policy, aa1035 role matrix included) pass, and
  `code_health.py check --base origin/main` is ok with the shrunk baseline.
- Proof of no behaviour change: the route table dump (568 routes, 57 in this router) is identical before and after
  except the `__module__` of two brand-identity dependency functions; `scripts/export_openapi.py` rewrites
  `docs/api/openapi.json` byte-identically (fastapi 0.161.1, pydantic 2.14.1); `python -m services.quality.lint
  --report` reports the same 0 errors and 25 warnings.

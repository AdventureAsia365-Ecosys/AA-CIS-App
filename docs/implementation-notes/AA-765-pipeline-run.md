# AA-765 (AA-799 follow-up) — remove the caller-less POST /v1/pipeline/run

The Linear comment on AA-765 (S244 master plan) moved the AA-799 follow-ups here: "check first what AA-882 and
AA-816 C9 already covered, then do only the rest". This is the separate, non-refactor part of AA-765; the judge
client / T11 dispatch refactor is its own branch (`refactor/aa-765-judge-client-consolidation`).

## Decisions

- AA-799 left two follow-ups (its closing comment, S235):
  1. role-scope the `/admin/review-queue/{id}/approve|reject` aliases — **already covered** by AA-882
     (`staff_policy_if_staff` router-wide on admin_pipeline, POLICY `REVIEW_ACT` for approve / reject / dismiss /
     supersede / claims label) and AA-816 C9 (`_require_admin` accepts a verified staff principal). Nothing to do.
  2. delete the caller-less `POST /v1/pipeline/run` — **done here**. No caller: the admin frontend never calls
     `/api/pipeline/run` (the BFF proxy forwards any path, but no page uses it), no script, no Lambda, no Terraform
     reference (Step Functions retired in AA-750). Uploads go through `/admin` (S0) and rewrites through the S1
     jobs. It was an LLM-spend entry point (up to 20 tour rewrites inside one request) outside the job runner.
- `_rewrite_tour` stays: `services/eval/regression.py` still uses it.
- `admin_pipeline.ENDPOINT_SERVICE_MAP` keeps its `/v1/pipeline/run` label: it names rows already stored in
  `shared.tenant_api_usage`, not a live route.

## Changed

- `api/routers/v1_pipeline.py`: route + its imports removed, module docstring rewritten.
- `api/core/staff_authz.py`: POLICY entry removed (it named a route that no longer exists; the route-policy snapshot test compares the two).
- Snapshots `aa882_staff_route_policy.json`, `aa899_untyped_public_routes.json` and `docs/api/openapi.json`: the
  path and its `Body_run_pipeline_v1_pipeline_run_post` schema removed. The OpenAPI file was edited as JSON and
  re-rendered with the export format (sorted keys, two-space indent); the local venv's fastapi/pydantic differ
  from the pins, so `scripts/export_openapi.py` cannot be run locally — CI's `test_committed_document_is_current`
  is the check.
- `tests/unit/test_aa799_pipeline_auth.py`: the "reaches the handler" test becomes "the route is gone"
  (404/405 with a valid admin secret and role).

## Tradeoffs

- Removing a route is a behaviour change, so it is not mixed into the pure-refactor branch.

## Should know

- Live check after deploy: `POST /v1/pipeline/run` from inside the api task with the admin secret → 404/405
  (from the internet `/v1/pipeline/*` already 404s at the edge, AA-799 note).

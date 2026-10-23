---
id: APP-05-cis
title: "AA-CIS API reference and breaking-change check"
description: Where the generated OpenAPI document of the CIS API lives, how to regenerate it, what the breaking-change check fails on and how to mark a PR as an intended breaking change.
type: how-to
status: draft
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-cis-api]
last_reviewed: 2026-10-24
review_every_days: 90
related: [APP-05, APP-07, ARCH-07]
---

# AA-CIS API reference and breaking-change check

`docs/api/openapi.json` is the OpenAPI document of the CIS API (`api.main`), generated from the code (AA-899). It is
the API reference of this repo and the base of the breaking-change check. Never edit it by hand.

## Regenerate it

After any change to a route, a request or response model, or the FastAPI / Pydantic pins:

```bash
python3 scripts/export_openapi.py
```

Commit the result in the same PR. The script needs no database, Redis, secret or AWS access: it sets the unit-test
dummy environment, points the AWS SDK at a closed local port with stub keys (so `init_sentry()` fails fast and no real
credential is used, even with a live MFA session), imports `api.main` and calls `app.openapi()`; the lifespan never
runs. The output is deterministic (sorted keys, no `servers`, `info.version` = `unreleased`, so a release PR never
makes it stale). FastAPI and Pydantic are pinned in `requirements.txt` because another version can change the output;
install `requirements.txt` first (the check prints the versions it ran with). A FastAPI or Pydantic bump is a PR that
also regenerates the file.

## The checks

| Check | Where | Fails when |
|---|---|---|
| Stale document | `Unit Tests` (required), `tests/unit/test_aa899_openapi_export.py` | the committed file differs from what the code generates |
| Untyped public route | same test file, `tests/unit/snapshots/aa899_untyped_public_routes.json` | a public route returns a 2xx JSON body with no schema and is not on the list, or a listed route got a model and is still listed |
| Breaking change | `API contract` / `OpenAPI breaking changes` (`.github/workflows/api-contract.yml`) | oasdiff finds an error-level change on a **public** path between the base branch's file and the PR's file |

**Public paths** are everything except `/admin/*`: the tenant API `/v1/*`, `/auth/*` (the Booking staff sign-in calls
`/auth/admin-login` and `/auth/booking-token`; Booking verifies the bridge token with `/auth/.well-known/jwks.json`) and
`/health*` (ALB, deploy smoke). `/admin/*` is called only by this repo's admin console through its BFF, which changes in
the same PR: its breaking changes are listed as warnings and in the job summary, never failing. Every change is listed
in the job summary (oasdiff changelog).

**What counts as breaking** is oasdiff's error level (pinned version in the workflow), with two checks raised to error
in `.github/oasdiff/severity-levels.txt` (`response-optional-property-removed`, `request-parameter-removed`). In
short: a removed path or operation, a removed response field (required or optional), a new required request
parameter or field, a removed request parameter, a narrowed type, enum or limit on a request, a widened response
enum, a response field that became optional or nullable, a removed success status or media type. Adding an optional
field, a new path or a new operation is not breaking.

**Response models.** oasdiff sees a removed response field only when the route declares its model:
`response_model=` (FastAPI then validates and serializes the return value: declare every field it returns, nullable
where it can be null), or `responses={code: {"model": ...}}` for a handler that returns its own `JSONResponse`
(documentation only). Public models live in `api/schemas/public_api.py`. The 58 `/v1/*` operations still returning a
bare dict are on the untyped list; type them route by route (remove the line from the snapshot in the same PR). Until
a route is typed, removing one of its fields is not caught.

## Making a breaking change on purpose

Prefer an additive change (keep the old field, add the new one, deprecate, remove later; root handbook §5.6). When the
break is intended, mark the PR in either way:

- the label **`api-breaking`** (adding or removing the label re-runs the check by itself), or
- a conventional breaking title, `feat!: ...` / `fix!: ...` (release-please then records it as a breaking change).

The check then passes with a warning per breaking change. Put the client plan in the PR body: who calls the path,
what they change and when (for a cross-app contract, a new major per root `docs/contracts/README.md`).

## Releases

Each GitHub release carries `aa-cis-openapi-vX.Y.Z.json`: the document of the released commit with `info.version` set
to the release version (`.github/workflows/release-please.yml`).

## Local run of the breaking check

```bash
git show origin/main:docs/api/openapi.json > /tmp/base-openapi.json
BASE_SPEC=/tmp/base-openapi.json HEAD_SPEC=docs/api/openapi.json INTERNAL_PATHS='^/admin/' SEVERITY_LEVELS=.github/oasdiff/severity-levels.txt bash .github/scripts/openapi_breaking.sh
```

It needs `oasdiff` on the PATH (the version and checksum the workflow pins).
`.github/scripts/openapi_breaking.sh` is a byte copy of the AA-Booking script: change both together.

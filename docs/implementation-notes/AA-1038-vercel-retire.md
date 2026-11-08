# AA-1038 — R5 hosting, final step: CIS after the TripPlanner Vercel retirement

Root runbook `docs/ops/runbooks/tripplanner-web-ecs-cutover.md` section 4 step 2. The TripPlanner site
(`tripplanner.lumiguides.it.com`) is served by the ECS service behind CloudFront since the S272 cut-over; the Vercel
project `aa-tripplanner` is deleted at this step.

## Decisions

- **Purge default = the public host:** `services/jobs/tripplanner_purge_job.py` `DEFAULT_URL` is
  `https://tripplanner.lumiguides.it.com/api/revalidate/catalog` (the route `frontend/app/api/revalidate/catalog/route.ts`
  of AA-TripPlanner-Web, `POST`). Infra keeps setting `TRIPPLANNER_REVALIDATE_URL` to the same value (its zone), so
  the default only matters for a task started without the env; the explicit "" off switch is unchanged.
- **The retired Vercel host leaves the sales-link allow list:** `services/acp_shared/subject_gate.py`
  `DEFAULT_TRIPPLANNER_HOSTS` drops `aa-tripplanner.vercel.app`. Not asked by the runbook, but a deleted
  `*.vercel.app` project name can be claimed by anyone, and the subject gate would have allowed a tenant post's
  sales link to that host. The tests use an invented host (`planner.example`) for the generic case.
- **Overview card link removed:** the admin overview "TripPlanner customers" card linked Vercel Web Analytics of the
  `aa-tripplanner` project (a dead link once the project is deleted). The link and its two messages (EN/VI) are
  removed; the card's own planner counters stay. No replacement link: CloudFront has no free page-visit view that the
  console may open without AWS console access.

## Changed

- `services/jobs/tripplanner_purge_job.py` (default URL, docstring), `services/acp_shared/subject_gate.py` (hosts),
  `frontend/app/admin/overview/_components/TripPlannerCard.tsx` + `ops.messages.ts` (link removed), `CONTEXT.md`.
- Tests: `tests/unit/test_aa987_tripplanner_purge.py` (default pinned to the public host, never Vercel),
  `tests/unit/test_aa1039_subject_gate_and_fanout.py` (default hosts, the retired Vercel host refused).

## Tradeoffs

- Hard-coding the Dev host as the default mirrors the old default (also a Dev host); prod sets the env from Infra.

## Should know

- No job kind, route, migration or openapi change. Deploy needs no job-table wait beyond the normal CIS gate.
- Live check after deploy: the next `tripplanner_purge` job (admin job runner) succeeds with HTTP 200 against the
  public host; the admin overview TripPlanner card shows no "Page visits" link.

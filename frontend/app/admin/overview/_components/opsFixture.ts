// app/admin/overview/_components/opsFixture.ts
// AA-858 — a typed sample of GET /api/admin/overview/ops for local UI work and screenshots without AWS or the
// Booking/TripPlanner views. Not imported by the page. Because it is typed as OpsOverview, `tsc` also checks that
// the UI types still describe the backend shape documented in api/services/ops_overview/. Cost rows reuse the
// real 1–19/10/2026 App-tag split measured during planning (USD).
import type { AppHealth, OpsOverview } from "./opsTypes";

const START = Date.UTC(2026, 9, 18, 13);

function hourly(base: number, errorsAt: number[] = []): AppHealth["hourly"] {
  return Array.from({ length: 24 }, (_, i) => ({
    hour: new Date(START + i * 3_600_000).toISOString().replace(".000Z", "+00:00"),
    requests: Math.max(0, Math.round(base * (0.4 + Math.abs(Math.sin(i / 3))))),
    errors: errorsAt.includes(i) ? 1 : 0,
  }));
}

function days(n: number, f: (i: number) => number) {
  return Array.from({ length: n }, (_, i) => f(i));
}

export const OPS_FIXTURE: OpsOverview = {
  generated_at: "2026-10-19T12:34:00+00:00",
  health: {
    status: "ok",
    as_of: "2026-10-19T12:34:00+00:00",
    window_hours: 24,
    window_start: "2026-10-18T13:00:00+00:00",
    window_end: "2026-10-19T13:00:00+00:00",
    environment: "dev",
    apps: [
      {
        app: "cis", label: "CIS", kind: "apigw", resources: ["aa-cis-dev-api"], has_data: true, partial: false,
        requests: 512, errors: 2, error_rate_pct: 0.39, client_errors: 31, throttles: null, p95_ms: 412.3,
        hourly: hourly(21, [5, 17]), alarms: { total: 24, no_data: 1, firing: [] }, state: "ok",
        state_reason: "Working normally",
      },
      {
        app: "tripplanner", label: "TripPlanner", kind: "lambda",
        resources: ["aa-tripplanner-dev-browse", "aa-tripplanner-dev-assembly"], has_data: true, partial: false,
        requests: 188, errors: 3, error_rate_pct: 1.6, client_errors: null, throttles: 0, p95_ms: 1840,
        hourly: hourly(8, [3, 4, 20]), alarms: { total: 4, no_data: 0, firing: [] }, state: "warn",
        state_reason: "1.6% of requests failed with a server error",
      },
      {
        app: "booking", label: "Booking", kind: "apigw", resources: ["aa-booking-dev-api"], has_data: true,
        partial: false, requests: 64, errors: 0, error_rate_pct: 0, client_errors: 4, throttles: null, p95_ms: 96,
        hourly: hourly(3), state: "alarm", state_reason: "1 alarm firing",
        alarms: {
          total: 5, no_data: 0,
          firing: [{
            name: "aa-booking-dev-no-healthy-task",
            description: "AA-Booking ECS service has no healthy task behind the load balancer.",
            since: "2026-10-19T11:58:00+00:00",
          }],
        },
      },
    ],
    shared_alarms: { total: 6, no_data: 0, firing: [] },
    alarms_error: null,
  },
  tripplanner: {
    status: "ok",
    as_of: "2026-10-19T12:34:00+00:00",
    days: days(14, (i) => i).map((i) => ({
      day: new Date(Date.UTC(2026, 9, 6 + i)).toISOString().slice(0, 10),
      planner_session_started: 6 + ((i * 7) % 9),
      trip_started: 2 + ((i * 5) % 4),
      trip_edited: 1 + (i % 3),
      inquiry_sent: i % 4 === 0 ? 1 : 0,
      ai_search: 10 + ((i * 3) % 7),
    })),
    totals_7d: { planner_session_started: 71, trip_started: 24, trip_edited: 14, inquiry_sent: 2, ai_search: 92 },
    prev_7d: { planner_session_started: 58, trip_started: 26, trip_edited: 13, inquiry_sent: 2, ai_search: 80 },
    change_pct: { planner_session_started: 22.4, trip_started: -7.7, trip_edited: 7.7, inquiry_sent: 0, ai_search: 15 },
    notes: { ai_search: "Repeat searches answered from the cache are not counted." },
  },
  booking: {
    status: "ok",
    as_of: "2026-10-19T12:34:00+00:00",
    weeks: days(8, (i) => i).map((i) => ({
      week_start: new Date(Date.UTC(2026, 7, 31 + i * 7)).toISOString().slice(0, 10),
      inquiry_received: 2 + (i % 3),
      trip_case_created: 2 + (i % 2),
      proposal_sent: i % 2,
      confirmed: i === 5 ? 1 : 0,
      cancelled: i === 3 ? 1 : 0,
    })),
    totals: { inquiry_received: 23, trip_case_created: 20, proposal_sent: 4, confirmed: 1, cancelled: 1 },
    conversion_pct: { inquiry_to_case: 87, case_to_proposal: 20, proposal_to_confirmed: 25 },
    cases_by_status: [
      { status: "new_inquiry", n: 9, closed: false },
      { status: "discovery", n: 4, closed: false },
      { status: "building", n: 3, closed: false },
      { status: "proposal_sent", n: 2, closed: false },
      { status: "confirmed", n: 1, closed: false },
      { status: "completed", n: 0, closed: true },
      { status: "cancelled", n: 1, closed: true },
    ],
    open_cases: 19,
    inbox_attention: { parked: 1, failed: 0 },
  },
  cost: {
    status: "ok",
    month: "2026-10",
    prev_month: "2026-09",
    period_start: "2026-10-01",
    period_end_exclusive: "2026-10-20",
    has_aws_data: true,
    aws_fetched_at: "2026-10-19T03:00:00+00:00",
    aws_covered_to: "2026-10-18",
    rows: [
      { key: "cis", label: "CIS (content + catalog)", kind: "app", hint: null, aws_usd: 6.57, ai_outside_aws_usd: 18.42, total_usd: 24.99, prev_total_usd: 31.2, top_services: [] },
      { key: "booking", label: "Booking", kind: "app", hint: null, aws_usd: 1.08, ai_outside_aws_usd: 0, total_usd: 1.08, prev_total_usd: 0, top_services: [] },
      { key: "tripplanner", label: "TripPlanner", kind: "app", hint: null, aws_usd: 0.16, ai_outside_aws_usd: 0.31, total_usd: 0.47, prev_total_usd: 0.12, top_services: [] },
      {
        key: "tag:shared", label: "Shared platform", kind: "shared_tag",
        hint: "AWS resources several apps use together (App tag 'shared').", aws_usd: 9.68, ai_outside_aws_usd: 0,
        total_usd: 9.68, prev_total_usd: 0, top_services: [{ service: "Amazon Virtual Private Cloud", amount_usd: 9.02 }],
      },
      {
        key: "untagged", label: "Not assigned to an app", kind: "untagged",
        hint: "AWS charges with no App tag: tax, support, data transfer and resources tagged before 16/10/2026. Per-app numbers are reliable from 16/10/2026.",
        aws_usd: 83.72, ai_outside_aws_usd: 0, total_usd: 83.72, prev_total_usd: 120.4,
        top_services: [
          { service: "Amazon Relational Database Service", amount_usd: 28.11 },
          { service: "Amazon Elastic Container Service", amount_usd: 19.4 },
          { service: "EC2 - Other", amount_usd: 12.86 },
          { service: "Amazon ElastiCache", amount_usd: 8.02 },
          { service: "Tax", amount_usd: 7.61 },
        ],
      },
    ],
    totals: { aws_usd: 101.21, ai_outside_aws_usd: 18.73, total_usd: 119.94, prev_total_usd: 151.72 },
    refresh: { attempted: false, ok: null, stale: false, reason: "fresh" },
    notes: [
      "AWS numbers come from the AWS bill and lag about one day.",
      "AI usage is an estimate from our own call log; most AI runs on other AWS accounts and providers, so it is not part of this AWS bill.",
    ],
  },
  sources: {
    health: "AWS CloudWatch (acc2, us-west-1)",
    tripplanner: "tripplanner.v_ops_traffic_daily (TripPlanner contract view, tp-015) and shared.llm_call_log stage tp_search_embed",
    booking: "booking.v_ops_funnel_daily and booking.v_ops_case_status (Booking contract views, bk-017)",
    cost: "AWS Cost Explorer by App tag (acc2) and LLM gateway estimates, via the Platform cost module",
  },
  _cached: false,
};

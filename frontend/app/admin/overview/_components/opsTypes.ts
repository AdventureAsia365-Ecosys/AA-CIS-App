// app/admin/overview/_components/opsTypes.ts
// AA-858 — response of GET /api/admin/overview/ops (backend api/services/ops_overview/). Every section is
// either { status: "ok", ... } or { status: "unavailable", reason, message } — the card then shows `message`.
import type { AppHealthVerdict } from "../../../../lib/platform";

export interface Unavailable {
  status: "unavailable";
  reason: string;
  message: string;
  _cached?: boolean;
}

export type Section<T> = (T & { status: "ok"; _cached?: boolean; _stale?: boolean }) | Unavailable;

export type HealthState = "ok" | "warn" | "alarm" | "unknown";

export interface AlarmBucket {
  total: number;
  no_data: number;
  firing: { name: string; description: string; since: string | null }[];
}

export interface AppHealth {
  app: "cis" | "tripplanner" | "booking";
  label: string;
  kind: "apigw" | "lambda";
  resources: string[];
  has_data: boolean;
  partial: boolean;
  requests: number;
  errors: number;
  error_rate_pct: number | null;
  client_errors: number | null;
  throttles: number | null;
  p95_ms: number | null;
  hourly: { hour: string; requests: number; errors: number }[];
  alarms: AlarmBucket | null;
  state: HealthState;
  state_reason: string;
  /** S243 — the app's one status, the same verdict /admin/platform/apps shows (backend app_health). */
  overall?: AppHealthVerdict;
}

export interface HealthData {
  as_of: string;
  window_hours: number;
  window_start: string;
  window_end: string;
  environment: string;
  apps: AppHealth[];
  shared_alarms: AlarmBucket | null;
  alarms_error: string | null;
}

export type TpMetric = "planner_session_started" | "trip_started" | "trip_edited" | "inquiry_sent" | "ai_search";

export interface TripPlannerData {
  as_of: string;
  days: ({ day: string } & Record<TpMetric, number>)[];
  totals_7d: Record<TpMetric, number>;
  prev_7d: Record<TpMetric, number> | null;
  change_pct: Record<TpMetric, number | null> | null;
  notes: Record<string, string>;
}

export type BkMetric = "inquiry_received" | "trip_case_created" | "proposal_sent" | "confirmed" | "cancelled";

export interface BookingData {
  as_of: string;
  weeks: ({ week_start: string } & Record<BkMetric, number>)[];
  totals: Record<BkMetric, number>;
  conversion_pct: {
    inquiry_to_case: number | null;
    case_to_proposal: number | null;
    proposal_to_confirmed: number | null;
  };
  cases_by_status: { status: string; n: number; closed: boolean }[];
  open_cases: number;
  inbox_attention: { parked: number; failed: number };
}

export interface CostRow {
  key: string;
  label: string;
  kind: "app" | "llm_app" | "shared_tag" | "other_tag" | "untagged";
  hint: string | null;
  aws_usd: number;
  ai_outside_aws_usd: number;
  total_usd: number;
  prev_total_usd: number | null;
  top_services: { service: string; amount_usd: number }[];
}

export interface CostData {
  month: string;
  prev_month: string;
  period_start: string;
  period_end_exclusive: string;
  has_aws_data: boolean;
  aws_fetched_at: string | null;
  aws_covered_to: string | null;
  rows: CostRow[];
  totals: {
    aws_usd: number;
    ai_outside_aws_usd: number;
    /** gross AI estimate and the part of it already on the AWS bill (S243: shown so the numbers add up) */
    ai_usd?: number;
    ai_in_aws_usd?: number;
    total_usd: number;
    prev_total_usd: number | null;
  };
  refresh: { attempted: boolean | null; ok: boolean | null; stale: boolean | null; reason: string | null };
  notes: string[];
}

export interface OpsOverview {
  generated_at: string;
  health: Section<HealthData>;
  tripplanner: Section<TripPlannerData>;
  booking: Section<BookingData>;
  cost: Section<CostData>;
  /** Engineering detail of each section's source (tables, metrics). Not shown: the page names sources in plain
   * words from ./sources.ts (AA-858, S243). */
  sources: Record<"health" | "tripplanner" | "booking" | "cost", string>;
  _cached?: boolean;
}

// app/admin/overview/_components/sources.ts
// AA-858 — the one place that says where every number on /admin/overview comes from (shown in its SourceHint).
// Plain business words + the system the number is read from, how far back it looks and how often it refreshes.
// No table, view or column names, migration numbers, account names or internal codes (S243 finding 11 rule; guarded
// by tests/unit/test_aa858_overview_sources.py). The engineering detail (tables, metrics) stays in CONTEXT.md.

import { AI_ON_BILL_SRC, AI_OUTSIDE_LABEL, AWS_BILL_LABEL } from "../../../../lib/costWording";
import type { AppHealth } from "./opsTypes";

// The ops cards (health, TripPlanner, Booking, cost) are not polled by the page (AWS-backed, cached on the server):
// the server renews them on a timer, the screen shows the new numbers after Refresh or a reload.
const OPS_5MIN = "updated on the server every 5 minutes (press Refresh to load the latest)";

// ── Apps health (AWS CloudWatch) ──────────────────────────────────────────────────────────────────────────────────
const HEALTH_REFRESH = `last 24 hours, ${OPS_5MIN}`;

/** What CloudWatch measures for each app, in plain words. */
const APP_SUBJECT: Record<AppHealth["app"], string> = {
  cis: "CIS API",
  booking: "Booking API",
  tripplanner: "TripPlanner functions (browse and trip assembly)",
};

/** What counts as one request and as one error: an HTTP request for the APIs, a function run for TripPlanner. */
const isTp = (app: AppHealth["app"]) => app === "tripplanner";
const ERRORS = (app: AppHealth["app"]) => (isTp(app) ? "runs that failed" : "server errors (5xx)");

export const HEALTH_SRC = {
  card:
    "AWS CloudWatch: requests (function runs for TripPlanner), errors and response time of each app and the state " +
    `of its alarms, ${HEALTH_REFRESH}`,
  requests: (app: AppHealth["app"]) =>
    `AWS CloudWatch, ${APP_SUBJECT[app]} ${isTp(app) ? "runs" : "requests"}, ${HEALTH_REFRESH}`,
  errors: (app: AppHealth["app"]) =>
    `AWS CloudWatch, ${APP_SUBJECT[app]} ${ERRORS(app)}, ${HEALTH_REFRESH}; the % is errors divided by ` +
    (isTp(app) ? "runs" : "requests"),
  speed: (app: AppHealth["app"]) =>
    app === "tripplanner"
      ? `AWS CloudWatch, ${APP_SUBJECT[app]} run time: 95% of runs finished within this time (the slower of the two ` +
        `functions), ${HEALTH_REFRESH}`
      : `AWS CloudWatch, ${APP_SUBJECT[app]} response time: 95% of requests were answered within this time, ` +
        HEALTH_REFRESH,
  clientErrors: (app: AppHealth["app"]) =>
    `AWS CloudWatch, ${APP_SUBJECT[app]} requests refused for bad input or no sign-in (4xx), ${HEALTH_REFRESH}; ` +
    "not a fault of the app",
  throttles: (app: AppHealth["app"]) =>
    `AWS CloudWatch, ${APP_SUBJECT[app]} runs refused because too many ran at once, ${HEALTH_REFRESH}`,
  hourly: (app: AppHealth["app"]) =>
    `AWS CloudWatch, ${APP_SUBJECT[app]} ${isTp(app) ? "runs" : "requests"} per hour ` +
    `(${isTp(app) ? "failed runs" : "server errors"} in red), ${HEALTH_REFRESH}`,
  alarms: (app: AppHealth["app"]) =>
    `AWS CloudWatch alarms set up for ${APP_SUBJECT[app]}, their state now, ${OPS_5MIN}`,
  shared:
    `AWS CloudWatch alarms of the shared services (network, staff sign-in, e-mail), their state now, ${OPS_5MIN}`,
};

// ── TripPlanner customers ─────────────────────────────────────────────────────────────────────────────────────────
const TP_WINDOW = `last 7 days (UTC days), ${OPS_5MIN}; the change compares with the 7 days before`;

export const TP_SRC = {
  card: `TripPlanner database (daily activity counts, no traveller details) and the AI calls log, ${OPS_5MIN}`,
  planner_session_started: `TripPlanner database, new guest planner sessions, ${TP_WINDOW}`,
  trip_started: `TripPlanner database, new trip drafts, ${TP_WINDOW}`,
  inquiry_sent: `TripPlanner database, "send to advisor" requests handed to Booking, ${TP_WINDOW}`,
  ai_search:
    `AI calls log, TripPlanner searches that called the AI, ${TP_WINDOW}; repeat searches answered from the cache ` +
    "are not counted",
  chart: `TripPlanner database, the same daily counts for the last 14 days (UTC days), ${OPS_5MIN}`,
};

// ── Booking activity ──────────────────────────────────────────────────────────────────────────────────────────────
const BK_8W = `total of the last 8 weeks, ${OPS_5MIN}`;
const BK_WEEK = "in each week (Monday to Sunday, UTC)";

export const BOOKING_SRC = {
  card: `Booking database (daily counts and current trip case statuses, no customer details), ${OPS_5MIN}`,
  steps: {
    inquiry_received: `Booking database, inquiries received from TripPlanner and the website, ${BK_8W}`,
    trip_case_created: `Booking database, trip cases opened (deleted ones left out), ${BK_8W}`,
    proposal_sent: `Booking database, trip cases that reached "proposal sent" for the first time, ${BK_8W}`,
    confirmed: `Booking database, trip cases that reached "confirmed" for the first time, ${BK_8W}`,
  },
  weeks: {
    inquiry_received: `Booking database, inquiries received ${BK_WEEK}`,
    trip_case_created: `Booking database, trip cases opened ${BK_WEEK}`,
    proposal_sent: `Booking database, trip cases that reached "proposal sent" for the first time ${BK_WEEK}`,
    confirmed: `Booking database, trip cases that reached "confirmed" for the first time ${BK_WEEK}`,
  },
  cancelled: `Booking database, trip cases cancelled for the first time ${BK_WEEK}`,
  attention: "Booking database, inquiries in the Booking inbox right now that are waiting or failed",
  openCases:
    "Booking database, trip cases by their status right now (deleted ones left out); the chips list every status " +
    "that has cases, Completed and Cancelled are closed and shown greyed",
};

// ── Cost per app ──────────────────────────────────────────────────────────────────────────────────────────────────
export const COST_SRC = {
  card:
    "AWS bill via Cost Explorer, split by each resource's App tag, month to date (fetched about once a day, the bill " +
    "itself lags about two days), plus the AI calls log for AI billed outside the AWS bill (updated on the server " +
    "every 15 minutes, press Refresh to load the latest); the same numbers as Cost per app",
  total: `${AWS_BILL_LABEL} + ${AI_OUTSIDE_LABEL}, for the same app and month`,
  prevMonth: (month: string) => `The same sources for the whole of ${month}`,
  aiNote: `AI calls log, this month: the whole AI estimate. ${AI_ON_BILL_SRC}`,
};

// ── CIS content & operations ──────────────────────────────────────────────────────────────────────────────────────
const CIS_DB = "CIS database";
const LIVE = "counted live, refreshed every minute";

export const PIPELINE_SRC = {
  card: `${CIS_DB}, every tour per country through the content pipeline, ${LIVE}`,
  raw: `${CIS_DB}, imported tours still active at the source; +n/7d = imported in the last 7 days`,
  generated: `${CIS_DB}, tours with at least one rewritten draft; +n/7d = tours with a new draft in the last 7 days`,
  review: `${CIS_DB}, tours waiting in the Review Queue right now`,
  published: `${CIS_DB}, tours published to Master Content (trashed ones left out); +n/7d = published in the last 7 days`,
  atomized: `${CIS_DB}, published tours split into active content pieces (atoms)`,
};

export const INTEL_SRC = {
  atoms: `${CIS_DB}, content pieces (atoms) cut from tours, deleted and empty ones left out, ${LIVE}`,
  segments: `${CIS_DB}, segments that hold at least one of those atoms, ${LIVE}`,
  routes: `${CIS_DB}, current routes (replaced versions left out), ${LIVE}`,
  hubs: `${CIS_DB}, hubs with at least one current route, ${LIVE}`,
  scores: `${CIS_DB}, current atom ranking scores (replaced ones left out), ${LIVE}`,
  research: `${CIS_DB}, places researched for segments; stale = at least one market last researched more than 30 days ago, ${LIVE}`,
};

export const TENANT_SRC = {
  active: `${CIS_DB}, tenant accounts switched on, ${LIVE}`,
  rewrites: `${CIS_DB}, tour versions tenants created in the last 7 days, ${LIVE}`,
  pieces: `${CIS_DB}, channel content pieces created for tenants in the last 7 days, ${LIVE}`,
  published: `${CIS_DB}, content pieces published to a tenant channel in the last 7 days, ${LIVE}`,
  quotaTours: `${CIS_DB}, tours used this billing month as a % of the plan's tour quota; tenants at 80% or more are listed`,
  quotaCalls: `${CIS_DB}, API calls this billing month as a % of the plan's call quota; tenants at 80% or more are listed`,
};

export const JOBS_SRC = {
  counts: `CIS job queue, jobs created in the last 30 days by status, ${LIVE}`,
  workers:
    "CIS job queue, workers seen in the last 60 seconds and not stopped (the same rule as Apps and the deploy check)",
  waiting: "CIS job queue, jobs queued and not started yet, right now",
  running: "CIS job queue, jobs running right now",
};

export const PROVIDER_COST_SRC = {
  today:
    "AI calls log (AWS Bedrock, OpenAI, Jev) and the DataForSEO calls log, spend since midnight UTC today; red when it " +
    "reaches the budget's alert level",
  month:
    "AI calls log, this month (the same AI estimate as Cost per app); DataForSEO: its calls log, this month",
  budget: "Spend budgets set in CIS (External Spend), the daily limit per provider",
  aiMonth: "AI calls log, all AI providers, this month (UTC)",
  dfsBalance: "DataForSEO account balance from the last stored balance check (not a live call)",
};

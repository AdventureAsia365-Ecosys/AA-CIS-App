"use client";
// app/admin/overview/_components/BookingFunnelCard.tsx
// AA-858 — "Booking funnel — last 8 weeks": inquiries → trip cases → proposals sent → confirmed, the step
// conversion, a per-week table, open cases by status and an attention line for stuck inquiries.
// Data: booking.v_ops_funnel_daily + booking.v_ops_case_status (Booking's own aggregate views, no customer data).
import { K, useIsPhone } from "../../../_kit";
import { BOOKING_ADMIN_URL } from "../../../../lib/apps";
import { SourceHint } from "./SourceHint";
import { fmtDay, fmtInt, OpsCard, PHONE_CELL, TableWrap, TD, TH, UnavailableNote } from "./opsUi";
import type { BkMetric, BookingData, Section } from "./opsTypes";

const FUNNEL_VIEW = "booking.v_ops_funnel_daily (Booking)";
const STATUS_VIEW = "booking.v_ops_case_status (Booking)";

const STEPS: { key: BkMetric; label: string; short: string; source: string }[] = [
  {
    key: "inquiry_received",
    label: "Inquiries received",
    short: "Inquiries",
    source: `${FUNNEL_VIEW}, metric inquiry_received — inquiries from TripPlanner and the website arriving in Booking`,
  },
  {
    key: "trip_case_created",
    label: "Trip cases opened",
    short: "Cases",
    source: `${FUNNEL_VIEW}, metric trip_case_created — new trip cases (not deleted)`,
  },
  {
    key: "proposal_sent",
    label: "Proposals sent",
    short: "Proposals",
    source: `${FUNNEL_VIEW}, metric proposal_sent — first time a case reached "proposal sent"`,
  },
  {
    key: "confirmed",
    label: "Confirmed",
    short: "Confirmed",
    source: `${FUNNEL_VIEW}, metric confirmed — first time a case reached "confirmed"`,
  },
];

const RATE_KEYS = ["inquiry_to_case", "case_to_proposal", "proposal_to_confirmed"] as const;

/** Plain labels for booking.trip_cases statuses (Booking trip_cases_status_chk). */
const STATUS_LABEL: Record<string, string> = {
  new_inquiry: "New inquiry",
  discovery: "Discovery",
  building: "Building the trip",
  proposal_sent: "Proposal sent",
  confirmed: "Confirmed",
  supplier_confirming: "Confirming suppliers",
  invoice_sent: "Invoice sent",
  payment_pending: "Waiting for payment",
  paid: "Paid",
  pre_departure: "Before departure",
  on_trip: "On the trip",
  completed: "Completed",
  cancelled: "Cancelled",
};

export function BookingFunnelCard({ section, source }: { section: Section<BookingData> | undefined; source: string }) {
  return (
    <OpsCard
      testId="ops-booking"
      title="Booking funnel — last 8 weeks"
      subtitle="From a traveller's inquiry to a confirmed trip (weeks start on Monday, UTC)"
      source={source}
      link={{ href: BOOKING_ADMIN_URL, label: "Open Booking admin", external: true, hint: "AA-Booking staff admin" }}
    >
      {!section ? null : section.status !== "ok" ? (
        <UnavailableNote section={section} />
      ) : (
        <BookingBody data={section} />
      )}
    </OpsCard>
  );
}

function BookingBody({ data }: { data: BookingData }) {
  const phone = useIsPhone();
  const th: React.CSSProperties = phone ? { ...TH, ...PHONE_CELL, fontSize: 11 } : TH;
  const td: React.CSSProperties = phone ? { ...TD, ...PHONE_CELL } : TD;
  const stuck = data.inbox_attention.parked + data.inbox_attention.failed;
  const openStatuses = data.cases_by_status.filter((s) => !s.closed && s.n > 0);
  const closedStatuses = data.cases_by_status.filter((s) => s.closed && s.n > 0);
  return (
    <>
      {stuck > 0 && (
        <div
          data-testid="ops-booking-attention"
          style={{
            marginBottom: 12,
            padding: "10px 12px",
            borderRadius: 10,
            background: K.amberSoft,
            color: K.amber,
            fontSize: 13,
            fontWeight: 500,
          }}
        >
          {stuck > 1 ? `${stuck} inquiries are stuck in the Booking inbox and need a look` : "1 inquiry is stuck in the Booking inbox and needs a look"}
          ({data.inbox_attention.parked} waiting, {data.inbox_attention.failed} failed).
          <SourceHint source={`${FUNNEL_VIEW}, metrics inquiry_parked / inquiry_failed — current inbox status`} />
        </div>
      )}

      <div
        data-testid="ops-booking-steps"
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 130px), 1fr))",
          gap: 8,
          marginBottom: 14,
        }}
      >
        {STEPS.map((s, i) => {
          const rate = i > 0 ? data.conversion_pct[RATE_KEYS[i - 1]] : null;
          return (
            <div
              key={s.key}
              style={{
                minWidth: 0,
                background: K.bg,
                border: `1px solid ${K.line}`,
                borderRadius: 10,
                padding: "10px 12px",
              }}
            >
              <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, display: "flex", alignItems: "center" }}>
                <span style={{ minWidth: 0 }}>
                  {i + 1}. {s.label}
                </span>
                <SourceHint source={`${s.source}; total of the 8 weeks`} />
              </div>
              <div style={{ fontSize: 22, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>
                {fmtInt(data.totals[s.key])}
              </div>
              <div style={{ fontSize: 11, color: K.muted }}>
                {i === 0 ? "start of the funnel" : rate == null ? "— of the step before" : `${rate}% of the step before`}
              </div>
            </div>
          );
        })}
      </div>

      <TableWrap testId="ops-booking-weeks">
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={th}>{phone ? "Week" : "Week of"}</th>
              {STEPS.map((s) => (
                <th key={s.key} style={{ ...th, textAlign: "right" }} title={s.label}>
                  {s.short}
                </th>
              ))}
              {!phone && (
                <th style={{ ...th, textAlign: "right" }}>
                  Cancelled
                  <SourceHint source={`${FUNNEL_VIEW}, metric cancelled — first time a case was cancelled`} size={11} />
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {[...data.weeks].reverse().map((w) => (
              <tr key={w.week_start}>
                <td style={{ ...td, color: K.ink, fontWeight: 500, whiteSpace: "nowrap" }}>{fmtDay(w.week_start)}</td>
                {STEPS.map((s) => (
                  <td key={s.key} style={{ ...td, textAlign: "right", color: w[s.key] ? K.ink : K.muted2 }}>
                    {fmtInt(w[s.key])}
                  </td>
                ))}
                {!phone && (
                  <td style={{ ...td, textAlign: "right", color: w.cancelled ? K.ink : K.muted2 }}>{fmtInt(w.cancelled)}</td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </TableWrap>

      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 12, color: K.muted, marginBottom: 6, display: "flex", alignItems: "center" }}>
          Open trip cases right now: <b style={{ color: K.ink, marginLeft: 4 }}>{fmtInt(data.open_cases)}</b>
          <SourceHint source={`${STATUS_VIEW} — cases not deleted, by current status (completed and cancelled are closed)`} />
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }} data-testid="ops-booking-status">
          {openStatuses.length === 0 && <span style={{ fontSize: 12, color: K.muted }}>No open cases.</span>}
          {openStatuses.map((s) => (
            <StatusChip key={s.status} label={STATUS_LABEL[s.status] ?? s.status} n={s.n} />
          ))}
          {closedStatuses.map((s) => (
            <StatusChip key={s.status} label={STATUS_LABEL[s.status] ?? s.status} n={s.n} muted />
          ))}
        </div>
      </div>
    </>
  );
}

function StatusChip({ label, n, muted }: { label: string; n: number; muted?: boolean }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        borderRadius: 999,
        fontSize: 12,
        border: `1px solid ${K.line}`,
        background: muted ? K.card : K.bg,
        color: muted ? K.muted : K.ink,
      }}
    >
      {label}
      <b style={{ fontVariantNumeric: "tabular-nums" }}>{fmtInt(n)}</b>
    </span>
  );
}

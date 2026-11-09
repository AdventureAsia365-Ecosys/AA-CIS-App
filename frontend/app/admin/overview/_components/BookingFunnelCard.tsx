"use client";
// app/admin/overview/_components/BookingFunnelCard.tsx
// AA-858 — "Booking activity — last 8 weeks": inquiries, trip cases, proposals sent and confirmations, a per-week
// table, open cases by status and an attention line for stuck inquiries.
// S243 — counts only, no "% of the step before": the steps are counted on their own in the window, not as one cohort
// (staff can open a case without an inquiry; a case opened before the window can get its proposal inside it), so
// 8 cases after 5 inquiries read as "160%". The weekly table is the kit DataTable (a card per week on a phone, AA-1006).
// Data: booking.v_ops_funnel_daily + booking.v_ops_case_status (Booking's own aggregate views, no customer data);
// the on-screen source texts (plain words) are in ./sources.ts.
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable, K, useLang, useT } from "../../../_kit";
import { BOOKING_ADMIN_URL } from "../../../../lib/apps";
import { SourceHint } from "./SourceHint";
import { CASE_STATUS, OPS } from "./ops.messages";
import { useSources } from "./sources";
import { fmtDay, fmtInt, hintColumn, OpsCard, UnavailableNote } from "./opsUi";
import type { BkMetric, BookingData, Section } from "./opsTypes";

/** The four counted steps (Cancelled is a table column only). */
type StepKey = Exclude<BkMetric, "cancelled">;

type StepWord = "stepInquiries" | "stepCases" | "stepProposals" | "stepConfirmed";
type StepShort = "stepInquiriesShort" | "stepCasesShort" | "stepProposalsShort" | "stepConfirmed";

const STEPS: { key: StepKey; label: StepWord; short: StepShort }[] = [
  { key: "inquiry_received", label: "stepInquiries", short: "stepInquiriesShort" },
  { key: "trip_case_created", label: "stepCases", short: "stepCasesShort" },
  { key: "proposal_sent", label: "stepProposals", short: "stepProposalsShort" },
  { key: "confirmed", label: "stepConfirmed", short: "stepConfirmed" },
];

/** Plain label of a booking.trip_cases status (Booking trip_cases_status_chk) in the UI language; unknown as sent. */
function useStatusLabel(): (status: string) => string {
  const t = useT(CASE_STATUS) as unknown as (key: string) => string;
  return (status) => (Object.hasOwn(CASE_STATUS.en, status) ? t(status) : status);
}

export function BookingFunnelCard({ section }: { section: Section<BookingData> | undefined }) {
  const t = useT(OPS);
  const { BOOKING_SRC } = useSources();
  return (
    <OpsCard
      testId="ops-booking"
      title={t("bookingTitle")}
      subtitle={t("bookingSubtitle")}
      source={BOOKING_SRC.card}
      link={{ href: BOOKING_ADMIN_URL, label: t("openBooking"), external: true, hint: t("bookingAdminHint") }}
    >
      {!section ? null : section.status !== "ok" ? (
        <UnavailableNote section={section} />
      ) : (
        <BookingBody data={section} />
      )}
    </OpsCard>
  );
}

type Week = BookingData["weeks"][number];

function BookingBody({ data }: { data: BookingData }) {
  const t = useT(OPS);
  const lang = useLang();
  const statusLabel = useStatusLabel();
  const { BOOKING_SRC } = useSources();
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
          {t("stuck", stuck)} {t("stuckParts", data.inbox_attention.parked, data.inbox_attention.failed)}
          <SourceHint source={BOOKING_SRC.attention} />
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
          return (
            <div
              key={s.key}
              data-kpi=""
              style={{
                minWidth: 0,
                background: K.bg,
                border: `1px solid ${K.line}`,
                borderRadius: 10,
                padding: "10px 12px",
              }}
            >
              <div style={{ fontSize: 11, fontWeight: 600, color: K.muted }}>
                {i + 1}. {t(s.label)}
                <SourceHint source={BOOKING_SRC.steps[s.key]} />
              </div>
              <div style={{ fontSize: 22, fontWeight: 600, color: K.ink, fontVariantNumeric: "tabular-nums" }}>
                {fmtInt(data.totals[s.key])}
              </div>
              <div style={{ fontSize: 11, color: K.muted }}>{t("in8Weeks")}</div>
            </div>
          );
        })}
      </div>
      <div data-testid="ops-booking-count-note" style={{ fontSize: 11, color: K.muted, margin: "-6px 0 12px", lineHeight: 1.5 }}>
        {t("countNote")}
      </div>

      <div data-testid="ops-booking-weeks">
        <DataTable<Week>
          tableId="ops-booking-weeks"
          data={[...data.weeks].reverse()}
          getRowId={(w) => w.week_start}
          searchable={false}
          paginate={false}
          compact
          tableMinWidth={440}
          columns={[
            { id: t("weekOf"), header: t("weekOf"), accessorKey: "week_start", enableSorting: false,
              cell: ({ row }) => <span style={{ color: K.ink, fontWeight: 500, whiteSpace: "nowrap" }}>{fmtDay(row.original.week_start, lang)}</span> },
            ...STEPS.map((s): ColumnDef<Week, unknown> => ({
              ...hintColumn(t(s.short), BOOKING_SRC.weeks[s.key]),
              cell: ({ row }) => <span style={{ color: row.original[s.key] ? K.ink : K.muted2, fontVariantNumeric: "tabular-nums" }}>{fmtInt(row.original[s.key])}</span>,
            })),
            { ...hintColumn(t("cancelled"), BOOKING_SRC.cancelled),
              cell: ({ row }) => <span style={{ color: row.original.cancelled ? K.ink : K.muted2, fontVariantNumeric: "tabular-nums" }}>{fmtInt(row.original.cancelled)}</span> },
          ]}
        />
      </div>

      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 12, color: K.muted, marginBottom: 6, display: "flex", alignItems: "center" }}>
          {t("openCasesNow")} <b style={{ color: K.ink, marginLeft: 4 }}>{fmtInt(data.open_cases)}</b>
          <SourceHint source={BOOKING_SRC.openCases} />
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }} data-testid="ops-booking-status">
          {openStatuses.length === 0 && <span style={{ fontSize: 12, color: K.muted }}>{t("noOpenCases")}</span>}
          {openStatuses.map((s) => (
            <StatusChip key={s.status} label={statusLabel(s.status)} n={s.n} />
          ))}
          {closedStatuses.map((s) => (
            <StatusChip key={s.status} label={statusLabel(s.status)} n={s.n} muted />
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

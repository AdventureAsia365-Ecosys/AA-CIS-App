"use client";
// app/admin/tenants/_components/Tenant360BookingBilling.tsx — AA-1037 (root ADR 0019 d9 "Screens now"): Tenant 360 ›
// Billing › Booking invoices, above the AA-666 usage meters. Staff only; a tenant never sees it.
//
// GET /api/admin/tenants/{id}/booking-billing: the agency billing status Booking publishes (booking.v_tenant_billing_status:
// status, overdue amount per currency, paid through, the warn / suspend days) and the CIS side the hourly
// tenant_billing_tick moves from it (account status, live subscriptions' paid through). Read only: invoices are issued,
// paid and voided in Booking admin ("Open in Booking", built by lib/apps.ts bookingAgencyInvoicesUrl only). Kit only
// (Section, KpiTile, Badge, Money, DateText, ErrorState).

import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { Badge, DateText, ErrorState, K, KpiTile, Money, Section, Spinner, apiGet, sans, useT } from "../../../_kit";
import { bookingAgencyInvoicesUrl } from "@/lib/apps";
import {
  accountStatusTone, bookingBillingKey, bookingBillingPath, bookingStatusTone, overdueEntries, type BookingBillingResp,
} from "@/lib/bookingBilling";
import { T360_BOOKING } from "./bookingBilling.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45, margin: 0 } as const;

function OpenInBooking({ tenantId }: { tenantId: string }) {
  const t = useT(T360_BOOKING);
  return (
    <a href={bookingAgencyInvoicesUrl(tenantId)} target="_blank" rel="noopener noreferrer"
      title={t("openInBookingTitle")} data-testid="t360-open-in-booking"
      style={{
        display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
        color: K.accentDeep, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
      }}>
      {t("openInBooking")} <ExternalLink size={12} />
    </a>
  );
}

export function Tenant360BookingBilling({ tenantId }: { tenantId: string }) {
  const t = useT(T360_BOOKING);
  const q = useQuery({
    queryKey: bookingBillingKey(tenantId),
    queryFn: () => apiGet<BookingBillingResp>(bookingBillingPath(tenantId), { admin: true }),
  });

  const statusLabel = (s: string) =>
    s === "ok" ? t("status_ok") : s === "warn" ? t("status_warn") : s === "past_due" ? t("status_past_due") : s;
  const productLabel = (p: string) =>
    p === "content" ? t("product_content") : p === "crm" ? t("product_crm")
      : p === "tripplanner" ? t("product_tripplanner") : p;

  let body: React.ReactNode;
  if (q.isLoading) {
    body = <div style={{ ...small, display: "flex", gap: 6, alignItems: "center" }}><Spinner size={12} /> {t("loading")}</div>;
  } else if (q.isError || !q.data) {
    body = <ErrorState message={t("loadError", (q.error as Error)?.message ?? t("unknownError"))} onRetry={() => q.refetch()} />;
  } else {
    const d = q.data;
    const b = d.booking;
    const overdue = overdueEntries(b?.overdue_amount);
    const reason = d.account.suspend_reason;
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {!d.available && <p role="status" style={small} data-testid="t360-booking-unavailable">{t("unavailable")}</p>}
        {d.available && !b && <p style={small} data-testid="t360-booking-no-row">{t("noRow")}</p>}
        {b && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(160px, 100%), 1fr))", gap: 10 }}>
              <KpiTile size="sm" label={t("status")} testId="t360-booking-status"
                value={<Badge tone={bookingStatusTone(b.status)}>{statusLabel(b.status)}</Badge>} />
              <KpiTile size="sm" label={t("overdue")} testId="t360-booking-overdue"
                value={overdue.length === 0 ? t("overdueNone") : (
                  <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {overdue.map(([code, amount]) => <Money key={code} amount={amount} currency={code} />)}
                  </span>
                )}
                valueTone={overdue.length ? "danger" : undefined}
                hint={b.overdue_invoices ? t("overdueInvoices", b.overdue_invoices) : undefined} />
              <KpiTile size="sm" label={t("paidThrough")} testId="t360-booking-paid-through"
                value={<DateText value={b.paid_through} />} hint={t("paidThroughHint")} />
              <KpiTile size="sm" label={t("oldest")}
                value={b.oldest_overdue_days ? t("days", b.oldest_overdue_days) : null} />
            </div>
            <p style={small} data-testid="t360-booking-rule">
              {b.warn_after_days != null && b.suspend_after_days != null
                ? t("rule", t("days", b.warn_after_days), t("days", b.suspend_after_days))
                : t("ruleUnset")}
            </p>
          </>
        )}
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", alignItems: "center", fontSize: 12 }}
          data-testid="t360-booking-cis-side">
          <span style={{ color: K.muted }}>{t("cisSide")}</span>
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <span style={{ color: K.ink, fontWeight: 600 }}>{t("account")}</span>
            <Badge tone={accountStatusTone(d.account.account_status)}>{d.account.account_status ?? "—"}</Badge>
            {d.account.account_status === "suspended" && reason && (
              <span style={{ color: K.muted }}>
                {t("suspendedFor", reason === "billing" ? t("reason_billing") : reason)}
              </span>
            )}
          </span>
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap", minWidth: 0 }}>
            <span style={{ color: K.ink, fontWeight: 600 }}>{t("subscriptions")}</span>
            {d.subscriptions.length === 0 && <span style={{ color: K.muted }}>{t("noSubscriptions")}</span>}
            {d.subscriptions.map((s) => (
              <span key={s.product} style={{ color: K.body }}>
                {productLabel(s.product)} · {s.status}
                {s.paid_through && <> · {t("subPaidThrough")} <DateText value={s.paid_through} /></>}
              </span>
            ))}
          </span>
        </div>
      </div>
    );
  }

  return (
    <Section title={t("title")} description={t("description")} actions={<OpenInBooking tenantId={tenantId} />}
      testId="t360-booking-billing" style={{ marginBottom: 16 }}>
      {body}
    </Section>
  );
}

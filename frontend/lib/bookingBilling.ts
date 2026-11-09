// frontend/lib/bookingBilling.ts
// AA-1037 (root ADR 0019 d9 "Screens now") — Tenant 360 › Billing › Booking invoices: the answer of
// GET /admin/tenants/{tenant_id}/booking-billing and the pure helpers of its section
// (app/admin/tenants/_components/Tenant360BookingBilling.tsx). Dependency-free (lib/bookingBilling.test.mjs).

export type BookingBillingStatus = "ok" | "warn" | "past_due";

export interface BookingBillingResp {
  tenant_id: string;
  /** false: Booking's view could not be read (the CIS side below is still filled). */
  available: boolean;
  /** null: Booking has no row for the tenant (no invoice terms yet). */
  booking: {
    status: BookingBillingStatus | string;
    oldest_overdue_days: number | null;
    overdue_invoices: number | null;
    /** {ISO currency code: decimal string} */
    overdue_amount: Record<string, string>;
    paid_through: string | null;
    warn_after_days: number | null;
    suspend_after_days: number | null;
  } | null;
  account: { account_status: string | null; suspend_reason: string | null };
  subscriptions: { product: string; status: string; paid_through: string | null }[];
}

export type ChipTone = "success" | "warning" | "danger" | "info" | "neutral";

export function bookingBillingKey(tenantId: string): readonly unknown[] {
  return ["tenant360", tenantId, "booking-billing"];
}

export function bookingBillingPath(tenantId: string): string {
  return `/api/admin/tenants/${encodeURIComponent(tenantId)}/booking-billing`;
}

/** The chip of Booking's status: ok green, warn amber, past_due red; anything else neutral. */
export function bookingStatusTone(status: string | null | undefined): ChipTone {
  return status === "ok" ? "success" : status === "warn" ? "warning" : status === "past_due" ? "danger" : "neutral";
}

/** The chip of the CIS account status (shared.tenants.account_status). */
export function accountStatusTone(status: string | null | undefined): ChipTone {
  switch (status) {
    case "active": return "success";
    case "trial": return "info";
    case "past_due": return "warning";
    case "suspended": return "danger";
    default: return "neutral";
  }
}

/** The overdue amounts as [code, amount] pairs, codes sorted; only ISO-shaped codes and non-zero amounts. */
export function overdueEntries(amounts: Record<string, string> | null | undefined): [string, string][] {
  return Object.entries(amounts ?? {})
    .filter(([code, amount]) => /^[A-Z]{3}$/.test(code) && Number.isFinite(Number(amount)) && Number(amount) !== 0)
    .sort(([a], [b]) => a.localeCompare(b));
}

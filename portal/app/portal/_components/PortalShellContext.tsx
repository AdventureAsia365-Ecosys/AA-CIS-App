"use client";
// portal/app/portal/_components/PortalShellContext.tsx
// AA-430 — the shell state route pages read (a layout cannot pass props into `children`).
// AA-759 — the values now come from react-query in the PortalShell (`_shell/useShellData.ts`), not
// from raw fetches on mount; the shape pages read is unchanged.
import { createContext, useContext } from "react";
import type { ActivityState } from "./workStatus";

/** One row of `/v1/billing` `activity` (the bell, the Activity page, the dashboard feed). */
export interface ShellActivity {
  id: string;
  status: string;
  edit_source: string;
  tour_name: string;
  country: string | null;
  created_at: string;
}

/** AA-760 — an activity row with its state read from the job (null until the versions load). */
export type ShellActivityView = ShellActivity & {
  state: ActivityState | null;
  /** AA-760 — the version's number from ["my-versions"] (null until it loads), to tell repeat rows apart. */
  version_number: number | null;
};

/** react-query status of a shell query: "pending" (no answer yet) | "error" | "success". */
export type ShellLoadStatus = "pending" | "error" | "success";

/** `/v1/billing` as the shell holds it; pages read the fields they need. */
export type ShellBilling = { activity?: ShellActivity[] } & Record<string, unknown>;

export interface PortalShellValue {
  /** "" while /api/tenant/me loads (render a skeleton, not a placeholder name). */
  tenantName: string;
  planTier: string;
  tenantStatus: ShellLoadStatus;
  /** null until the pool answers. */
  poolTotal: number | null;
  catTotal: number;
  billing: ShellBilling | null;
  /** AA-760 — /v1/billing load state (billing + activity): loading / failed is not "empty". */
  billingStatus: ShellLoadStatus;
  retryBilling: () => void;
  /** AA-760 — `billing.activity` joined to the job state (dashboard feed, Activity page, bell). */
  activity: ShellActivityView[];
  globalSearch: string;
  /** Re-read the tenant's tour versions (the My Tours count and page share one query). */
  refreshCatalogCount: () => void;
  /** Success toast (kit ToastProvider, role=status). */
  showToast: (msg: string) => void;
}

export const PortalShellContext = createContext<PortalShellValue | null>(null);

export function usePortalShell(): PortalShellValue {
  const ctx = useContext(PortalShellContext);
  if (!ctx) {
    throw new Error("usePortalShell() must be called from a page under app/portal/ (layout.tsx provides it)");
  }
  return ctx;
}

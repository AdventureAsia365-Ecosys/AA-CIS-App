"use client";
// portal/app/portal/_components/PortalShellContext.tsx
// AA-430 — route migration (tab-state -> real routes per T-stage).
//
// portal/page.tsx used to be a single client component owning tenantName/planTier/
// poolTotal/catTotal/billing/toast/globalSearch as local useState, shared across every
// "tab" by conditional-rendering the matching Tab component inline. Now each T-stage is
// its own route (own page.tsx), so that state has to live one level up, in
// portal/layout.tsx (which stays mounted across navigations within /portal/*, same as
// the old page.tsx stayed mounted across tab switches) — and be exposed to the route
// pages below it via context, since a layout can't pass props directly into `children`.
import { createContext, useContext } from "react";

export interface PortalShellValue {
  tenantName: string;
  planTier: string;
  poolTotal: number;
  catTotal: number;
  billing: any;
  globalSearch: string;
  refreshCatalogCount: () => void;
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

"use client";
// app/admin/usage/page.tsx — AA-1045: AI & spend › Usage explorer. See UsageExplorer.tsx.
// AI & spend roles only (lib/rbac.ts PAGE_RULES /admin/usage; backend spend.read).
import { Suspense } from "react";
import UsageExplorer from "./UsageExplorer";

export default function UsagePage() {
  // useSearchParams() (every control lives in the address) needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <UsageExplorer />
    </Suspense>
  );
}

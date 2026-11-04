"use client";
// app/admin/writing-memory/page.tsx — AA-917 WM-14: Tenant output › Writing memory. See WritingMemory.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/writing-memory; backend tenant.read / ai.admin).
// useSearchParams() (the ?tab= link from the Writing Lab) needs a Suspense boundary in the App Router.
import { Suspense } from "react";
import { ToastProvider } from "../../../_kit";
import WritingMemory from "./WritingMemory";

export default function WritingMemoryPage() {
  return (
    <ToastProvider>
      <Suspense fallback={null}>
        <WritingMemory />
      </Suspense>
    </ToastProvider>
  );
}

"use client";
// app/admin/lab/eval-runs/page.tsx — AA-1169: Writing Lab › Eval runs. See EvalRuns.tsx.
// PAGE_RULES /admin/lab (lab.read) applies; the item read is a logged Lab tenant-text read in the backend.
// useSearchParams() (?tab=, ?run=, ?item=) needs a Suspense boundary in the App Router.
import { Suspense } from "react";
import { ToastProvider } from "../../../_kit";
import EvalRuns from "./EvalRuns";

export default function EvalRunsPage() {
  return (
    <ToastProvider>
      <Suspense fallback={null}>
        <EvalRuns />
      </Suspense>
    </ToastProvider>
  );
}

"use client";
// app/admin/content-workspace/new/page.tsx — AA-1148: a new hand-made tour (blank form; Create makes the source tour
// and its first wording draft, then the editor opens on /admin/content-workspace/<tour id>).
import { ToastProvider } from "../../../_kit";
import TourWorkspaceEditor from "../_parts/TourWorkspaceEditor";

export default function NewHandMadeTourPage() {
  return (
    <ToastProvider>
      <TourWorkspaceEditor tourId={null} />
    </ToastProvider>
  );
}

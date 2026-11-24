"use client";
// app/admin/content-workspace/[tourId]/page.tsx — AA-1148: the editor of one tour in the content workspace (facts,
// customer wording through the AA-1146 draft gate, preview, history, the required list). See
// ../_parts/TourWorkspaceEditor.tsx.
import { useParams } from "next/navigation";
import { ToastProvider } from "../../../_kit";
import TourWorkspaceEditor from "../_parts/TourWorkspaceEditor";

export default function HandMadeTourPage() {
  const params = useParams<{ tourId: string }>();
  return (
    <ToastProvider>
      <TourWorkspaceEditor tourId={params.tourId} />
    </ToastProvider>
  );
}

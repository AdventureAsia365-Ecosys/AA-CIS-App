"use client";
// portal/app/portal/_components/AngleGateTab.tsx — thin wrapper around AngleGateWizard.tsx for
// the standalone /portal/create/write route.
//
// AA-564 Nhóm 4.2 (2026-09-08) — the actual wizard (state machine, every card, every fetch call)
// moved to AngleGateWizard.tsx so SlateTab.tsx can render the SAME wizard embedded inline under a
// Subject row, not just on this standalone page. This file's only job now: read
// ?resume_request_id= (AA-497 — the Slate's "pick to write" handoff before AA-564 4.2 also gave
// SlateTab an embedded option) and pass it down as a prop. Route stays /portal/create/write —
// still a real, valid deep-link (e.g. a bookmarked resume link), not removed.

import { useSearchParams } from "next/navigation";
import AngleGateWizard from "./AngleGateWizard";

export default function AngleGateTab() {
  // AA-497 — the Slate (SlateTab.tsx::pick_subject()) hands off here via
  // /portal/create/write?resume_request_id=... when the tenant reaches this page directly
  // (not embedded) — the ONLY way this standalone route loads a request.
  const searchParams = useSearchParams();
  const resumeRequestId = searchParams.get("resume_request_id");

  return <AngleGateWizard requestId={resumeRequestId} />;
}

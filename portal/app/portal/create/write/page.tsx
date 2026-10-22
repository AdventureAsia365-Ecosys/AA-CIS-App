"use client";
// portal/app/portal/create/write/page.tsx — AA-449 (T8 Angle Gate).
// AA-759: was /portal/t8-angle-gate (308 from the old slug, next.config.ts).
// The route used to follow the T-number convention (t0-brand, t1-rewrite, t4-pool, t6-atoms,
// t7-planning) — see docs/claude_audit/AA-449-00-step0-t8-angle-gate-investigation.md §8.
//
// AA-494 — AngleGateTab reads useSearchParams() (?resume_request_id= from the Slate's "pick to
// write" handoff, AA-522 — the old ?atom_id= entry point and its SlotPickerPanel.tsx source were
// both removed), which requires a Suspense boundary or `next build` fails prerendering this
// route (same pattern t6-atoms/page.tsx already established for AtomsTab's ?tour_id=, AA-454).
import { Suspense } from "react";
import AngleGateTab from "../../_components/AngleGateTab";

export default function AngleGatePage() {
  return (
    <Suspense>
      <AngleGateTab />
    </Suspense>
  );
}

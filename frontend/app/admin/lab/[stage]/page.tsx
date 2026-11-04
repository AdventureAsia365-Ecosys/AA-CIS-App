"use client";
// app/admin/lab/[stage]/page.tsx — AA-1039 R6 slice 3: Writing Lab › one stage (WritingLabStage.tsx). An unknown stage
// slug falls back to the first stage's link (the stage bar lists every known one).
import { useParams } from "next/navigation";
import { Suspense } from "react";
import WritingLabStage from "../WritingLabStage";

export default function WritingLabStagePage() {
  const params = useParams<{ stage: string }>();
  const slug = typeof params?.stage === "string" ? decodeURIComponent(params.stage) : "";
  // useSearchParams() (the ?tab= link) needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <WritingLabStage key={slug} slug={slug} />
    </Suspense>
  );
}

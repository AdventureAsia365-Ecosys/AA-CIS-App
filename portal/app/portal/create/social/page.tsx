"use client";
// portal/app/portal/create/social/page.tsx — Create › Social Posts (T7 Slate, every Channel but blog).
// AA-759: was /portal/t7-planning ("Social Content"; 308 from the old slug, next.config.ts).
import { PageHeader } from "@/app/_kit";
import PlanningTab from "../../_components/PlanningTab";

export default function CreateSocialPage() {
  return (
    <>
      <PageHeader title="Social Posts"
        description="Ranked topics for your social Channels, each cleared against that Channel's bar. Pick one to start writing." />
      <PlanningTab kind="social" />
    </>
  );
}

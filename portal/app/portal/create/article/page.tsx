"use client";
// portal/app/portal/create/article/page.tsx — Create › Articles (AA-759): the same Slate as Social
// Posts, on the blog Channel only (search-led: measured demand and questions to answer).
import { PageHeader } from "@/app/_kit";
import PlanningTab from "../../_components/PlanningTab";

export default function CreateArticlePage() {
  return (
    <>
      <PageHeader title="Articles"
        description="Ranked blog topics with measured search demand. Pick one to start writing an article." />
      <PlanningTab kind="article" />
    </>
  );
}

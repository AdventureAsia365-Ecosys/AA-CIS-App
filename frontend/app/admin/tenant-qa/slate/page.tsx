"use client";
// app/admin/tenant-qa/slate/page.tsx — AA-1039 R6 slice 1 (S266). Tenant content QA › Slate: the per-tenant Slate that
// was section 05 of /admin/atom-curation (that URL with `?section=slate` is a 307 here, lib/adminIa.ts). Admin only
// (lib/rbac.ts PAGE_RULES /admin/tenant-qa). Same frame and sub-nav as the other Tenant content QA pages.
import { PageFrame, PageHeader, useT } from "../../../_kit";
import SocialContentSubNav, { SocialContentSplit } from "../../_components/SocialContentSubNav";
import { SlateSection } from "./SlateSection";
import { SLATE_PAGE } from "./messages";

export default function TenantQaSlatePage() {
  const t = useT(SLATE_PAGE);
  return (
    <PageFrame>
      <PageHeader title={t("title")} description={t("description")} />
      <SocialContentSplit gap={16}>
        <SocialContentSubNav active="slate" />
        {/* width/maxWidth 100%: in the phone column layout a flex-start child would shrink-wrap to its widest row
            (the Slate table's nowrap Seed line) and overflow the page (S241 AA-863). */}
        <div data-testid="tenant-qa-slate-ready" style={{ flex: 1, minWidth: 0, width: "100%", maxWidth: "100%" }}>
          <SlateSection />
        </div>
      </SocialContentSplit>
    </PageFrame>
  );
}

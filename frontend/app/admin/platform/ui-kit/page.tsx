"use client";
// app/admin/platform/ui-kit/page.tsx — AA-1039 R6 rest (root ADR 0019 d13, IA §6.2: Platform › UI kit): the kit's living
// reference (AA-662, SettingsKitTab), which was the UI Kit tab of Settings. Static sample data, no backend read.
// Admin only, like every Platform page (lib/rbac.ts PAGE_RULES /admin/platform); `/admin/settings?tab=ui-kit` answers 307
// here (lib/adminIa.ts ADMIN_ROUTE_MOVES).

import { PageFrame, PageHeader, useT } from "../../../_kit";
import SettingsKitTab from "../../_components/SettingsKitTab";
import { UI_KIT_PAGE } from "./messages";

export default function UiKitPage() {
  const t = useT(UI_KIT_PAGE);
  return (
    <PageFrame>
      <PageHeader title={t("title")} description={t("description")} />
      <div data-testid="admin-content-ready">
        <SettingsKitTab />
      </div>
    </PageFrame>
  );
}

"use client";
// app/admin/platform/infra/page.tsx — Platform › Infra limits: the limits that stay in Terraform, read-only.
// See InfraLimits.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read).
import InfraLimits from "./InfraLimits";

export default function PlatformInfraPage() {
  return <InfraLimits />;
}

"use client";
// app/admin/ai-gateway/page.tsx — AA-804: Platform › AI & spend › AI providers. See AiGateway.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/ai-gateway; backend spend.read).
import { Suspense } from "react";
import AiGateway from "./AiGateway";

export default function AiGatewayPage() {
  // useSearchParams() (the ?tab= / ?trace= links) needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <AiGateway />
    </Suspense>
  );
}

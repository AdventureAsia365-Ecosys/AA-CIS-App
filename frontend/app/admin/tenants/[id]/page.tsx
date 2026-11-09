"use client";
// app/admin/tenants/[id]/page.tsx — AA-1039 R6 rest (root ADR 0019 d13, IA §6.2): Tenant 360 as a route. A Directory
// row opens /admin/tenants/<id>; `?tab=<key>` opens one of its tabs (AI gateway › Budgets links `?tab=cost`). The
// tenant row comes from the same scoped list the Directory reads (GET /admin/tenants: the backend filters it to the
// caller's tenant book, api/core/tenant_scope.py), so a tenant outside the book reads "not found", never a 403 page.
// Status, offboard and restore stay on the Directory row; the panel is _components/TenantDetail.tsx.

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { EmptyState, ErrorState, LoadingScreen, PageFrame, PageHeader, useT } from "../../../_kit";
import { TenantDetail } from "../_components/TenantDetail";
import type { Tenant } from "../_components/tenantRow";
import { TEN } from "../messages";

export default function Tenant360Page() {
  // TenantDetail reads useSearchParams() (the deep-link tab), which needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <Tenant360 />
    </Suspense>
  );
}

function Tenant360() {
  const tr = useT(TEN);
  const params = useParams<{ id: string }>();
  const tenantId = typeof params?.id === "string" ? decodeURIComponent(params.id) : "";
  // AA-797 — include suspended / offboarded tenants, as the Directory does.
  const listQ = useQuery({
    queryKey: ["admin-tenants", "include_inactive"],
    queryFn: async () => {
      const res = await fetch("/api/admin/tenants?include_inactive=true");
      if (!res.ok) throw new Error(String(res.status));
      return ((await res.json()).tenants ?? []) as Tenant[];
    },
  });
  const tenant = listQ.data?.find((x) => x.tenant_id === tenantId) ?? null;
  const crumbs = [{ label: tr("directory"), href: "/admin/tenants" }];

  return (
    <PageFrame>
      <PageHeader title={tenant?.name ?? tr("title")} breadcrumbs={crumbs} />
      {listQ.isPending ? (
        <LoadingScreen message={tr("loadingTenant")} />
      ) : listQ.isError ? (
        <ErrorState message={tr("loadError")} onRetry={() => listQ.refetch()} />
      ) : !tenant ? (
        <EmptyState title={tr("tenantNotFound")} description={tr("tenantNotFoundBody")} />
      ) : (
        <div data-testid="tenant360-ready">
          <TenantDetail key={tenant.tenant_id} tenant={tenant} onChanged={() => listQ.refetch()} />
        </div>
      )}
    </PageFrame>
  );
}

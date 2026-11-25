"use client";
// app/admin/content-workspace/page.tsx — AA-1148: AA Catalogue › Content workspace, the list of hand-made tours (a
// supplier sent one tour outside an Excel sheet). New tour opens the editor; Supplier links opens the report of the old
// tours whose supplier is only the sheet's text.
//
// API: GET /api/admin/content-workspace/tours (BFF → api/routers/admin_content_workspace.py). Loaded on open and on
// Refresh: no polling. Who: lib/rbac.ts PAGE_RULES /admin/content-workspace (content.read); New tour needs content.write
// (can() hides it, the backend re-checks).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import {
  Badge, Button, DataTable, K, PageFrame, PageHeader, apiGet, useAdminIdentity, useFormat, useT, type ColumnDef,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { CW_API, CW_PAGE, stateTone, tourHref, type TourRow } from "@/lib/contentWorkspace";
import { CW } from "./messages";

const KEY = ["content-workspace-tours"] as const;

export default function ContentWorkspacePage() {
  const t = useT(CW);
  const qc = useQueryClient();
  const { role } = useAdminIdentity();
  const { formatDateTime } = useFormat();
  const q = useQuery({
    queryKey: KEY,
    queryFn: () => apiGet<{ items: TourRow[]; total: number }>(`${CW_API}/tours?limit=200`),
    retry: false,
  });
  const columns = useMemo<ColumnDef<TourRow, unknown>[]>(() => [
    {
      accessorKey: "name", header: t("colName"),
      cell: (c) => (
        <Link href={tourHref(c.row.original.tour_id)} data-testid="cw-tour-link"
          style={{ color: K.accentDeep, fontWeight: 600 }}>
          {c.row.original.name || t("untitled")}
        </Link>
      ),
    },
    {
      accessorKey: "supplier_name", header: t("colSupplier"),
      cell: (c) => c.row.original.supplier_name ?? <span style={{ color: K.muted }}>{t("notLinked")}</span>,
    },
    { accessorKey: "country", header: t("colCountry"), cell: (c) => c.row.original.country ?? "—" },
    { accessorKey: "days", header: t("colDays") },
    {
      accessorKey: "state", header: t("colState"),
      cell: (c) => <Badge tone={stateTone(c.row.original.state)}>{t(`state_${c.row.original.state}`)}</Badge>,
    },
    {
      accessorKey: "created_at", header: t("colCreated"),
      cell: (c) => (c.row.original.created_at ? formatDateTime(c.row.original.created_at) : "—"),
    },
  ], [t, formatDateTime]);

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
            <Link href={`${CW_PAGE}/supplier-links`} style={{ textDecoration: "none" }}>
              <Button variant="secondary" size="sm"><Link2 size={13} /> {t("supplierLinks")}</Button>
            </Link>
            <Button variant="secondary" size="sm" onClick={() => void qc.invalidateQueries({ queryKey: KEY })}
              disabled={q.isFetching} aria-label="Refresh">
              <RefreshCw size={13} />
            </Button>
            {can(role, "content.write") && (
              <Link href={`${CW_PAGE}/new`} style={{ textDecoration: "none" }}>
                <Button variant="primary" size="sm"><Plus size={13} /> {t("newTour")}</Button>
              </Link>
            )}
          </span>
        }
      />
      <DataTable<TourRow>
        tableId="content-workspace-tours"
        data={q.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.tour_id}
        loading={q.isLoading}
        error={q.isError ? ((q.error as Error)?.message || t("loadError")) : null}
        onRetry={() => void qc.invalidateQueries({ queryKey: KEY })}
        searchable
        emptyTitle={t("noTours")}
        emptyDescription={t("noToursDesc")}
        tableMinWidth={720}
      />
    </PageFrame>
  );
}

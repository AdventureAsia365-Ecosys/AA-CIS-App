"use client";
// app/(tenant)/portal/_components/CatalogTab.tsx — My Content.
// AA-662 PR 3: migrated to the UI kit (DataTable, Drawer, Toast) + react-query. The hand-rolled
// list fetch + the two setInterval polling effects are replaced by one useQuery with a
// refetchInterval that runs only while a tour is still being AI-written (isAiWriting) and stops on
// its own — no setState-in-effect (React Compiler rule, frontend/AGENTS.md). The drawer body lives
// in CatalogDrawer.tsx. Export (CSV/XLSX/DOCX) and the one-row-per-tour grouping are unchanged.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Download } from "lucide-react";
import { useMemo, useState } from "react";
import * as XLSX from "xlsx";
import { T, sans, Btn, parseContent } from "./ui";
import {
  Badge,
  type ColumnDef,
  DataTable,
  Drawer,
  StatusBadge,
  ToastProvider,
  useToast,
} from "../../../_kit";
import { CatalogDrawer } from "./CatalogDrawer";
import {
  fetchMyVersions,
  fetchVersionDetail,
  isAiWriting,
  isRewriteFailed,
  requestRewrite as apiRequestRewrite,
  retryRewrite as apiRetryRewrite,
  saveVersionEdit,
  type Version,
} from "./catalogApi";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function flattenVal(v: any): string {
  if (v == null || v === "") return "";
  if (typeof v === "string") {
    try {
      return flattenVal(JSON.parse(v));
    } catch {
      /* plain string */
    }
    return v.replace(/[\t\n\r]+/g, " ").trim();
  }
  if (Array.isArray(v)) {
    return v
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .map((x: any) => {
        if (x == null) return "";
        if (typeof x === "string") return x.replace(/[\t\n\r]+/g, " ").trim();
        if (typeof x === "object")
          return String(x.keyword ?? x.text ?? x.description ?? x.title ?? x.value ?? JSON.stringify(x));
        return String(x);
      })
      .filter(Boolean)
      .join(" | ");
  }
  if (typeof v === "object") {
    return Object.values(v)
      .filter((x) => x != null && x !== "")
      .map((x) => String(x))
      .join(" | ");
  }
  return String(v);
}

function CatalogInner() {
  const toast = useToast();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const tourIdFilter = searchParams.get("tour_id");

  const [openTourId, setOpenTourId] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportingDocxId, setExportingDocxId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saveOk, setSaveOk] = useState(false);

  // ── Query: my versions. Poll (5s) only while a tour is still being written; stop automatically
  //    once nothing is writing — read the live result in the interval fn so no effect is needed. ──
  const { data: list = [], isLoading } = useQuery({
    queryKey: ["my-versions"],
    queryFn: fetchMyVersions,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.some(isAiWriting) ? 5000 : false;
    },
  });

  // One row per tour: highest version_number per published_tour_id (unchanged).
  const grouped = useMemo(() => {
    const byTour = new Map<string, Version>();
    for (const v of list) {
      if (!v.published_tour_id) continue;
      const cur = byTour.get(v.published_tour_id);
      if (!cur || v.version_number > cur.version_number) byTour.set(v.published_tour_id, v);
    }
    return [...byTour.values()].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }, [list]);

  const visible = useMemo(
    () => (tourIdFilter ? grouped.filter((g) => g.tour_id === tourIdFilter) : grouped),
    [grouped, tourIdFilter],
  );

  const openGroup = useMemo(
    () => (openTourId ? grouped.find((v) => v.published_tour_id === openTourId) ?? null : null),
    [grouped, openTourId],
  );

  const invalidate = () => qc.invalidateQueries({ queryKey: ["my-versions"] });

  // ── Mutations ──
  const retryM = useMutation({
    mutationFn: apiRetryRewrite,
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(e instanceof Error ? e.message : "Retry failed"),
  });

  const saveM = useMutation({
    mutationFn: (vars: { versionId: string; edited: Record<string, unknown> }) =>
      saveVersionEdit(vars.versionId, vars.edited),
    onSuccess: () => {
      setSaveOk(true);
      setDirty(false);
      toast.success("Saved");
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Save failed"),
  });

  const rewriteM = useMutation({
    mutationFn: (vars: { publishedTourId: string; lang: string }) =>
      apiRequestRewrite(vars.publishedTourId, vars.lang),
    onSuccess: () => {
      toast.info("Rewrite started — your new version will appear here when it's ready");
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Rewrite request failed"),
  });

  // ── DOCX export (unchanged endpoint/trigger) ──
  async function exportDocx(versionId: string) {
    setExportingDocxId(versionId);
    try {
      const r = await fetch(`/api/tenant/v1/tours/versions/${versionId}/export-docx`);
      if (!r.ok) {
        toast.error("DOCX export failed");
        return;
      }
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") || "";
      const match = /filename=([^;]+)/.exec(cd);
      const filename = match ? match[1].trim() : "tour.docx";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExportingDocxId(null);
    }
  }

  // ── CSV/XLSX multi-export (unchanged logic) ──
  async function exportSelected(fmt: "csv" | "xls", selectedVersions: Version[]) {
    if (selectedVersions.length === 0) return;
    setIsExporting(true);
    try {
      const fullData = await Promise.all(
        selectedVersions.map((v) => fetchVersionDetail(v.id).catch(() => null)),
      );
      const headers = [
        "Name",
        "Subtitle",
        "Country",
        "Duration",
        "SEO Title",
        "SEO Meta",
        "Summary",
        "Highlights",
        "Itineraries",
        "Created At",
        "Language",
      ];
      const rows: Record<string, string>[] = fullData.map((d, i) => {
        const v = selectedVersions[i];
        const rc = parseContent(d?.rewritten_content ?? v.rewritten_content) as Record<string, unknown> | null;
        return {
          Name: String(rc?.name ?? d?.aa_name ?? v.aa_name ?? ""),
          Subtitle: String(rc?.subtitle ?? d?.aa_subtitle ?? ""),
          Country: String(d?.country ?? v.country ?? ""),
          Duration: String(d?.duration ?? v.duration ?? ""),
          "SEO Title": String(rc?.seo_title ?? d?.aa_seo_title ?? ""),
          "SEO Meta": String(rc?.seo_meta ?? d?.aa_seo_meta ?? ""),
          Summary: String(rc?.summary ?? d?.aa_summary ?? ""),
          Highlights: flattenVal(rc?.highlights ?? d?.aa_highlights ?? ""),
          Itineraries: flattenVal(rc?.itineraries ?? d?.aa_itineraries ?? ""),
          "Created At": v.created_at ? new Date(v.created_at).toLocaleDateString("en-GB") : "",
          Language: v.rewrite_language,
        };
      });
      if (fmt === "csv") {
        const san = (s: string) => s.replace(/[\t\n\r]+/g, " ");
        const tsv = [headers.join("\t"), ...rows.map((r) => headers.map((h) => san(r[h] ?? "")).join("\t"))].join("\n");
        const blob = new Blob(["\ufeff" + tsv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "my-catalog.csv";
        a.click();
        URL.revokeObjectURL(url);
      } else {
        const ws = XLSX.utils.json_to_sheet(rows, { header: headers });
        ws["!cols"] = [30, 30, 15, 12, 40, 50, 60, 60, 80, 15, 15].map((w) => ({ wch: w }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, "My Catalog Tours");
        XLSX.writeFile(wb, "my-catalog.xlsx");
      }
    } finally {
      setIsExporting(false);
    }
  }

  // ── Columns ──
  const columns = useMemo<ColumnDef<Version, unknown>[]>(
    () => [
      {
        accessorKey: "aa_name",
        header: "Tour Name",
        cell: (c) => {
          const v = c.row.original;
          return (
            <div>
              <div style={{ fontWeight: 600, color: T.ink }}>{v.aa_name || "Tour"}</div>
              {v.aa_subtitle && (
                <div style={{ fontWeight: 400, fontSize: 11.5, color: T.muted, marginTop: 2 }}>{v.aa_subtitle}</div>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "country",
        header: "Country",
        cell: (c) => <span style={{ color: T.body }}>{c.row.original.country || "—"}</span>,
      },
      {
        id: "state",
        header: "Status",
        enableSorting: false,
        cell: (c) => {
          const v = c.row.original;
          if (isAiWriting(v)) return <StatusBadge status={v.job_status === "queued" ? "queued" : "running"} label={v.job_status === "queued" ? "Queued" : "Writing"} />;
          if (isRewriteFailed(v)) return <StatusBadge status="failed" label="Writing failed" />;
          return <Badge tone="success">Ready</Badge>;
        },
      },
      {
        id: "actions",
        header: "Actions",
        enableSorting: false,
        enableHiding: false,
        cell: (c) => {
          const v = c.row.original;
          return (
            <div onClick={(e) => e.stopPropagation()} style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              {isRewriteFailed(v) ? (
                <Btn variant="secondary" size="sm" disabled={retryM.isPending} onClick={() => retryM.mutate(v.id)}>
                  {retryM.isPending ? "Retrying…" : "Retry"}
                </Btn>
              ) : !isAiWriting(v) ? (
                <Btn variant="secondary" size="sm" onClick={() => setOpenTourId(v.published_tour_id ?? null)}>
                  Open →
                </Btn>
              ) : null}
            </div>
          );
        },
      },
    ],
    [retryM],
  );

  return (
    <>
      {tourIdFilter && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
            padding: "9px 14px",
            marginBottom: 12,
            borderRadius: 8,
            background: T.goldTint,
            border: `1px solid ${T.goldSoft}`,
            fontFamily: sans,
          }}
        >
          <span style={{ fontSize: 12, color: T.amber }}>
            Showing versions for: <strong>{visible[0]?.aa_name ?? "this tour"}</strong>
          </span>
          <Link href="/portal/t4-pool" style={{ fontSize: 12, color: T.amber, fontWeight: 600, textDecoration: "none" }}>
            Clear filter ×
          </Link>
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 15, fontWeight: 700, color: T.ink, fontFamily: sans }}>My Catalog Tours</span>
        <span style={{ fontSize: 12, color: T.muted2 }}>
          {visible.length < grouped.length ? `${visible.length} of ${grouped.length}` : `${grouped.length}`} tours
        </span>
      </div>

      <DataTable<Version>
        data={visible}
        columns={columns}
        tableId="my-content"
        getRowId={(r) => r.id}
        loading={isLoading}
        searchable
        searchPlaceholder="Search name or country…"
        enableSelection
        enableSavedViews
        enableCsv
        csvFilename="my-catalog"
        pageSize={25}
        emptyTitle="No rewrites yet"
        emptyDescription="Browse tours and rewrite your first one in your brand voice"
        onRowClick={(v) => {
          if (!isAiWriting(v)) setOpenTourId(v.published_tour_id ?? null);
        }}
        bulkActions={({ selectedRows, clearSelection }) => (
          <>
            <Btn variant="secondary" size="sm" disabled={isExporting} onClick={() => exportSelected("csv", selectedRows)}>
              <Download size={12} /> {isExporting ? "Fetching…" : `CSV (${selectedRows.length})`}
            </Btn>
            <Btn variant="primary" size="sm" disabled={isExporting} onClick={() => exportSelected("xls", selectedRows)}>
              <Download size={12} /> {isExporting ? "Fetching…" : `XLSX (${selectedRows.length})`}
            </Btn>
            <Btn variant="ghost" size="sm" onClick={clearSelection}>
              Clear
            </Btn>
          </>
        )}
      />

      <Drawer open={!!openGroup} onClose={() => setOpenTourId(null)} title="" width={1000}>
        {openGroup && (
          <CatalogDrawer
            key={openGroup.id}
            group={openGroup}
            saving={saveM.isPending}
            saveOk={saveOk}
            dirty={dirty}
            exporting={exportingDocxId === openGroup.id}
            rewriting={rewriteM.isPending}
            retrying={retryM.isPending}
            onDirty={() => {
              setDirty(true);
              setSaveOk(false);
            }}
            onSave={(edited) => saveM.mutate({ versionId: openGroup.id, edited })}
            onRequestRewrite={() =>
              openGroup.published_tour_id &&
              rewriteM.mutate({ publishedTourId: openGroup.published_tour_id, lang: openGroup.rewrite_language || "en-US" })
            }
            onRetry={() => retryM.mutate(openGroup.id)}
            onExportDocx={(id) => exportDocx(id)}
            onClose={() => setOpenTourId(null)}
          />
        )}
      </Drawer>

      {/* StatusBadge's "running" variant uses animation: spin — define the keyframe once. */}
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    </>
  );
}

export default function CatalogTab() {
  return (
    <ToastProvider>
      <CatalogInner />
    </ToastProvider>
  );
}

"use client";
// app/admin/tenant-qa/compliance/page.tsx — AA-1039 R6 slice 2 (root ADR 0019 d20 / d21). Tenant content QA › Compliance:
// the rows of acp_shared.content_compliance_flag (CIS 360) from GET /admin/tenant-qa/compliance. Two kinds:
//   - subject_gate: a tenant piece with no Adventure Asia subject, or a booking link outside the AA catalogue (the
//     AA-subject gate at write and before publish). Held: the publish answers 409 with the sentence the tenant reads.
//     Staff may release the text the check read (POST …/{flag_id}/release, admin only, reason + its content_sha256;
//     409 when the tenant changed the text since).
//   - tour_retired: a live post about a tour AA retired (WordPress: moved to draft after the notice period;
//     Facebook: the tenant is asked to edit, nothing is deleted).
// Admin only (lib/rbac.ts PAGE_RULES /admin/tenant-qa). Tenants never see this page (memory: tenant sees content only).
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, DataTable, Drawer, Field, K, PageFrame, PageHeader, Select, Tabs, Textarea, apiGet, apiSend, mono,
  useFormat, useT, type ColumnDef,
} from "../../../_kit";
import SocialContentSubNav, { SocialContentSplit } from "../../_components/SocialContentSubNav";
import { TenantSelect } from "../../_components/TenantSelect";
import { COMPLIANCE } from "./messages";

type FlagKind = "subject_gate" | "tour_retired";
type Resolution = "passed" | "released" | "restored" | "unpublished" | "drafted" | "superseded";

interface FlagRow {
  flag_id: string;
  tenant_id: string;
  tenant_name: string | null;
  kind: FlagKind;
  stage: "write" | "publish" | "retirement";
  piece_id: string;
  publish_id: string | null;
  tour_id: string | null;
  tour_name: string | null;
  channel: string | null;
  external_url: string | null;
  publish_status: string | null;
  codes: string[];
  detail: {
    refused_links?: string[]; draft_error?: string; release_reason?: string; content_sha256?: string;
    [k: string]: unknown;
  };
  tenant_message: string;
  action: "none" | "draft_after_notice" | "notice_only";
  draft_due_at: string | null;
  drafted_at: string | null;
  state: "open" | "resolved";
  resolution: Resolution | null;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string | null;
  piece_title: string | null;
}

interface FlagList {
  data: FlagRow[];
  total: number;
  open_counts: Record<FlagKind, number>;
}

type StateTab = "open" | "resolved" | "all";

export default function TenantQaCompliancePage() {
  const t = useT(COMPLIANCE);
  const { formatDateTime, formatDate } = useFormat();
  const qc = useQueryClient();
  const [state, setState] = useState<StateTab>("open");
  const [kind, setKind] = useState<"" | FlagKind>("");
  const [tenant, setTenant] = useState("");
  const [open, setOpen] = useState<FlagRow | null>(null);
  const [reason, setReason] = useState("");

  const q = useQuery({
    queryKey: ["tenant-qa-compliance", state, kind, tenant],
    queryFn: () => {
      const p = new URLSearchParams({ state, limit: "200" });
      if (kind) p.set("kind", kind);
      if (tenant) p.set("tenant_id", tenant);
      return apiGet<FlagList>(`/api/admin/tenant-qa/compliance?${p}`);
    },
  });
  const rows = useMemo(() => q.data?.data ?? [], [q.data]);
  const counts = q.data?.open_counts;

  const release = useMutation({
    mutationFn: (f: FlagRow) =>
      // the hash of the text the check read: the API refuses (409) when the tenant changed the text since
      apiSend(`/api/admin/tenant-qa/compliance/${f.flag_id}/release`, {
        body: { reason: reason.trim(), content_sha256: f.detail.content_sha256 ?? "" },
      }),
    onSuccess: () => {
      setOpen(null);
      setReason("");
      qc.invalidateQueries({ queryKey: ["tenant-qa-compliance"] });
    },
  });

  const what = (r: FlagRow): string => {
    if (r.state === "resolved" && r.resolution) return t("resolvedAs", t(`resolution_${r.resolution}`));
    if (r.kind === "subject_gate") return t("actionHeld");
    if (r.action === "draft_after_notice" && r.draft_due_at) return t("actionDraft", formatDate(r.draft_due_at));
    return t("actionNotice");
  };

  const columns = useMemo<ColumnDef<FlagRow, unknown>[]>(() => [
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => r.tenant_name ?? r.tenant_id,
      cell: ({ row: { original: r } }) => (
        <div>
          <div style={{ fontWeight: 600 }}>{r.tenant_name || "—"}</div>
          <div title={r.tenant_id} style={{ fontFamily: mono, fontSize: 10.5, color: K.muted2 }}>{r.tenant_id.slice(0, 8)}</div>
        </div>
      ) },
    { id: "kind", header: t("colKind"), accessorFn: (r) => r.kind, cell: ({ row: { original: r } }) => (
      <Badge tone={r.kind === "subject_gate" ? "warning" : "danger"}>
        {r.kind === "subject_gate" ? t("kindSubjectGate") : t("kindTourRetired")}
      </Badge>
    ) },
    { id: "piece", header: t("colPiece"), accessorFn: (r) => r.piece_title ?? "", cell: ({ row: { original: r } }) => (
      <div style={{ minWidth: 0 }}>
        <div>{r.piece_title || t("noTitle")}</div>
        <div style={{ fontSize: 11, color: K.muted }}>{[r.channel, r.tour_name].filter(Boolean).join(" · ")}</div>
      </div>
    ) },
    { id: "what", header: t("colWhat"), accessorFn: (r) => what(r), cell: ({ row: { original: r } }) => (
      <span style={{ fontSize: 12 }}>{what(r)}</span>
    ) },
    { id: "tenant_sees", header: t("colTenantSees"), accessorFn: (r) => r.tenant_message,
      cell: ({ row: { original: r } }) => <span style={{ fontSize: 12, color: K.body }}>{r.tenant_message}</span> },
    { id: "created_at", header: t("colCreated"), accessorFn: (r) => (r.created_at ? Date.parse(r.created_at) : 0),
      sortDescFirst: true, cell: ({ row: { original: r } }) => (
        <span style={{ fontFamily: mono, fontSize: 11, color: K.muted }}>{formatDateTime(r.created_at)}</span>
      ) },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t, formatDateTime, formatDate]);

  const tabs = [
    { key: "open", label: t("tabOpen"),
      count: counts ? (counts.subject_gate ?? 0) + (counts.tour_retired ?? 0) : undefined },
    { key: "resolved", label: t("tabResolved") },
    { key: "all", label: t("tabAll") },
  ];

  return (
    <PageFrame>
      <PageHeader title={t("title")} description={t("description")} />
      <SocialContentSplit gap={16}>
        <SocialContentSubNav active="compliance" />
        <div style={{ flex: 1, minWidth: 0, width: "100%", maxWidth: "100%" }}>
          <Tabs variant="underline" tabs={tabs} active={state} onChange={(k) => setState(k as StateTab)}
                ariaLabel={t("title")} />
          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, margin: "12px 0" }}>
            <Select aria-label={t("colKind")} value={kind} onChange={(e) => setKind(e.target.value as "" | FlagKind)}
                    options={[
                      { value: "", label: t("allKinds") },
                      { value: "subject_gate", label: t("kindSubjectGate") },
                      { value: "tour_retired", label: t("kindTourRetired") },
                    ]} />
            <TenantSelect inline includeInactive value={tenant} onChange={setTenant} placeholder={t("allTenants")} />
          </div>
          {q.isPending ? (
            <div style={{ padding: 24, textAlign: "center", color: K.muted }}>{t("loading")}</div>
          ) : q.isError ? (
            <div data-testid="admin-content-ready" style={{ padding: 24, textAlign: "center", color: K.danger }}>
              {String(q.error).includes("503") ? t("unavailable") : String(q.error)}
            </div>
          ) : rows.length === 0 ? (
            <div data-testid="admin-content-ready" style={{ padding: 24, textAlign: "center", color: K.muted2 }}>
              {t("empty")}
            </div>
          ) : (
            <div data-testid="admin-content-ready">
              <DataTable<FlagRow> tableId="tenant-qa-compliance" data={rows} columns={columns}
                                  getRowId={(r) => r.flag_id} onRowClick={setOpen} searchable={false}
                                  paginate={false} compact />
            </div>
          )}
        </div>
      </SocialContentSplit>
      <Drawer open={open != null} onClose={() => { setOpen(null); setReason(""); }}
              title={open ? (open.piece_title || t("noTitle")) : undefined}
              footer={open && open.kind === "subject_gate" && open.state === "open" ? (
                <Button variant="primary" disabled={reason.trim().length < 3 || release.isPending}
                        onClick={() => release.mutate(open)}>
                  {release.isPending ? t("releasing") : t("releaseButton")}
                </Button>
              ) : undefined}>
        {open && (
          <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
            <div>
              <strong>{t("drawerFindings")}</strong>
              <ul style={{ margin: "4px 0 0 16px", padding: 0 }}>
                {open.codes.map((c) => <li key={c}>{t(`code_${c}` as "code_no_aa_subject")}</li>)}
              </ul>
            </div>
            <div style={{ color: K.muted }}>{t("drawerStage")}: {t(`stage_${open.stage}`)}</div>
            {open.tour_name && <div>{t("drawerTour")}: {open.tour_name}</div>}
            {open.external_url && (
              <div style={{ overflowWrap: "anywhere" }}>
                {t("drawerPost")}: <a href={open.external_url} target="_blank" rel="noreferrer">{open.external_url}</a>
              </div>
            )}
            {(open.detail.refused_links ?? []).length > 0 && (
              <div>
                <strong>{t("drawerLinks")}</strong>
                <ul style={{ margin: "4px 0 0 16px", padding: 0, fontFamily: mono, fontSize: 11 }}>
                  {(open.detail.refused_links ?? []).map((u) => <li key={u} style={{ overflowWrap: "anywhere" }}>{u}</li>)}
                </ul>
              </div>
            )}
            {open.detail.draft_error && <div style={{ color: K.danger }}>{t("draftError", String(open.detail.draft_error))}</div>}
            <div style={{ background: K.bg, borderRadius: 8, padding: 12 }}>{open.tenant_message}</div>
            {open.kind === "subject_gate" && open.state === "open" && (
              <Field label={t("releaseReason")} hint={t("releaseHelp")}>
                <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} maxLength={500} />
              </Field>
            )}
            {release.isError && <div style={{ color: K.danger }}>{String(release.error)}</div>}
          </div>
        )}
      </Drawer>
    </PageFrame>
  );
}

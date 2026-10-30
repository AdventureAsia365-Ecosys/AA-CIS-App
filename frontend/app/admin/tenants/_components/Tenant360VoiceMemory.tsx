"use client";
// app/admin/tenants/_components/Tenant360VoiceMemory.tsx — AA-921 WM-29: the Tenant 360 "Voice memory" card (app ADR
// 0010): the tenant's active / candidate writing rules and voice summary, its masked voice examples per writer and
// channel, and its latest release per writer. GET /admin/writing-memory/tenants/{id}/voice (admin only, tenant.read;
// every read filters the tenant id). The tenant never sees this card's data (memory: tenant sees content only).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Badge, DataTable, ErrorState, K, KpiTile, Spinner, apiGet, mono, useFormat, useT } from "../../../_kit";
import { WM_API, type VoiceMemory } from "@/lib/writingMemory";
import { VM } from "./voiceMemory.messages";

type ReleaseRow = VoiceMemory["releases"][number];

function H({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em",
    color: K.muted, margin: "18px 0 8px" }}>{children}</div>;
}

export function Tenant360VoiceMemory({ tenantId }: { tenantId: string }) {
  const t = useT(VM);
  const { formatDateTime } = useFormat();
  const q = useQuery({ queryKey: ["tenant360", tenantId, "voice"],
    queryFn: () => apiGet<VoiceMemory>(`${WM_API}/tenants/${tenantId}/voice`) });
  const cols = useMemo<ColumnDef<ReleaseRow, unknown>[]>(() => [
    { accessorKey: "writer", header: t("colWriter"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{c.row.original.writer}</span> },
    { accessorKey: "release_no", header: t("colRelease"), cell: (c) => `#${c.row.original.release_no}` },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={c.row.original.status === "active" ? "success" : "neutral"} dot={false}>
        {c.row.original.status}</Badge> },
    { accessorKey: "content_class", header: t("colClass"),
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 11.5 }}>{c.row.original.content_class}</span> },
    { accessorKey: "rules", header: t("colRules") },
    { accessorKey: "activated_at", header: t("colActivated"),
      cell: (c) => (c.row.original.activated_at ? formatDateTime(c.row.original.activated_at) : "—") },
  ], [t, formatDateTime]);

  const groups = useMemo(() => {
    const out = new Map<string, { pinned: number; candidate: number; never: number }>();
    for (const e of q.data?.exemplars ?? []) {
      const k = `${e.writer} · ${e.channel}`;
      const g = out.get(k) ?? { pinned: 0, candidate: 0, never: 0 };
      g[e.status] += e.n;
      out.set(k, g);
    }
    return [...out.entries()];
  }, [q.data]);

  return (
    <section style={{ minWidth: 0, marginTop: 22 }} data-testid="tenant360-voice-memory">
      <H>{t("title")}</H>
      <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 10 }}>{t("help")}</div>
      {q.isLoading ? (
        <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}>
          <Spinner size={12} /> {t("loading")}</div>
      ) : q.isError || !q.data ? (
        <ErrorState message={t("loadError", (q.error as Error)?.message ?? "")} onRetry={() => q.refetch()} />
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
            <KpiTile size="sm" label={t("activeRules")} value={q.data.totals.active_rules}
              hint={t("candidateRules", q.data.totals.candidate_rules)} />
            <KpiTile size="sm" label={t("summaries")} value={q.data.totals.summaries}
              hint={q.data.preference.enabled ? t("summaryReady") : t("summaryOff")} />
            <KpiTile size="sm" label={t("exemplars")} value={q.data.totals.exemplars} />
            <KpiTile size="sm" label={t("activeReleases")} value={q.data.totals.active_releases} />
          </div>
          {q.data.rules.filter((r) => r.kind === "preference_summary").map((r) => (
            <div key={r.rule_id} style={{ fontSize: 12.5, lineHeight: 1.5, border: `1px solid ${K.line}`,
              borderRadius: 8, padding: 10, overflowWrap: "anywhere" }}>
              <div style={{ fontSize: 11.5, color: K.muted, marginBottom: 4 }}>
                {t("summaryHeading")} · <span style={{ fontFamily: mono }}>{r.writer}</span> · {r.status}</div>
              {r.rule_text}
            </div>
          ))}
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>{t("examplesByChannel")}</div>
            {groups.length ? groups.map(([k, g]) => (
              <div key={k} style={{ fontSize: 12, color: K.ink2, marginBottom: 2 }}>
                <span style={{ fontFamily: mono }}>{k}</span>: {t("exampleCounts", g.pinned, g.candidate, g.never)}
              </div>
            )) : <div style={{ fontSize: 12, color: K.muted }}>{t("noExamples")}</div>}
          </div>
          <DataTable tableId="tenant360-voice-releases" data={q.data.releases} columns={cols}
            getRowId={(r) => r.release_id} paginate={false} compact tableMinWidth={560}
            emptyTitle={t("noReleases")} />
          <a href="/admin/writing-memory" style={{ fontSize: 12.5, color: K.accentDeep }}>{t("openWritingMemory")}</a>
        </div>
      )}
    </section>
  );
}

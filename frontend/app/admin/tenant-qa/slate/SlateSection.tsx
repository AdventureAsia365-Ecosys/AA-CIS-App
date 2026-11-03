"use client";
// app/admin/tenant-qa/slate/SlateSection.tsx — AA-1039 R6 slice 1 (S266). The Slate (each tenant's proposed content
// subjects per channel, acp_shared.subject) moved out of the AA atom store page (/admin/atom-curation, was its
// section 05) into Tenant content QA: it is tenant content, not part of the AA catalogue (root ADR 0019 d13, IA table
// §6.2). The section itself is unchanged (AA-554, AA-561, AA-564, AA-863); its copy stays in atom-curation/messages.ts
// (AT) so the English and Vietnamese text did not move twice. Backend: GET /admin/dashboard/slate (unchanged).
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchJson, EmptyState, ErrorState, AuditTable, Col } from "../../_components/auditPanels";
import { Badge, K, LoadingScreen, Section, Select, mono, sans, useFormat, useT, type Tone } from "../../../_kit";
import { SeedLine, type SeedInfo } from "../../_components/SeedLine";
import { TenantSelect } from "../../_components/TenantSelect";
import { AT } from "../../atom-curation/messages";

// AA-702 — Route scores are averages of Segment total ranks (1392.3333333333333 on screen).
function fmtScore(n: number | null | undefined): string {
  return n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
}

const selectStyle: React.CSSProperties = {
  padding: "8px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8,
  fontSize: 13, fontFamily: sans, color: K.body, cursor: "pointer",
};

interface SlateRow {
  subject_id: string; tenant_name: string | null; channel: string; state: string; score: number | null;
  segment_id: string | null; route_id: string | null; created_at: string;
  // AA-564 2.1 — real topic name, joined server-side the same way the Tenant Portal already does
  // (services/acp_shared/slate.py::fetch_slate()) instead of a raw segment_id/route_id.
  canonical_place: string | null; canonical_action: string | null; hub_name: string | null;
  tour_name: string | null; segment_count: number | null;
  // AA-863 — the Slate seed: the frozen request atom once picked, else the seed a pick would use now.
  seed?: SeedInfo | null;
}

const SLATE_STATE_COLOR: Record<string, Tone> = {
  proposed: "neutral", picked: "info", used: "success", cut: "danger",
};

// AA-554 H.24 — explains all 4 states via a tooltip on each stat-bar badge. `used` text reflects
// this build's own H.1 wiring (no longer "not yet active" — it's real now); `cut` still says so,
// since H.2 only prepared the backend, no tenant UI button exists yet (see the AA-554 child issue).
// Message keys (messages.ts).
const SLATE_STATE_TOOLTIP: Record<string, "tipProposed" | "tipPicked" | "tipUsed" | "tipCut"> = {
  proposed: "tipProposed",
  picked: "tipPicked",
  used: "tipUsed",
  cut: "tipCut",
};

const SLATE_STATE_ORDER = ["proposed", "picked", "used", "cut"] as const;

// AA-561 1e — Nghiệp confirmed the AA-557 G.14 one-liner + column-header tooltips (below) were
// still too technical for a non-code reader ("Kind: Segment" with no explanation). Replaced with
// a plain-language intro block (what Slate IS, in one paragraph) + a full legend for the 4 states
// and 2 Kind values, always visible above the table — not a hover-only tooltip, so it reads
// without the reader needing to know to hover. Keeps the AA-557 G.14 tenant-portal pointer too.
function SlateExplainerNote() {
  const t = useT(AT);
  return (
    <Section style={{ marginBottom: 14, background: K.card }} bodyStyle={{ padding: "14px 18px" }}>
      <div style={{ fontSize: 13, color: K.body, lineHeight: 1.55, marginBottom: 10 }}>
        <strong>{t("whatIsSlate")}</strong> {t("slateIntro")}{" "}
        (<code style={{ fontFamily: mono }}>/portal/t7-planning</code>).
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", fontSize: 11.5, color: K.muted }}>
        <span><strong>proposed</strong> {t("legendProposed")}</span>
        <span><strong>picked</strong> {t("legendPicked")}</span>
        <span><strong>used</strong> {t("legendUsed")}</span>
        <span><strong>cut</strong> {t("legendCut")}</span>
        <span style={{ borderLeft: `1px solid ${K.line}`, paddingLeft: 18 }}>
          <strong>{t("kindSegment")}</strong> {t("legendKindSegment")}
        </span>
        <span><strong>{t("kindRoute")}</strong> {t("legendKindRoute")}</span>
        <span style={{ borderLeft: `1px solid ${K.line}`, paddingLeft: 18 }}>
          <strong>{t("seed")}</strong> {t("legendSeed")}
        </span>
      </div>
    </Section>
  );
}

// AA-564 2.1 — same title logic as the Tenant Portal's SlateTab.tsx:295-297, so Admin and Tenant
// read identically instead of Admin showing a generic "Segment"/"Route" label.
function slateTopicTitle(r: SlateRow, untitled: { journey: string; moment: string }): string {
  if (r.route_id) return r.hub_name ?? untitled.journey;
  return [r.canonical_place, r.canonical_action].filter(Boolean).join(" — ") || untitled.moment;
}

const CHANNELS = ["blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads"];

export function SlateSection() {
  const t = useT(AT);
  const { formatDateTime } = useFormat();
  const untitled = { journey: t("untitledJourney"), moment: t("untitledMoment") };
  // AA-564 2.2 — Slate is genuinely per-tenant (ADR-0003); this section now picks a Tenant, not a
  // Tour, and no longer depends on the page's shared Tour/Market filter at all.
  const [selectedTenant, setSelectedTenant] = useState<string | null>(null);
  const [selectedChannel, setSelectedChannel] = useState("");

  const q = useQuery({
    queryKey: ["tenant-qa", "slate", selectedTenant, selectedChannel],
    enabled: !!selectedTenant,
    queryFn: () => {
      const qs = new URLSearchParams({ tenant_id: selectedTenant ?? "" });
      if (selectedChannel) qs.set("channel", selectedChannel);
      return fetchJson<{ data: SlateRow[]; total: number; by_state: Record<string, number> }>(
        `/api/admin/dashboard/slate?${qs}`);
    },
  });
  const data = selectedTenant ? (q.data ?? null) : null;
  const loading = !!selectedTenant && q.isPending;
  const error = selectedTenant && q.isError ? String((q.error as Error).message || q.error) : null;
  const reload = () => { q.refetch(); };

  const tenantPicker = (
    <div style={{ display: "flex", gap: 10, marginBottom: 14, alignItems: "center", flexWrap: "wrap" }}>
      {/* AA-919 S254: the shared admin tenant picker (active tenants, by name). */}
      <TenantSelect inline value={selectedTenant ?? ""} onChange={id => setSelectedTenant(id || null)}
        placeholder={t("chooseTenant")} />
      <span style={{ fontSize: 12, color: K.muted }}>{t("channel")}</span>
      <Select value={selectedChannel} onChange={e => setSelectedChannel(e.target.value)} style={selectStyle}>
        <option value="">{t("allChannels")}</option>
        {CHANNELS.map(c => <option key={c} value={c}>{c}</option>)}
      </Select>
    </div>
  );

  if (!selectedTenant) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title={t("selectTenant")} body={t("selectTenantBody")} /></>;
  }
  if (error) return <><SlateExplainerNote />{tenantPicker}<ErrorState message={error} onRetry={reload} /></>;
  if (loading) return <><SlateExplainerNote />{tenantPicker}<LoadingScreen message={t("loadingSlate")} /></>;
  if (!data || data.total === 0) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title={t("noSlate")} body={t("noSlateBody")} /></>;
  }
  return (
    <>
      <SlateExplainerNote />
      {tenantPicker}
      {/* AA-554 H.23 — sticky header stat bar, same mechanism Segment/Score's filter rows use.
          H.3 — CUT badge is NOT hidden (shows the real count). AA-556 removed the "coming soon"
          note that used to sit under it, now that the tenant-facing Cut button is real. */}
      <div style={{ display: "flex", gap: 14, marginBottom: 14, flexWrap: "wrap", alignItems: "flex-start",
        position: "sticky", top: 0, background: K.bg, zIndex: 5, paddingTop: 4, paddingBottom: 10 }}>
        {SLATE_STATE_ORDER.map(state => (
          <div key={state}>
            <span title={t(SLATE_STATE_TOOLTIP[state])} style={{ cursor: "help" }}>
              <Badge tone={SLATE_STATE_COLOR[state] ?? "neutral"}>{state}: {data.by_state[state] ?? 0}</Badge>
            </span>
          </div>
        ))}
      </div>
      {/* AA-554 H.25 — Score here is copied as-is from Segment's total_rank / Route's score at
          proposal time, never recomputed by Slate itself. */}
      <div style={{ fontSize: 12, color: K.muted, marginBottom: 10 }}>
        {t("scoreCopied")}
      </div>
      {/* AA-557 G.13 — real table: sortable + per-column filter. */}
      <AuditTable sortable rows={data.data} rowKey={r => r.subject_id} columns={[
        { key: "channel", label: t("colChannel"), render: r => r.channel,
          sortValue: r => r.channel, filterValue: r => r.channel },
        {
          key: "state", label: t("colState"), render: r => (
            <span title={SLATE_STATE_TOOLTIP[r.state] ? t(SLATE_STATE_TOOLTIP[r.state]) : undefined} style={{ cursor: "help" }}>
              <Badge tone={SLATE_STATE_COLOR[r.state] ?? "neutral"}>{r.state}</Badge>
            </span>
          ),
          sortValue: r => r.state, filterValue: r => r.state,
        },
        { key: "score", label: t("colScore"), render: r => fmtScore(r.score), sortValue: r => r.score },
        {
          key: "kind", label: t("colKind"), render: r => (
            <span title={r.route_id ? t("kindRouteTitle") : t("kindSegmentTitle")}
              style={{ cursor: "help" }}>
              {r.route_id ? t("kindRouteWord") : t("kindSegmentWord")}
            </span>
          ),
          sortValue: r => r.route_id ? "Route" : "Segment",
          filterValue: r => r.route_id ? t("kindRouteWord") : t("kindSegmentWord"),
        },
        {
          // AA-564 2.1 — the whole point of this fix: a real topic name + its originating Tour
          // (and segment count for Route-based ideas), instead of a generic "Segment"/"Route" label.
          key: "topic", label: t("colTopic"), render: r => (
            <div>
              <div style={{ fontWeight: 600, color: K.ink }}>{slateTopicTitle(r, untitled)}</div>
              <div style={{ fontSize: 11, color: K.muted2, marginTop: 2 }}>
                {t("topicTour", r.tour_name || "—", r.segment_count != null ? t("topicSegments", r.segment_count) : "")}
              </div>
            </div>
          ),
          sortValue: r => slateTopicTitle(r, untitled),
          filterValue: r => `${slateTopicTitle(r, untitled)} ${r.tour_name ?? ""}`,
        },
        {
          // AA-863 — the seed atom's place · country next to the Subject, with a warning badge when
          // the seed is not the Subject's place (seed rule) or not in the Segment's country.
          key: "seed", label: t("colSeed"), render: r => (
            <div style={{ maxWidth: 280, minWidth: 0 }}>
              {r.seed ? <SeedLine seed={r.seed} /> : <span style={{ color: K.muted2 }}>—</span>}
            </div>
          ),
          sortValue: r => (r.seed?.mismatch ? "0" : "1") + (r.seed?.place ?? ""),
          filterValue: r => `${r.seed?.place ?? ""} ${r.seed?.country ?? ""} ${r.seed?.mismatch ? "mismatch" : ""}`,
        },
        { key: "created", label: t("colProposed"), render: r => formatDateTime(r.created_at),
          sortValue: r => r.created_at },
      ] as Col<SlateRow>[]} />
    </>
  );
}

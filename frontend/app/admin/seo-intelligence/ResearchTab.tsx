"use client";
// app/admin/seo-intelligence/ResearchTab.tsx — the Research tab of SEO Intelligence: automatic search demand research
// (switch, window, caps), the month's research spend against its hard cap, what waits (never-researched places, stale
// places per market, new markets), the last 10 runs with their DataForSEO and LLM + Jev USD, every DataForSEO stage of
// the month, and Preview / Run of a research run and of the tour questions backfill (existing routes; Preview is free).
// Data: GET /api/admin/segment-research/status (BFF -> /admin/segment-research/status).

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  apiGet,
  apiSend,
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Input,
  K,
  KpiTile,
  LoadingScreen,
  Section,
  useT,
} from "../../_kit";
import { marketRows, monthWarn, runScope, usd } from "@/lib/researchPanel";
import { RESEARCH } from "./research.messages";

const SETTINGS_HREF = "/admin/platform/settings?q=research";

interface Run {
  id: string;
  kind: string;
  status: string;
  created_by: string | null;
  scope: string | null;
  markets: string[] | null;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  stop_reason: string | null;
  places_researched: number | null;
  places_selected: number | null;
  filled_store: number | null;
  filled_serp: number | null;
  dfs_usd: number;
  llm_usd: number;
}
interface StageRow { stage: string; usd: number; calls: number }
interface NewPlace { place: string; tours: number; first_seen: string | null }
interface Status {
  running: boolean;
  auto: { enabled: boolean; window_start: number; window_end: number; auto_max_usd_per_run: number; dfs_queue: string };
  month: { cap_usd: number | null; spent_usd: number; remaining_usd: number | null; warn_pct: number } | null;
  pending: {
    places_in_scope: number;
    new: number;
    oldest_new_wait_days: number | null;
    new_places: NewPlace[];
    stale: Record<string, number>;
    activation: string[];
    markets: string[];
  } | null;
  next?: { action: string; kind: string | null; max_usd: number | null; reason: string };
  runs: Run[];
  dfs_month_by_stage: StageRow[];
  history: { rows: number; keywords: number; first_month: string | null; last_month: string | null } | null;
  tour_paa: { published: number; with_paa: number; pct: number | null } | null;
  errors: Record<string, string>;
}
interface Preview {
  places_stale: number;
  places_selected: number;
  estimate: { dfs_usd_cap: number; llm_usd: number };
}
interface PaaPreview {
  fill_from_store: number;
  still_empty_after_store: number;
  serp_fits_caps: number;
  estimate_usd: number;
}

export function ResearchTab() {
  const t = useT(RESEARCH);
  const [markets, setMarkets] = useState("US");
  const [maxUsd, setMaxUsd] = useState("1");
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["admin-segment-research-status"],
    queryFn: () => apiGet<Status>("/api/admin/segment-research/status", { admin: true }),
  });

  const scope = () => runScope(markets, maxUsd);
  const act = async (key: string, fn: () => Promise<string>) => {
    setBusy(key);
    try {
      setNote(await fn());
      await refetch();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (isLoading) return <LoadingScreen message={t("loading")} />;
  if (isError || !data) return <ErrorState message={(error as Error)?.message || ""} onRetry={() => refetch()} />;

  const m = data.month;
  const p = data.pending;
  const markets_ = marketRows(p?.stale, p?.activation);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }} data-testid="seo-research-tab">
      {Object.keys(data.errors ?? {}).length > 0 && (
        <div style={{ fontSize: 12, color: K.amber }}>{t("partial")}</div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
        <KpiTile label={t("autoTitle")} size="sm" testId="research-auto"
          value={data.auto.enabled ? t("autoOn") : t("autoOff")} valueTone={data.auto.enabled ? "success" : "neutral"}
          hint={t("autoHint", data.auto.window_start, data.auto.window_end, data.auto.dfs_queue)} />
        <KpiTile label={t("monthSpend")} size="sm" testId="research-month"
          value={m ? t("monthOf", usd(m.spent_usd), usd(m.cap_usd)) : null}
          valueTone={monthWarn(m) ? "warning" : undefined}
          hint={t("monthNote")} />
        <KpiTile label={t("runCap")} size="sm" value={usd(data.auto.auto_max_usd_per_run)}
          hint={data.next ? `${t("nextRun")}: ${data.next.action}${data.next.kind ? ` (${data.next.kind})` : ""}`
            : undefined} />
        <KpiTile label={t("newPlaces")} size="sm" testId="research-new"
          value={p ? p.new.toLocaleString() : null}
          hint={p?.oldest_new_wait_days != null ? t("oldestWait", Math.round(p.oldest_new_wait_days))
            : t("noneWaiting")} />
        <KpiTile label={t("historyRows")} size="sm"
          value={data.history ? data.history.rows.toLocaleString() : null}
          hint={data.history?.first_month ? t("historyHint", data.history.keywords, data.history.first_month,
            data.history.last_month ?? "") : undefined} />
        <KpiTile label={t("paaCoverage")} size="sm"
          value={data.tour_paa?.pct != null ? `${data.tour_paa.pct}%` : null}
          hint={data.tour_paa ? t("paaCoverageHint", data.tour_paa.with_paa, data.tour_paa.published) : undefined} />
      </div>
      <div style={{ fontSize: 12 }}>
        <Link href={SETTINGS_HREF} style={{ color: K.accentDeep }}>{t("settingsLink")}</Link>
        {data.next && <span style={{ color: K.muted, marginLeft: 10 }}>{data.next.reason}</span>}
      </div>

      <Section title={t("actionsTitle")}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
          <Field label={t("markets")}>
            <Input value={markets} onChange={(e) => setMarkets(e.target.value)} />
          </Field>
          <Field label={t("maxUsd")}>
            <Input value={maxUsd} inputMode="decimal" onChange={(e) => setMaxUsd(e.target.value)} />
          </Field>
          <Button size="sm" testId="research-preview" disabled={busy !== null}
            onClick={() => act("preview", async () => {
              const r = await apiSend<Preview>("/api/admin/segment-research/preview",
                { admin: true, body: scope() });
              return t("previewResult", r.places_selected, r.places_stale,
                usd(r.estimate.dfs_usd_cap + r.estimate.llm_usd));
            })}>{t("preview")}</Button>
          <Button size="sm" variant="primary" testId="research-run" disabled={busy !== null || data.running}
            onClick={() => act("run", async () => {
              const r = await apiSend<{ job_id?: string; reason?: string }>("/api/admin/segment-research/run",
                { admin: true, body: scope() });
              return r.job_id ? t("runStarted", r.job_id) : (r.reason ?? "");
            })}>{t("run")}</Button>
        </div>
        <div style={{ marginTop: 14, fontSize: 13, fontWeight: 600, color: K.ink }}>{t("paaTitle")}</div>
        <div style={{ fontSize: 12, color: K.muted, margin: "4px 0 8px" }}>{t("paaHint")}</div>
        <div style={{ display: "flex", gap: 10 }}>
          <Button size="sm" testId="paa-preview" disabled={busy !== null}
            onClick={() => act("paa-preview", async () => {
              const r = await apiSend<PaaPreview>("/api/admin/segment-research/tour-paa/preview",
                { admin: true, body: { paid: true } });
              return t("paaPreviewResult", r.fill_from_store, r.still_empty_after_store, r.serp_fits_caps,
                usd(r.estimate_usd));
            })}>{t("paaPreview")}</Button>
          <Button size="sm" variant="primary" testId="paa-run" disabled={busy !== null}
            onClick={() => act("paa-run", async () => {
              const r = await apiSend<{ job_id: string }>("/api/admin/segment-research/tour-paa/run",
                { admin: true, body: { paid: true } });
              return t("runStarted", r.job_id);
            })}>{t("paaRun")}</Button>
        </div>
        {note && <div style={{ marginTop: 10, fontSize: 13, color: K.body }} data-testid="research-note">{note}</div>}
      </Section>

      <Section title={t("staleTitle")} description={t("newMarketNote")}>
        <DataTable tableId="research-stale" data={markets_} getRowId={(r) => r.market} searchable={false}
          paginate={false} compact
          columns={[
            { id: "market", header: t("market"), accessorKey: "market",
              cell: ({ row }) => <span style={{ fontWeight: 600 }}>{row.original.market}</span> },
            { id: "stale", header: t("stale"), accessorKey: "stale", meta: { align: "right" } },
            { id: "new", header: "", enableSorting: false, cell: ({ row }) => (row.original.isNew
              ? <Badge tone="warning" dot={false}>{t("newMarket")}</Badge> : null) },
          ]} />
      </Section>

      <Section title={t("pendingTitle")}>
        {p && p.new_places.length > 0 ? (
          <DataTable tableId="research-new-places" data={p.new_places} getRowId={(r) => r.place} pageSize={10}
            searchable compact
            columns={[
              { id: "place", header: t("place"), accessorKey: "place" },
              { id: "tours", header: t("tours"), accessorKey: "tours", meta: { align: "right" } },
              { id: "first", header: t("firstSeen"), accessorKey: "first_seen",
                cell: ({ row }) => (row.original.first_seen ?? "").slice(0, 10) },
            ]} />
        ) : <EmptyState title={t("noneWaiting")} />}
      </Section>

      <Section title={t("runsTitle")}>
        {data.runs.length === 0 ? <EmptyState title={t("noRuns")} /> : (
          <DataTable<Run> tableId="research-runs" data={data.runs} getRowId={(r) => r.id} searchable={false}
            paginate={false} compact
            columns={[
              { id: "kind", header: t("kind"), accessorFn: (r) => r.scope ?? r.kind,
                cell: ({ row }) => row.original.scope ?? row.original.kind },
              { id: "by", header: t("by"), accessorKey: "created_by" },
              { id: "status", header: t("status"), accessorKey: "status",
                cell: ({ row }) => <Badge tone={row.original.status === "succeeded" ? "success"
                  : row.original.status === "running" || row.original.status === "queued" ? "neutral" : "warning"}
                  dot={false}>{row.original.status}</Badge> },
              { id: "places", header: t("places"), meta: { align: "right" },
                accessorFn: (r) => r.places_researched ?? (r.filled_store ?? 0) + (r.filled_serp ?? 0) },
              { id: "dfs", header: t("dfs"), accessorKey: "dfs_usd", meta: { align: "right" },
                cell: ({ row }) => usd(row.original.dfs_usd) },
              { id: "llm", header: t("llm"), accessorKey: "llm_usd", meta: { align: "right" },
                cell: ({ row }) => usd(row.original.llm_usd) },
              { id: "created", header: t("created"), accessorKey: "created_at",
                cell: ({ row }) => row.original.created_at.slice(0, 16).replace("T", " ") },
              { id: "stop", header: t("stop"), accessorFn: (r) => r.stop_reason ?? r.error ?? "",
                cell: ({ row }) => <span style={{ fontSize: 12, color: K.muted }}>
                  {row.original.stop_reason ?? row.original.error ?? t("never")}</span> },
            ]} />
        )}
      </Section>

      <Section title={t("stagesTitle")} description={t("stagesNote")}>
        <DataTable<StageRow> tableId="research-dfs-stages" data={data.dfs_month_by_stage} getRowId={(r) => r.stage}
          searchable={false} paginate={false} compact
          columns={[
            { id: "stage", header: t("stage"), accessorKey: "stage" },
            { id: "calls", header: t("calls"), accessorKey: "calls", meta: { align: "right" } },
            { id: "usd", header: t("usd"), accessorKey: "usd", meta: { align: "right" },
              cell: ({ row }) => usd(row.original.usd) },
          ]} />
      </Section>
    </div>
  );
}

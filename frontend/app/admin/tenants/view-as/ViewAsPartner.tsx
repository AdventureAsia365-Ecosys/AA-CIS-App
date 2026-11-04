"use client";
// app/admin/tenants/view-as/ViewAsPartner.tsx — AA-1035 R2 slice 2 (root ADR 0019 d5): the read-only "view as
// partner" page. The banner stays on top for the whole session (tenant, time left, reason, ticket, End view); below,
// the partner's own reads through the view-as BFF (GET /api/view-as/v1/<path>, lib/viewAs.ts). Nothing on this page
// writes: there is no control that could, and the API refuses every write of the token anyway. Only the reads the
// staff member's role may make are shown (session `allowed_reads`; support: no plan / usage card).

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, LogOut } from "lucide-react";
import {
  Badge, Button, DataTable, DateText, EmptyState, ErrorState, K, KpiTile, PageHeader, Section, Tabs, useT,
  type ColumnDef,
} from "../../../_kit";
import {
  VIEW_AS_API, VIEW_AS_QUERY_KEY, formatCountdown, pickText, rowsOf, secondsLeft, viewAsReadUrl,
  type CurrentViewAs, type ViewAsSession,
} from "@/lib/viewAs";

const QUOTA_READ = "/v1/quota";
const MARKETPLACE_READ = "/v1/marketplace";
import { VIEW_AS } from "./messages";

type Row = Record<string, unknown>;

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = (body as { detail?: unknown }).detail;
    throw Object.assign(new Error(typeof detail === "string" ? detail : `HTTP ${r.status}`), { status: r.status });
  }
  return body as T;
}

export function useCurrentViewAs() {
  return useQuery({
    queryKey: VIEW_AS_QUERY_KEY,
    queryFn: () => getJson<CurrentViewAs>(VIEW_AS_API),
    refetchInterval: 60_000,
  });
}

function Countdown({ expiresAt, onExpired }: { expiresAt: string | null; onExpired: () => void }) {
  const t = useT(VIEW_AS);
  const [left, setLeft] = useState(() => secondsLeft(expiresAt));
  useEffect(() => {
    let fired = false;   // tell the page once, not every second after the expiry
    const id = setInterval(() => {
      const s = secondsLeft(expiresAt);
      setLeft(s);
      if (s <= 0 && !fired) { fired = true; onExpired(); }
    }, 1000);
    return () => clearInterval(id);
  }, [expiresAt, onExpired]);
  return <strong style={{ fontVariantNumeric: "tabular-nums" }}>{t("bannerEndsIn", formatCountdown(left))}</strong>;
}

export function ViewAsBanner({ session, onEnded }: { session: ViewAsSession; onEnded: () => void }) {
  const t = useT(VIEW_AS);
  const [ending, setEnding] = useState(false);
  const end = async () => {
    setEnding(true);
    try { await fetch(VIEW_AS_API, { method: "DELETE" }); } finally { setEnding(false); onEnded(); }
  };
  return (
    <div role="status" aria-live="polite" data-testid="view-as-banner"
      style={{ position: "sticky", top: 0, zIndex: 5, marginBottom: 16, padding: "12px 16px", borderRadius: 10,
        background: K.amberSoft, border: `2px solid ${K.amber}`, color: K.ink, display: "flex", gap: 12,
        alignItems: "flex-start", flexWrap: "wrap" }}>
      <Eye size={18} style={{ color: K.amber, flexShrink: 0, marginTop: 2 }} />
      <div style={{ flex: "1 1 260px", minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <strong style={{ fontSize: 14 }}>{t("bannerTitle", session.tenant_name)}</strong>
          <Badge tone="warning">{t("title")}</Badge>
          <Countdown expiresAt={session.expires_at} onExpired={onEnded} />
        </div>
        <span style={{ fontSize: 13, color: K.body }}>{t("bannerReadOnly")}</span>
        {session.reason && <span style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>
          {t("bannerReason", session.reason)}</span>}
        {session.ticket_ref && <span style={{ fontSize: 12, color: K.muted }}>{t("bannerTicket", session.ticket_ref)}</span>}
      </div>
      <Button variant="secondary" size="sm" onClick={end} disabled={ending} testId="view-as-end">
        <LogOut size={13} /> {ending ? t("ending") : t("endView")}
      </Button>
    </div>
  );
}

type TabKey = "overview" | "tours" | "content" | "published" | "notifications" | "members" | "activity";

interface ListSpec {
  path: string;
  columns: { header: string; keys: readonly string[]; kind?: "date" | "badge" }[];
}

function ReadList({ spec, onEnded }: { spec: ListSpec; onEnded: () => void }) {
  const t = useT(VIEW_AS);
  const q = useQuery({ queryKey: ["view-as", "read", spec.path], queryFn: () => getJson<unknown>(viewAsReadUrl(spec.path)),
    retry: false });
  useEffect(() => {
    if ((q.error as { status?: number } | null)?.status === 401) onEnded();
  }, [q.error, onEnded]);
  const rows = useMemo(() => rowsOf(q.data), [q.data]);
  const columns = useMemo<ColumnDef<Row, unknown>[]>(() => spec.columns.map((c, i) => ({
    id: `c${i}`, header: c.header,
    accessorFn: (r: Row) => pickText(r, c.keys) ?? "",
    cell: (cell) => {
      const v = pickText(cell.row.original, c.keys);
      if (v == null) return <span style={{ color: K.muted }}>{t("unknown")}</span>;
      if (c.kind === "date") return <DateText value={v} withTime />;
      if (c.kind === "badge") return <Badge tone="neutral" dot={false}>{v}</Badge>;
      return <span style={{ overflowWrap: "anywhere" }}>{v}</span>;
    },
  })), [spec.columns, t]);
  if (q.error) return <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />;
  return (
    <DataTable<Row> tableId={`view-as-${spec.path}`} data={rows} columns={columns}
      getRowId={(r) => pickText(r, ["id", "publish_id", "piece_id", "version_id", "membership_id", "request_id", "tour_id"])
        ?? JSON.stringify(r).slice(0, 80)}
      loading={q.isLoading} emptyTitle={t("emptyList")} tableMinWidth={640} compact />
  );
}

function Overview({ onEnded, allowed }: { onEnded: () => void; allowed: (path: string) => boolean }) {
  const t = useT(VIEW_AS);
  const showQuota = allowed(QUOTA_READ);
  const showMarket = allowed(MARKETPLACE_READ);
  const quota = useQuery({ queryKey: ["view-as", "read", QUOTA_READ], retry: false, enabled: showQuota,
    queryFn: () => getJson<{ plan_tier: string; tours_per_month: number; rewrites_used: number; resets_at: string }>(
      viewAsReadUrl(QUOTA_READ)) });
  const market = useQuery({ queryKey: ["view-as", "read", MARKETPLACE_READ], retry: false, enabled: showMarket,
    queryFn: () => getJson<unknown>(viewAsReadUrl(MARKETPLACE_READ)) });
  useEffect(() => {
    for (const e of [quota.error, market.error]) if ((e as { status?: number } | null)?.status === 401) onEnded();
  }, [quota.error, market.error, onEnded]);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
      {showQuota && <Section title={t("quotaTitle")}>
        {quota.error ? <span style={{ color: K.danger, fontSize: 13 }}>{t("loadError")}</span> : quota.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13, color: K.body }}>
            <KpiTile label={t("planLabel")} value={quota.data.plan_tier} />
            <span>{t("quotaLine", quota.data.rewrites_used, quota.data.tours_per_month)}</span>
            <span style={{ color: K.muted }}>{t("quotaResets", quota.data.resets_at)}</span>
          </div>
        ) : null}
      </Section>}
      {showMarket && <Section title={t("marketplaceTitle")}>
        {market.error ? <span style={{ color: K.danger, fontSize: 13 }}>{t("loadError")}</span> : market.data ? (
          <span style={{ fontSize: 13, color: K.body }}>{t("marketplaceLine", rowsOf(market.data).length)}</span>
        ) : null}
      </Section>}
    </div>
  );
}

export default function ViewAsPartner() {
  const t = useT(VIEW_AS);
  const qc = useQueryClient();
  const router = useRouter();
  const current = useCurrentViewAs();
  const [tab, setTab] = useState<TabKey>("overview");
  const onEnded = useMemo(() => () => {
    void qc.invalidateQueries({ queryKey: VIEW_AS_QUERY_KEY });
    qc.removeQueries({ queryKey: ["view-as", "read"] });
  }, [qc]);
  const endAndRefresh = useCallback(() => { onEnded(); router.refresh(); }, [onEnded, router]);

  const specs: Record<Exclude<TabKey, "overview">, ListSpec> = useMemo(() => ({
    tours: { path: "/v1/tours/my-versions", columns: [
      { header: t("colTour"), keys: ["tour_name", "title", "name", "tour_id"] },
      { header: t("colStatus"), keys: ["status"], kind: "badge" },
      { header: t("colUpdated"), keys: ["updated_at", "created_at"], kind: "date" }] },
    content: { path: "/v1/content-writing/reviews", columns: [
      { header: t("colTitle"), keys: ["angle_name", "title", "tour_name", "request_id"] },
      { header: t("colChannel"), keys: ["channel"], kind: "badge" },
      { header: t("colStatus"), keys: ["status", "piece_status"], kind: "badge" },
      { header: t("colUpdated"), keys: ["updated_at", "created_at"], kind: "date" }] },
    published: { path: "/v1/publish-log", columns: [
      { header: t("colTitle"), keys: ["title"] },
      { header: t("colChannel"), keys: ["channel"], kind: "badge" },
      { header: t("colStatus"), keys: ["status"], kind: "badge" },
      { header: t("colWhen"), keys: ["published_at", "unpublished_at"], kind: "date" }] },
    notifications: { path: "/v1/notifications", columns: [
      { header: t("colTitle"), keys: ["title"] },
      { header: t("colMessage"), keys: ["message"] },
      { header: t("colWhen"), keys: ["created_at"], kind: "date" }] },
    members: { path: "/v1/account/members", columns: [
      { header: t("colMember"), keys: ["member_ref", "membership_id"] },
      { header: t("colRole"), keys: ["role"], kind: "badge" },
      { header: t("colStatus"), keys: ["status"], kind: "badge" },
      { header: t("colUpdated"), keys: ["updated_at", "added_at"], kind: "date" }] },
    activity: { path: "/v1/account/activity", columns: [
      { header: t("colAction"), keys: ["label", "action"] },
      { header: t("colMember"), keys: ["actor"] },
      { header: t("colWhen"), keys: ["at"], kind: "date" }] },
  }), [t]);

  const header = (
    <PageHeader title={t("title")} description={t("description")}
      breadcrumbs={[{ label: t("crumbTenants"), href: "/admin/tenants" }, { label: t("title") }]} />
  );
  if (current.isLoading) return header;
  if (current.error) {
    return <>{header}<ErrorState message={`${t("loadError")}: ${(current.error as Error).message}`}
      onRetry={() => current.refetch()} /></>;
  }
  const s = current.data;
  if (!s || !s.active) {
    return (
      <>
        {header}
        <EmptyState icon={<Eye size={20} />} title={t("noSession")} description={t("noSessionHint")}
          action={<Link href="/admin/tenants"><Button variant="secondary" size="sm">{t("openDirectory")}</Button></Link>} />
      </>
    );
  }
  // The reads this member's role may make (the BFF and the API refuse the others anyway); an older answer without
  // the list falls back to every read, which the BFF still filters per role.
  const allowedReads = s.allowed_reads;
  const allowed = (path: string) => !allowedReads || allowedReads.includes(path);
  const tabs: { key: TabKey; label: string }[] = ([
    { key: "overview", label: t("tabOverview") }, { key: "tours", label: t("tabTours") },
    { key: "content", label: t("tabContent") }, { key: "published", label: t("tabPublished") },
    { key: "notifications", label: t("tabNotifications") }, { key: "members", label: t("tabMembers") },
    { key: "activity", label: t("tabActivity") },
  ] as { key: TabKey; label: string }[]).filter((x) => x.key === "overview" || allowed(specs[x.key].path));
  return (
    <>
      <ViewAsBanner session={s} onEnded={endAndRefresh} />
      {header}
      <div style={{ borderBottom: `1px solid ${K.line}`, marginBottom: 14 }}>
        <Tabs variant="underline" gap={18} ariaLabel={t("title")} testId="view-as-tabs" tabs={tabs} active={tab}
          onChange={(k) => setTab(k as TabKey)} style={{ marginBottom: -1 }} />
      </div>
      {tab === "overview" ? <Overview onEnded={onEnded} allowed={allowed} />
        : <Section padded={false}><ReadList key={tab} spec={specs[tab]} onEnded={onEnded} /></Section>}
    </>
  );
}

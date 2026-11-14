"use client";
// app/admin/platform/settings/SettingsRegistry.tsx — S279 thresholds L1: Platform › Settings.
//
// Every admin-editable threshold, limit, cap and policy value (the settings registry, CIS 407): tabs by app, then by
// area; per setting the value in force and where it comes from (approved value, default, emergency env override),
// the default, the allowed range, the last change (who, when, why), the badges ("overridden by env", "not enforced
// yet", a pending proposal, evidence required) and the actions: Change (propose, and approve at once when allowed)
// and History (every version with the value it replaced; approve / reject an open proposal). Booking keeps its own
// maker-checker page (Pricing › Policy): its tab links there.
//
// API (backend api/routers/admin_settings_registry.py; BFF /api/admin/settings/registry, admin only — lib/rbac.ts):
//   GET  /api/admin/settings/registry?app=cis
//   GET  /api/admin/settings/registry/{key}/history
//   POST /api/admin/settings/registry/{key}/proposals      {value, reason, evidence_ref?, effective_from?, base_version,
//                                                           apply}
//   POST /api/admin/settings/registry/proposals/{id}/approve | reject   {note?}
// Pure helpers: lib/settingsRegistry.ts. Kit only.
// AA-1096 l23: every row explains itself (SettingGuide.tsx): the one-line "what" under the label, an Explain toggle
// with the whole guide, the same guide at the top of the change dialog; who changed a value is shown by name.
// AA-1106: `?app=<app>&area=<area>` opens that app tab and area (the Product map links a setting named in a condition
// here); an unknown value falls back to the first tab / area.
// AA-1096 (S280): a key that allows a tenant value changes the platform or one tenant (SettingScope.tsx; the row
// counts the tenant values, the history dialog shows each tenant's value and history); the search box searches every
// area of the app by key, label and description (SettingsTable.tsx).
//   GET  /api/admin/settings/registry/{key}/history?tenant_id=   one tenant's rows
//   POST .../proposals {..., tenant_id?}                         a tenant value
// AA-1096 (S280 UX fix round, tech lead "check and fix everything"): the change dialog (SettingPropose.tsx) and the
// history dialog (SettingHistory.tsx) live in their own files. The row shows a scheduled change ("Change scheduled"
// badge, "Scheduled: <value> from <date>" under the value in force), why a stored value is refused, and as "Last
// change" an approved change only (a CRM rejection is labelled); Change is disabled while the platform has an open
// proposal of a platform-only key; on the CRM tab Change / Approve appear only for a CRM writer (the list's
// can_write, the CRM's own answer); a 503 says "not set up yet" only for the registry's own codes, else "did not
// answer" with a retry. Values, bounds, areas and the form's problems are worded in the page's language.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, History, Pencil, SlidersHorizontal } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import {
  Badge, Button, DateText, ErrorState, K, PageFrame, PageHeader, Section, Tabs, mono, useT, type ColumnDef, type Tone,
} from "../../../_kit";
import { TenantCount } from "./SettingScope";
import { SettingsTable } from "./SettingsTable";
import { useAdminIdentity } from "../../../_kit/useAdminIdentity";
import { BOOKING_ADMIN_URL } from "@/lib/apps";
import {
  APPS, badgesOf, boundsText, formatValue, lastChangeState, loadProblem, openProposal, registryPath, scheduledOf,
  tenantScoped, valuesInForce, type BadgeKey, type RegistryResp, type SettingApp, type SettingItem, type ValueOf,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";
import { ActorName, SettingCell } from "./SettingGuide";
import { HistoryModal, StatusPill } from "./SettingHistory";
import { ProposeModal } from "./SettingPropose";
import { getJson, type RegistryError } from "./settingsApi";
import { useSettingWords, type SettingWords } from "./useSettingWords";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const APP_LABEL: Record<SettingApp, "appCis" | "appTripplanner" | "appCrm" | "appBooking"> = {
  cis: "appCis", tripplanner: "appTripplanner", crm: "appCrm", booking: "appBooking",
};
type BadgeLabel = "badgeEnv" | "badgeNotEnforced" | "badgePending" | "badgeRefused" | "badgeEvidence" | "badgeChanged" |
  "badgeScheduled";
const BADGE: Record<BadgeKey, { tone: Tone; label: BadgeLabel }> = {
  env: { tone: "warning", label: "badgeEnv" },
  notEnforced: { tone: "neutral", label: "badgeNotEnforced" },
  pending: { tone: "info", label: "badgePending" },
  scheduled: { tone: "accent", label: "badgeScheduled" },
  refused: { tone: "danger", label: "badgeRefused" },
  evidence: { tone: "purple", label: "badgeEvidence" },
  changed: { tone: "accent", label: "badgeChanged" },
};

function useSettingsT() {
  return useT(SETTINGS);
}
type ST = ReturnType<typeof useSettingsT>;

function Badges({ item }: { item: SettingItem }) {
  const t = useSettingsT();
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
      {item.impact === "high" && <Badge tone="warning" dot={false}>{t("impactHigh")}</Badge>}
      {badgesOf(item).map((b) => <Badge key={b} tone={BADGE[b].tone} dot={false}>{t(BADGE[b].label)}</Badge>)}
    </span>
  );
}

function sourceText(item: SettingItem, t: ST): string {
  if (item.source === "env") return t("sourceEnv");
  if (item.source === "tenant") return t("sourceTenant");
  if (item.source === "platform") return t("sourcePlatform", item.version ?? 0);
  return t("sourceDefault");
}

/** Value in force: the value, where it comes from, why a stored value is refused, and the next scheduled change. */
function ValueCell({ item }: { item: SettingItem }) {
  const t = useSettingsT();
  const { words } = useSettingWords();
  const next = scheduledOf(item)[0];
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <strong style={{ fontFamily: mono, fontSize: 13, wordBreak: "break-word" }}>{formatValue(item.value, item, words)}</strong>
      <span style={small}>{sourceText(item, t)}</span>
      {item.refused && <span style={{ ...small, color: K.danger }} data-testid={`settings-refused-${item.key}`}>
        {t("refusedWhy", item.refused)}</span>}
      {next && <span style={small} data-testid={`settings-scheduled-${item.key}`}>
        {t("scheduledLine", formatValue(next.value, item, words))} <DateText value={next.effective_from} withTime />
      </span>}
    </div>
  );
}

/** Last change: who, when, why; a rejection (the CRM lists one) or a scheduled approval is labelled. */
function LastChangeCell({ item }: { item: SettingItem }) {
  const t = useSettingsT();
  const last = item.last_change;
  if (!last) return <span style={small}>{t("never")}</span>;
  return (
    <span style={small}>
      {lastChangeState(last) && <><StatusPill row={last} />{" "}</>}
      <ActorName actor={last.decided_by} name={last.decided_by_name} />, <DateText value={last.decided_at} withTime />
      <br />{last.reason}
    </span>
  );
}

function ActionsCell({ item, canWrite, onChange, onHistory }: {
  item: SettingItem; canWrite: boolean; onChange: () => void; onHistory: () => void;
}) {
  const t = useSettingsT();
  // a platform-only key with an open proposal cannot take a second one: review it first
  const pending = !tenantScoped(item) && !!openProposal(item, null);
  return (
    <span style={{ display: "inline-flex", gap: 6 }}>
      {canWrite && <Button size="sm" variant="secondary" onClick={onChange} disabled={pending}
        title={pending ? t("pendingTitle") : undefined} testId={`settings-change-${item.key}`}>
        <Pencil size={12} /> {t("change")}</Button>}
      <Button size="sm" variant="secondary" onClick={onHistory}>
        <History size={12} /> {item.proposals.length ? t("review") : t("history")}</Button>
    </span>
  );
}

function useColumns(valueOf: ValueOf, words: SettingWords["words"], actions: (s: SettingItem) => ReactNode):
  ColumnDef<SettingItem, unknown>[] {
  const t = useSettingsT();
  return useMemo<ColumnDef<SettingItem, unknown>[]>(() => [
    { id: "setting", header: t("colSetting"), accessorKey: "label",
      cell: ({ row: { original: s } }) => (
        <SettingCell item={s} badges={<Badges item={s} />} valueOf={valueOf}
          extra={<>
            <TenantCount item={s} />
            {s.env_ignored && <span style={{ ...small, color: K.danger }}>{t("envIgnored", s.env_ignored)}</span>}
          </>} />
      ) },
    { id: "value", header: t("colValue"), accessorFn: (s) => formatValue(s.value, s, words),
      cell: ({ row: { original: s } }) => <ValueCell item={s} /> },
    { id: "default", header: t("colDefault"), accessorFn: (s) => formatValue(s.default_value, s, words),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12, wordBreak: "break-word" }}>
        {String(getValue())}</span> },
    { id: "bounds", header: t("colBounds"), accessorFn: (s) => boundsText(s, words),
      cell: ({ getValue }) => <span style={small}>{String(getValue()) || "—"}</span> },
    { id: "last", header: t("colLast"), accessorFn: (s) => s.last_change?.decided_at ?? "",
      cell: ({ row: { original: s } }) => <LastChangeCell item={s} /> },
    { id: "actions", header: t("colActions"), enableSorting: false,
      cell: ({ row: { original: s } }) => actions(s) },
  ], [t, valueOf, words, actions]);
}

/** Why the list did not load: not set up yet (the registry's own 503 codes), the app did not answer (retry), other. */
function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const t = useSettingsT();
  const e = error as RegistryError;
  const kind = loadProblem(e.status, e.code);
  if (kind === "notMigrated") return <Section><div style={small}>{t("notMigrated")}</div></Section>;
  if (kind === "unavailable") return <ErrorState message={t("unavailable")} onRetry={onRetry} />;
  return <ErrorState message={`${t("loadError")}: ${e.message}`} onRetry={onRetry} />;
}

function BookingLink() {
  const t = useSettingsT();
  return (
    <Section testId="settings-booking" bodyStyle={{ display: "grid", gap: 10 }}>
      <div style={small}>{t("bookingNote")}</div>
      <a href={`${BOOKING_ADMIN_URL}/pricing/policy`} target="_blank" rel="noopener noreferrer"
        style={{ color: K.accentDeep, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 6 }}>
        {t("openBooking")} <ExternalLink size={13} />
      </a>
    </Section>
  );
}

function RegistryNotes({ data, crmReadOnly }: { data: RegistryResp; crmReadOnly: boolean }) {
  const t = useSettingsT();
  return (
    <div style={small} data-testid="settings-two-person">
      {data.two_person_approval ? t("twoPersonOn") : t("twoPersonOff")}
      {crmReadOnly && <><br /><span data-testid="settings-crm-read-only">{t("crmReadOnly")}</span></>}
      {data.reader_error && <><br /><span style={{ color: K.danger }}>{t("readerError", data.reader_error)}</span></>}
    </div>
  );
}

function AppSettings({ app, initialArea }: { app: SettingApp; initialArea: string }) {
  const t = useSettingsT();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const { words } = useSettingWords();
  const q = useQuery({ queryKey: ["settings-registry", app], queryFn: () => getJson<RegistryResp>(registryPath(app)),
    enabled: app !== "booking" });
  const items = useMemo(() => q.data?.settings ?? [], [q.data]);
  const valueOf = useMemo(() => valuesInForce(items), [items]);
  // the CRM checks its own group (crm-platform-admin writes): offer the writes only when it says this person may
  const crmReadOnly = app === "crm" && q.data?.can_write === false;
  const canWrite = isAdmin && !crmReadOnly;
  const [editing, setEditing] = useState<SettingItem | null>(null);
  const [viewing, setViewing] = useState<SettingItem | null>(null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["settings-registry", app] });
    void qc.invalidateQueries({ queryKey: ["settings-history"] });
  };
  const actions = useCallback((s: SettingItem) => (
    <ActionsCell item={s} canWrite={canWrite} onChange={() => setEditing(s)} onHistory={() => setViewing(s)} />
  ), [canWrite]);
  const columns = useColumns(valueOf, words, actions);

  if (app === "booking") return <BookingLink />;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      {q.isError && <LoadError error={q.error} onRetry={() => void q.refetch()} />}
      {q.data && <RegistryNotes data={q.data} crmReadOnly={isAdmin && crmReadOnly} />}
      {q.data && items.length === 0 && <Section><div style={small}>{t("noneYet")}</div></Section>}
      <SettingsTable app={app} settings={items} initialArea={initialArea} columns={columns} />
      {editing && (
        <ProposeModal item={editing} twoPerson={!!q.data?.two_person_approval} canWrite={canWrite} valueOf={valueOf}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      )}
      {viewing && (
        <HistoryModal item={viewing} canApprove={canWrite} onClose={() => setViewing(null)}
          onDone={() => { setViewing(null); refresh(); }} />
      )}
    </div>
  );
}

/** AA-1106: the app tab and area asked by `?app=&area=` (an unknown app opens CIS); picking another tab drops the
 *  asked area. */
function useAskedTab() {
  const params = useSearchParams();
  const asked = params.get("app");
  const [app, setApp] = useState<SettingApp>(
    (APPS as readonly string[]).includes(asked ?? "") ? (asked as SettingApp) : "cis");
  const [askedArea, setAskedArea] = useState(params.get("area") ?? "");
  const pick = (k: string) => { setApp(k as SettingApp); setAskedArea(""); };
  return { app, askedArea, pick };
}

export default function SettingsRegistry() {
  const t = useSettingsT();
  const { app, askedArea, pick } = useAskedTab();
  return (
    <PageFrame>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <SlidersHorizontal size={18} /> {t("title")}</span>}
        description={t("intro")}
      />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        <Tabs ariaLabel={t("appsLabel")} active={app} onChange={pick} testId="settings-apps"
          tabs={APPS.map((a) => ({ key: a, label: t(APP_LABEL[a]) }))} />
        <AppSettings key={app} app={app} initialArea={askedArea} />
      </div>
    </PageFrame>
  );
}

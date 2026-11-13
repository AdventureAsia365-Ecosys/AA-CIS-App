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

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, History, Pencil, SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, Checkbox, DataTable, DateText, ErrorState, Field, Input, K, Modal, PageFrame, PageHeader, Section,
  Select, Tabs, Textarea, apiGet, mono, useT, useToast, type ColumnDef, type Tone,
} from "../../../_kit";
import { useAdminIdentity } from "../../../_kit/useAdminIdentity";
import { BOOKING_ADMIN_URL } from "@/lib/apps";
import { staffErrorMessage } from "@/lib/rbac";
import {
  APPS, actorText, badgesOf, boundsText, decisionPath, draftProblem, draftText, formatValue, groupByArea,
  historyPath, proposalBody, proposalPath, registryPath, type BadgeKey, type HistoryResp, type ProposalDraft,
  type RegistryResp, type SettingApp, type SettingItem, type SettingRow,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const APP_LABEL: Record<SettingApp, "appCis" | "appTripplanner" | "appCrm" | "appBooking"> = {
  cis: "appCis", tripplanner: "appTripplanner", crm: "appCrm", booking: "appBooking",
};
const BADGE: Record<BadgeKey, { tone: Tone; label: "badgeEnv" | "badgeNotEnforced" | "badgePending" | "badgeRefused" |
  "badgeEvidence" | "badgeChanged" }> = {
  env: { tone: "warning", label: "badgeEnv" },
  notEnforced: { tone: "neutral", label: "badgeNotEnforced" },
  pending: { tone: "info", label: "badgePending" },
  refused: { tone: "danger", label: "badgeRefused" },
  evidence: { tone: "purple", label: "badgeEvidence" },
  changed: { tone: "accent", label: "badgeChanged" },
};
const STATUS: Record<SettingRow["status"], { tone: Tone; label: "statusProposed" | "statusApproved" |
  "statusRejected" | "statusSuperseded" }> = {
  proposed: { tone: "info", label: "statusProposed" },
  approved: { tone: "success", label: "statusApproved" },
  rejected: { tone: "danger", label: "statusRejected" },
  superseded: { tone: "neutral", label: "statusSuperseded" },
};

type Sent = { ok: true; data: unknown } | { ok: false; message: string };

function useSettingsT() {
  return useT(SETTINGS);
}
type ST = ReturnType<typeof useSettingsT>;

async function post(path: string, body: unknown, t: ST): Promise<Sent> {
  let res: Response;
  try {
    res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
  } catch {
    return { ok: false, message: t("network") };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, data };
  const detail = data.detail as { message?: string } | string | undefined;
  const message = typeof detail === "object" && detail && "message" in detail && detail.message
    ? String(detail.message) : staffErrorMessage(res.status, detail, t("failed", res.status), t("readOnly"));
  return { ok: false, message };
}

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

function ProposeModal({ item, twoPerson, canWrite, onClose, onDone }: {
  item: SettingItem; twoPerson: boolean; canWrite: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useSettingsT();
  const toast = useToast();
  const [draft, setDraft] = useState<ProposalDraft>({
    text: draftText(item, item.value), reason: "", evidence: "", effectiveFrom: "", apply: !twoPerson,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = draftProblem(item, draft);
  const set = (patch: Partial<ProposalDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const submit = async () => {
    if (problem || busy) return;
    setBusy(true);
    setError(null);
    const out = await post(proposalPath(item.key), proposalBody(item, item, draft), t);
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    toast.success(draft.apply ? t("applied", item.label) : t("proposed", item.label), 6000);
    onDone();
  };
  const bounds = boundsText(item);
  return (
    <Modal open onClose={onClose} title={t("proposeTitle", item.label)} width={560} pinFooter testId="settings-propose"
      footer={<>
        <Button variant="secondary" onClick={onClose}>{t("cancel")}</Button>
        <Button variant="primary" disabled={!!problem || busy || !canWrite} onClick={() => void submit()}
          testId="settings-propose-submit">
          {busy ? t("saving") : draft.apply ? t("proposeApply") : t("propose")}
        </Button>
      </>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <div style={small}>{item.description}</div>
        <div style={small}>
          {t("current")}: <strong style={{ fontFamily: mono }}>{formatValue(item.value, item)}</strong>
          {bounds && <> · {t("colBounds")}: {bounds}</>}
        </div>
        {item.value_type === "bool" ? (
          <Field label={t("newValue")}>
            <Select value={draft.text} onChange={(e) => set({ text: e.target.value })}
              options={[{ value: "on", label: t("on") }, { value: "off", label: t("off") }]} />
          </Field>
        ) : item.value_type === "enum" ? (
          <Field label={t("newValue")}>
            <Select value={draft.text} onChange={(e) => set({ text: e.target.value })}
              options={(item.allowed_values ?? []).map((v) => ({ value: String(v), label: String(v) }))} />
          </Field>
        ) : item.value_type === "json" || item.value_type === "list" ? (
          <Field label={t("newValue")} hint={t("jsonHint")}>
            <Textarea rows={7} value={draft.text} onChange={(e) => set({ text: e.target.value })}
              style={{ fontFamily: mono, fontSize: 12 }} />
          </Field>
        ) : (
          <Field label={item.unit ? `${t("newValue")} (${item.unit})` : t("newValue")} hint={bounds || undefined}>
            <Input inputMode="decimal" value={draft.text} onChange={(e) => set({ text: e.target.value })}
              data-testid="settings-propose-value" />
          </Field>
        )}
        <Field label={t("reason")} required hint={t("reasonHint")}>
          <Textarea rows={3} value={draft.reason} onChange={(e) => set({ reason: e.target.value })}
            data-testid="settings-propose-reason" />
        </Field>
        <Field label={t("evidence")} required={item.evidence_required}
          hint={item.evidence_required ? t("evidenceHint") : t("evidenceOptional")}>
          <Input value={draft.evidence} onChange={(e) => set({ evidence: e.target.value })} />
        </Field>
        <Field label={t("effectiveFrom")} hint={t("effectiveHint")}>
          <Input type="datetime-local" value={draft.effectiveFrom} onChange={(e) => set({ effectiveFrom: e.target.value })} />
        </Field>
        {twoPerson ? <div style={small}>{t("applyTwoPerson")}</div> : (
          <Checkbox checked={draft.apply} onChange={(e) => set({ apply: e.target.checked })}
            label={item.approval === "maker_checker" ? t("applySelf") : t("applyNow")} />
        )}
        {problem && (draft.reason || draft.text !== draftText(item, item.value)) &&
          <div role="alert" style={{ ...small, color: K.danger }}>{problem}</div>}
        {error && <div role="alert" style={{ ...small, color: K.danger }}>{error}</div>}
      </div>
    </Modal>
  );
}

function HistoryModal({ item, canApprove, onClose, onDone }: {
  item: SettingItem; canApprove: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useSettingsT();
  const toast = useToast();
  const q = useQuery({ queryKey: ["settings-history", item.key], queryFn: () => apiGet<HistoryResp>(historyPath(item.key)) });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = (q.data?.history ?? []).find((r) => r.status === "proposed");
  const decide = async (approve: boolean) => {
    if (!open || busy) return;
    setBusy(true);
    setError(null);
    const out = await post(decisionPath(open.id, approve), { note: note.trim() || null }, t);
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    toast.success(approve ? t("approved") : t("rejected"), 6000);
    onDone();
  };
  const columns = useMemo<ColumnDef<SettingRow, unknown>[]>(() => [
    { id: "version", header: t("colVersion"), accessorKey: "version", meta: { align: "right" } },
    { id: "status", header: t("colStatus"), accessorKey: "status",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
          <Badge tone={STATUS[r.status].tone} dot={false}>{t(STATUS[r.status].label)}</Badge>
          {r.self_approved && <Badge tone="warning" dot={false}>{t("selfApproved")}</Badge>}
        </span>
      ) },
    { id: "was", header: t("colWas"), accessorFn: (r) => formatValue(r.previous_value, item),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{String(getValue())}</span> },
    { id: "now", header: t("colNow"), accessorFn: (r) => formatValue(r.value, item),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{String(getValue())}</span> },
    { id: "who", header: t("colWho"), accessorFn: (r) => actorText(r.proposed_by),
      cell: ({ row: { original: r } }) => <span style={small}>{actorText(r.proposed_by)}<br />
        <DateText value={r.proposed_at} withTime /></span> },
    { id: "reason", header: t("colReason"), accessorKey: "reason",
      cell: ({ row: { original: r } }) => <span style={small}>{r.reason}{r.evidence_ref && <><br />{r.evidence_ref}</>}
      </span> },
    { id: "decided", header: t("colDecided"), accessorFn: (r) => r.decided_at ?? "",
      cell: ({ row: { original: r } }) => r.decided_at
        ? <span style={small}>{actorText(r.decided_by)}<br /><DateText value={r.decided_at} withTime />
          {r.decision_note && <><br />{r.decision_note}</>}</span>
        : <span style={small}>—</span> },
  ], [t, item]);
  return (
    <Modal open onClose={onClose} title={t("historyTitle", item.label)} width={880} pinFooter testId="settings-history"
      footer={<>
        {open && canApprove && <>
          <Button variant="secondary" disabled={busy || note.trim().length < 3} onClick={() => void decide(false)}>
            {t("reject")}</Button>
          <Button variant="primary" disabled={busy} onClick={() => void decide(true)}
            testId="settings-approve">{t("approve")}</Button>
        </>}
        <Button variant="secondary" onClick={onClose}>{t("close")}</Button>
      </>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <div style={small}>{t("keyLabel")}: <span style={{ fontFamily: mono }}>{item.key}</span>
          {item.env_override && <> · {t("envLabel", item.env_override)}</>}</div>
        {q.isError && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
          onRetry={() => void q.refetch()} />}
        {q.data && q.data.history.length === 0 && <div style={small}>{t("noHistory")}</div>}
        {q.data && q.data.history.length > 0 && (
          <DataTable<SettingRow> tableId="settings-history-table" data={q.data.history} columns={columns}
            getRowId={(r) => r.id} searchable={false} paginate={false} compact tableMinWidth={760} />
        )}
        {open && canApprove && (
          <Field label={t("note")} hint={t("noteHint")}>
            <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        )}
        {error && <div role="alert" style={{ ...small, color: K.danger }}>{error}</div>}
      </div>
    </Modal>
  );
}

function AppSettings({ app }: { app: SettingApp }) {
  const t = useSettingsT();
  const qc = useQueryClient();
  const { isAdmin } = useAdminIdentity();
  const q = useQuery({ queryKey: ["settings-registry", app], queryFn: () => apiGet<RegistryResp>(registryPath(app)) });
  const groups = useMemo(() => groupByArea(q.data?.settings ?? []), [q.data]);
  const [area, setArea] = useState<string>("");
  const [editing, setEditing] = useState<SettingItem | null>(null);
  const [viewing, setViewing] = useState<SettingItem | null>(null);
  const activeArea = groups.some((g) => g.area === area) ? area : groups[0]?.area ?? "";
  const rows = groups.find((g) => g.area === activeArea)?.items ?? [];
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["settings-registry", app] });
    void qc.invalidateQueries({ queryKey: ["settings-history"] });
  };
  const columns = useMemo<ColumnDef<SettingItem, unknown>[]>(() => [
    { id: "setting", header: t("colSetting"), accessorKey: "label",
      cell: ({ row: { original: s } }) => (
        <div style={{ display: "grid", gap: 4, minWidth: 220 }}>
          <strong style={{ fontSize: 13 }}>{s.label}</strong>
          <span style={{ ...small, fontFamily: mono, fontSize: 11 }}>{s.key}</span>
          <Badges item={s} />
          {s.env_ignored && <span style={{ ...small, color: K.danger }}>{t("envIgnored", s.env_ignored)}</span>}
        </div>
      ) },
    { id: "value", header: t("colValue"), accessorFn: (s) => formatValue(s.value, s),
      cell: ({ row: { original: s } }) => (
        <div style={{ display: "grid", gap: 2 }}>
          <strong style={{ fontFamily: mono, fontSize: 13, wordBreak: "break-word" }}>{formatValue(s.value, s)}</strong>
          <span style={small}>{sourceText(s, t)}</span>
        </div>
      ) },
    { id: "default", header: t("colDefault"), accessorFn: (s) => formatValue(s.default_value, s),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12, wordBreak: "break-word" }}>
        {String(getValue())}</span> },
    { id: "bounds", header: t("colBounds"), accessorFn: (s) => boundsText(s),
      cell: ({ getValue }) => <span style={small}>{String(getValue()) || "—"}</span> },
    { id: "last", header: t("colLast"), accessorFn: (s) => s.last_change?.decided_at ?? "",
      cell: ({ row: { original: s } }) => s.last_change
        ? <span style={small}>{actorText(s.last_change.decided_by)}, <DateText value={s.last_change.decided_at} withTime />
          <br />{s.last_change.reason}</span>
        : <span style={small}>{t("never")}</span> },
    { id: "actions", header: t("colActions"), enableSorting: false,
      cell: ({ row: { original: s } }) => (
        <span style={{ display: "inline-flex", gap: 6 }}>
          {isAdmin && <Button size="sm" variant="secondary" onClick={() => setEditing(s)}
            testId={`settings-change-${s.key}`}><Pencil size={12} /> {t("change")}</Button>}
          <Button size="sm" variant="secondary" onClick={() => setViewing(s)}>
            <History size={12} /> {s.proposals.length ? t("review") : t("history")}</Button>
        </span>
      ) },
  ], [t, isAdmin]);

  if (app === "booking") {
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
  const status = (q.error as { status?: number } | null)?.status;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
      {q.isError && (status === 503
        ? <Section><div style={small}>{t("notMigrated")}</div></Section>
        : <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => void q.refetch()} />)}
      {q.data && (
        <div style={small} data-testid="settings-two-person">
          {q.data.two_person_approval ? t("twoPersonOn") : t("twoPersonOff")}
          {q.data.reader_error && <><br /><span style={{ color: K.danger }}>{t("readerError", q.data.reader_error)}</span></>}
        </div>
      )}
      {q.data && groups.length === 0 && <Section><div style={small}>{t("noneYet")}</div></Section>}
      {groups.length > 0 && (
        <Tabs variant="underline" ariaLabel={t("title")} active={activeArea} onChange={setArea}
          tabs={groups.map((g) => ({ key: g.area, label: g.area, count: g.items.length }))} testId="settings-areas" />
      )}
      {rows.length > 0 && (
        <DataTable<SettingItem> tableId={`settings-${app}-${activeArea}`} data={rows} columns={columns}
          getRowId={(s) => s.key} searchable paginate={false} tableMinWidth={980} />
      )}
      {editing && (
        <ProposeModal item={editing} twoPerson={!!q.data?.two_person_approval} canWrite={isAdmin}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      )}
      {viewing && (
        <HistoryModal item={viewing} canApprove={isAdmin} onClose={() => setViewing(null)}
          onDone={() => { setViewing(null); refresh(); }} />
      )}
    </div>
  );
}

export default function SettingsRegistry() {
  const t = useSettingsT();
  const [app, setApp] = useState<SettingApp>("cis");
  return (
    <PageFrame>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <SlidersHorizontal size={18} /> {t("title")}</span>}
        description={t("intro")}
      />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        <Tabs ariaLabel={t("title")} active={app} onChange={(k) => setApp(k as SettingApp)} testId="settings-apps"
          tabs={APPS.map((a) => ({ key: a, label: t(APP_LABEL[a]) }))} />
        <AppSettings key={app} app={app} />
      </div>
    </PageFrame>
  );
}

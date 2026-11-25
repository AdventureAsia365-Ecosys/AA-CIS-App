"use client";
// app/admin/overview/_status/Incidents.tsx — AA-1142 (P2): the incident record. "Declare incident" from a
// Needs-attention alert (or by hand): title, SEV, app, summary; the person who declares owns it. The list shows open
// incidents first (resolved ones on request); "Open" shows its timeline (its own updates, the events of its app from
// cis.ops.status_incident_lookback_min before it was declared, the acks of its alert), the update controls (status,
// SEV, take over, note) and "Write lesson" (the lesson log of the root repo, with a draft line to copy).
// GET/POST /api/admin/overview/incidents[/{id}] (reads ops.read; writes ops.write and never the business owner: the
// status answer's viewer.can_ack says whether to show the controls). Every write is audited by the API. Kit only.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Badge, Button, DataTable, Drawer, Field, Input, K, LinkButton, Modal, Select, Textarea, apiErrorMessage, apiGet,
  apiSend, useFormat, useToast, type ColumnDef, type Tone,
} from "../../../_kit";
import { levelTone, type AttentionItem, type StatusResp } from "@/lib/opsStatus";
import {
  INCIDENTS_QUERY_KEY, INCIDENT_STATUSES, SEVERITIES, incidentTone, titleFromItem, titleOk, type Incident,
  type IncidentDetail, type IncidentList, type IncidentStatus, type TimelineRow,
} from "@/lib/opsStatusP2";
import { Card, rowWrap, small } from "./ui";
import { useWords } from "./words";
import { useP2 } from "./Slo";

type PT = ReturnType<typeof useP2>;
const APPS = ["cis", "tripplanner", "booking", "crm", "shared"] as const;
const BY_HAND = "__hand__";

function errorOf(e: unknown): { text: string; incidentId: number | null } {
  const err = e as { status?: number; body?: { detail?: { incident_id?: number } }; message?: string };
  const id = err?.body?.detail?.incident_id;
  return { text: err?.body !== undefined ? apiErrorMessage(err.body, err.status ?? 0) : err?.message ?? "error",
    incidentId: typeof id === "number" ? id : null };
}

function statusWord(s: IncidentStatus, t: PT): string {
  return t(s);
}

function DeclareDialog({ items, range, onClose }: { items: AttentionItem[]; range: string; onClose: () => void }) {
  const t = useP2();
  const toast = useToast();
  const qc = useQueryClient();
  const first = items[0];
  const [itemId, setItemId] = useState<string>(first ? first.id : BY_HAND);
  const item = items.find((i) => i.id === itemId);
  const [title, setTitle] = useState(titleFromItem(first?.title));
  const [sev, setSev] = useState<string>(first && SEVERITIES.includes(first.severity as never) ? first.severity : "sev2");
  const [app, setApp] = useState<string>(first?.app ?? "shared");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const pick = (id: string) => {
    setItemId(id);
    const it = items.find((i) => i.id === id);
    if (it) {
      setTitle(titleFromItem(it.title));
      if (SEVERITIES.includes(it.severity as never)) setSev(it.severity);
      setApp(it.app);
    }
  };
  const save = async () => {
    setBusy(true);
    try {
      await apiSend("/api/admin/overview/incidents", { method: "POST", admin: true, body: {
        title: title.trim(), severity: sev, app, summary: summary.trim() || null,
        item_id: item ? item.id : null, range: range === "custom" ? "24h" : range } });
      toast.success(t("declared"));
      await qc.invalidateQueries({ queryKey: [INCIDENTS_QUERY_KEY] });
      onClose();
    } catch (e) {
      const err = errorOf(e);
      toast.error(err.incidentId ? t("alreadyOpen", err.incidentId) : t("failed", err.text));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} title={t("declareTitle")} testId="status-incident-declare-dialog"
      footer={<span style={rowWrap}>
        <Button variant="ghost" onClick={onClose}>{t("cancel")}</Button>
        <Button disabled={!titleOk(title) || busy} onClick={save} testId="status-incident-declare-save">
          {busy ? t("saving") : t("declareSave")}</Button>
      </span>}>
      <div style={{ display: "grid", gap: 12 }}>
        <Field label={t("fromAlert")}>
          <Select value={itemId} onChange={(e) => pick(e.target.value)} data-testid="status-incident-item"
            options={[...items.map((i) => ({ value: i.id, label: `${i.severity.toUpperCase()} · ${i.title}` })),
              { value: BY_HAND, label: t("byHand") }]} />
        </Field>
        <Field label={t("fTitle")} required hint={t("fTitleHint")}>
          <Input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)}
            data-testid="status-incident-title" />
        </Field>
        <span style={{ ...rowWrap, gap: 12 }}>
          <Field label={t("fSev")}>
            <Select value={sev} onChange={(e) => setSev(e.target.value)} data-testid="status-incident-sev"
              options={SEVERITIES.map((s) => ({ value: s, label: s.toUpperCase() }))} />
          </Field>
          <Field label={t("fApp")}>
            <Select value={app} onChange={(e) => setApp(e.target.value)}
              options={APPS.map((a) => ({ value: a, label: a }))} />
          </Field>
        </span>
        <Field label={t("fSummary")}>
          <Textarea rows={3} value={summary} maxLength={2000} onChange={(e) => setSummary(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

type When = (s: string | null | undefined) => string;
const UPDATE_TEXT: Readonly<Record<string, (r: TimelineRow, t: PT, by: string) => string>> = {
  declared: (r, t, by) => t("tlDeclared", by, (r.to ?? "").toUpperCase()),
  status: (r, t, by) => t("tlStatus", r.from ?? "", r.to ?? "", by),
  severity: (r, t, by) => t("tlSeverity", (r.from ?? "").toUpperCase(), (r.to ?? "").toUpperCase(), by),
  owner: (_r, t, by) => t("tlOwner", by),
};

function ackText(r: TimelineRow, t: PT, by: string, when: When): string {
  return t("tlAck", r.type, by, when(r.until)) + (r.note ? `: ${r.note}` : "");
}

function timelineText(r: TimelineRow, t: PT, when: When): string {
  const by = r.by_name ?? t("someone");
  if (r.kind === "event") return r.text ?? r.type;
  if (r.kind === "ack") return ackText(r, t, by, when);
  const build = UPDATE_TEXT[r.type];
  return build ? build(r, t, by) : `${t("tlNote", by)}: ${r.note ?? ""}`;
}

function UpdateForm({ inc, onDone }: { inc: IncidentDetail; onDone: () => void }) {
  const t = useP2();
  const toast = useToast();
  const [status, setStatus] = useState<IncidentStatus>(inc.status);
  const [sev, setSev] = useState<string>(inc.severity);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (takeOver: boolean) => {
    setBusy(true);
    try {
      await apiSend(`/api/admin/overview/incidents/${inc.incident_id}`, { method: "POST", admin: true, body: {
        status: status === inc.status ? null : status, severity: sev === inc.severity ? null : sev,
        take_over: takeOver, note: note.trim() || null } });
      toast.success(t("updated"));
      setNote("");
      onDone();
    } catch (e) {
      toast.error(t("failed", errorOf(e).text));
    } finally {
      setBusy(false);
    }
  };
  const changed = status !== inc.status || sev !== inc.severity || note.trim().length > 0;
  return (
    <div style={{ display: "grid", gap: 10, borderTop: `1px solid ${K.line}`, paddingTop: 10 }}>
      <b style={{ color: K.ink }}>{t("update")}</b>
      <span style={{ ...rowWrap, gap: 12 }}>
        <Field label={t("colStatus")}>
          <Select value={status} onChange={(e) => setStatus(e.target.value as IncidentStatus)}
            data-testid="status-incident-status" options={INCIDENT_STATUSES.map((s) => ({ value: s,
              label: statusWord(s, t) }))} />
        </Field>
        <Field label={t("fSev")}>
          <Select value={sev} onChange={(e) => setSev(e.target.value)}
            options={SEVERITIES.map((s) => ({ value: s, label: s.toUpperCase() }))} />
        </Field>
      </span>
      <Field label={t("note")}>
        <Textarea rows={2} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)}
          data-testid="status-incident-note" />
      </Field>
      <span style={rowWrap}>
        <Button size="sm" disabled={!changed || busy} onClick={() => send(false)} testId="status-incident-save">
          {busy ? t("saving") : t("saveUpdate")}</Button>
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => send(true)}
          testId="status-incident-take-over">{t("takeOver")}</Button>
      </span>
    </div>
  );
}

function IncidentDrawer({ id, canWrite, onClose }: { id: number; canWrite: boolean; onClose: () => void }) {
  const t = useP2();
  const toast = useToast();
  const qc = useQueryClient();
  const { formatDateTime } = useFormat();
  const res = useQuery({ queryKey: [INCIDENTS_QUERY_KEY, id],
    queryFn: () => apiGet<IncidentDetail>(`/api/admin/overview/incidents/${id}`, { admin: true }) });
  const inc = res.data;
  const when = (s: string | null | undefined) => (s ? formatDateTime(s) : "—");
  const copy = async () => {
    if (!inc) return;
    try {
      await navigator.clipboard.writeText(inc.lesson_draft);
      toast.success(t("copied"));
    } catch {
      toast.error(t("failed", "clipboard"));
    }
  };
  return (
    <Drawer open onClose={onClose} width={560} testId="status-incident-drawer"
      title={inc ? t("incHeading", inc.incident_id, inc.title) : t("incTitle")}>
      {!inc ? <span style={small}>{res.isError ? t("loadFailed") : "…"}</span> : (
        <div style={{ display: "grid", gap: 12 }}>
          <span style={rowWrap}>
            <Badge tone={levelTone(inc.severity) as Tone}>{inc.severity.toUpperCase()}</Badge>
            <Badge tone={incidentTone(inc.status)}>{statusWord(inc.status, t)}</Badge>
            <span style={small}>{t("colOwner")}: {inc.owner_name ?? t("someone")}</span>
          </span>
          {inc.summary && <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{inc.summary}</span>}
          <span style={small}>{t("windowNote", inc.window.lookback_min)}</span>
          {inc.timeline.length === 0 ? <span style={small}>{t("noTimeline")}</span> : (
            <ol style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }} data-testid="status-incident-timeline">
              {inc.timeline.map((r, i) => (
                <li key={`${r.kind}-${i}`} style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>
                  <span style={small}>{when(r.t)} · </span>{timelineText(r, t, when)}
                </li>
              ))}
            </ol>)}
          <span style={{ ...rowWrap, borderTop: `1px solid ${K.line}`, paddingTop: 10 }}>
            <a href={inc.lessons_url} target="_blank" rel="noreferrer" style={{ color: K.accentDeep, fontWeight: 600,
              fontSize: 13 }} data-testid="status-incident-lesson">{t("writeLesson")}</a>
            <LinkButton size="sm" onClick={copy} testId="status-incident-copy-lesson">{t("copyDraft")}</LinkButton>
          </span>
          <span style={small}>{t("lessonHint")}</span>
          <code style={{ ...small, overflowWrap: "anywhere" }}>{inc.lesson_draft}</code>
          {canWrite && <UpdateForm inc={inc} onDone={async () => {
            await qc.invalidateQueries({ queryKey: [INCIDENTS_QUERY_KEY] });
          }} />}
        </div>)}
    </Drawer>
  );
}

function columns(t: PT, when: (s: string | null) => string, open: (id: number) => void,
  app: (id: string) => string): ColumnDef<Incident>[] {
  return [
    { id: "inc", header: t("colInc"), accessorFn: (r) => r.incident_id,
      cell: ({ row }) => <b style={{ color: K.ink, overflowWrap: "anywhere" }}>
        #{row.original.incident_id} {row.original.title}</b> },
    { id: "sev", header: t("colSev"), accessorFn: (r) => r.severity,
      cell: ({ row }) => <Badge tone={levelTone(row.original.severity) as Tone}>
        {row.original.severity.toUpperCase()}</Badge> },
    { id: "status", header: t("colStatus"), accessorFn: (r) => r.status,
      cell: ({ row }) => <Badge tone={incidentTone(row.original.status)}>{statusWord(row.original.status, t)}</Badge> },
    { id: "app", header: t("fApp"), accessorFn: (r) => r.app ?? "", cell: ({ row }) => (row.original.app
      ? app(row.original.app) : "—") },
    { id: "owner", header: t("colOwner"), accessorFn: (r) => r.owner_name ?? "",
      cell: ({ row }) => row.original.owner_name ?? t("someone") },
    { id: "declared", header: t("colDeclared"), accessorFn: (r) => r.declared_at,
      cell: ({ row }) => <span style={small}>{when(row.original.declared_at)}</span> },
    { id: "open", header: "", enableSorting: false,
      cell: ({ row }) => <LinkButton size="sm" onClick={() => open(row.original.incident_id)}
        testId={`status-incident-open-${row.original.incident_id}`}>{t("openIncident")}</LinkButton> },
  ];
}

function IncidentTable({ data, isError, onOpen }: {
  data: IncidentList | undefined; isError: boolean; onOpen: (id: number) => void;
}) {
  const t = useP2();
  const w = useWords();
  const { formatDateTime } = useFormat();
  const [showResolved, setShowResolved] = useState(false);
  if (isError) return <span style={small}>{t("loadFailed")}</span>;
  const all = data?.incidents ?? [];
  const rows = all.filter((i) => showResolved || i.status !== "resolved");
  const when = (s: string | null) => (s ? formatDateTime(s) : "—");
  return (
    <>
      {rows.length === 0 ? <span style={small} data-testid="status-incidents-none">{t("incNone")}</span> : (
        <DataTable<Incident> tableId="status-incidents" data={rows} getRowId={(r) => String(r.incident_id)}
          columns={columns(t, when, onOpen, w.app)} searchable={false} paginate={false} compact
          tableMinWidth={720} />)}
      {all.some((i) => i.status === "resolved") && (
        <span><LinkButton size="sm" tone="muted" onClick={() => setShowResolved((v) => !v)}
          aria-pressed={showResolved}>{t("showResolved")}</LinkButton></span>)}
    </>
  );
}

/** The incidents card; the controls show only when the status answer says this viewer may act (viewer.can_ack). */
export function IncidentsCard({ data }: { data: StatusResp }) {
  const t = useP2();
  const canWrite = data.viewer?.can_ack === true;
  const [declaring, setDeclaring] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const res = useQuery({ queryKey: [INCIDENTS_QUERY_KEY],
    queryFn: () => apiGet<IncidentList>("/api/admin/overview/incidents", { admin: true }), refetchInterval: 60_000 });
  const alerts = data.verdict.attention.filter((i) => i.status === "open");
  return (
    <Card title={t("incTitle")} testId="status-incidents"
      actions={canWrite ? <Button size="sm" variant="secondary" onClick={() => setDeclaring(true)}
        testId="status-incident-declare">{t("declare")}</Button> : undefined}>
      {!canWrite && <span style={small}>{t("incReadOnly")}</span>}
      <IncidentTable data={res.data} isError={res.isError} onOpen={setOpenId} />
      {declaring && <DeclareDialog items={alerts} range={data.range.key} onClose={() => setDeclaring(false)} />}
      {openId != null && <IncidentDrawer id={openId} canWrite={canWrite} onClose={() => setOpenId(null)} />}
    </Card>
  );
}

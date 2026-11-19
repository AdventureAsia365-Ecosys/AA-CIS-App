"use client";

// app/admin/_components/TourDraftEditor.tsx — AA-1146: the Rewrite tab of the tour drawer edits a DRAFT version, never
// the live text. Save draft (PUT /api/admin/tours/{id}/draft, reason required) → the diff against the live version →
// Run checks (the existing re-validation of that version) → Publish (POST …/draft/publish: the one gate, which locks
// the published text against automatic rewrites). Unlock needs master.admin. Before the database update the API answers
// 503 and the drawer keeps its old inline editor (`fallback`). Buttons follow can() (the backend re-checks every call).

import React, { useCallback, useEffect, useState } from "react";
import {
  K, Badge, Button, Input, Textarea, mono, useAdminIdentity, useFormat, useT, useToast, type Translator,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { FIELD } from "../review/messages";
import { D } from "./tourDraft.messages";

type FieldKey =
  | "aa_name" | "aa_subtitle" | "aa_summary" | "aa_description" | "aa_highlights" | "aa_itineraries"
  | "mobile_card_text" | "seo_title" | "seo_meta" | "seo_keywords_used";

// Labels: the Review Queue's field catalogue (FIELD, English and Vietnamese). List fields take one item per line.
const FIELDS: { key: FieldKey; rows?: number; list?: boolean; limit?: number }[] = [
  { key: "aa_name" },
  { key: "aa_subtitle" },
  { key: "aa_summary", rows: 5 },
  { key: "aa_description", rows: 5 },
  { key: "aa_highlights", rows: 4, list: true },
  { key: "aa_itineraries", rows: 12 },
  { key: "mobile_card_text", limit: 80 },
  { key: "seo_title", limit: 70 },
  { key: "seo_meta", rows: 3, limit: 170 },
  { key: "seo_keywords_used", rows: 3, list: true },
];

type Fields = Partial<Record<FieldKey | "og_tags", unknown>>;
type Texts = Record<FieldKey, string>;
interface DiffOp { op: "equal" | "insert" | "delete"; text: string }
interface Draft {
  id: string; version_num: number; reason: string | null; editors: string[]; checks_passed: boolean | null;
  fields: Fields;
}
interface DraftState {
  live: { id: string; version_num: number; fields: Fields } | null;
  draft: Draft | null;
  diff: { field: string; ops: DiffOp[] }[];
  lock: { at: string | null; by: string | null; reason: string | null } | null;
  two_person_publish: boolean;
}
type Busy = "" | "save" | "checks" | "publish" | "discard" | "unlock";
type T = Translator<typeof D.en>;

function asText(v: unknown, list?: boolean): string {
  if (v == null) return "";
  if (Array.isArray(v)) return v.map(String).join("\n");
  if (!list || typeof v !== "string") return String(v);
  try {
    const p = JSON.parse(v);
    return Array.isArray(p) ? p.map(String).join("\n") : v;
  } catch { return v; }
}

function toValue(text: string, list?: boolean): unknown {
  return list ? text.split("\n").map(s => s.trim()).filter(Boolean) : text;
}

function errText(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  const m = (d as { message?: unknown } | undefined)?.message;
  return typeof m === "string" ? m : fallback;
}

/** The texts a state starts the form with: the draft's fields, else the live version's. */
function textsOf(s: DraftState): Texts {
  const src = s.draft?.fields ?? s.live?.fields ?? {};
  return Object.fromEntries(FIELDS.map(f => [f.key, asText(src[f.key], f.list)])) as Texts;
}

function reasonOk(reason: string): boolean {
  const r = reason.trim().length;
  return r >= 3 && r <= 500;
}

function checksTone(passed: boolean | null): string {
  if (passed === true) return K.success;
  return passed === false ? K.danger : K.muted;
}

function checksKey(passed: boolean | null): "checksPassed" | "checksFailed" | "checksPending" {
  if (passed === true) return "checksPassed";
  return passed === false ? "checksFailed" : "checksPending";
}

const NOTE: React.CSSProperties = { fontSize: 12, color: K.muted, margin: "0 0 12px" };

function Note({ show, text }: { show: boolean; text: string }) {
  return show ? <p style={NOTE}>{text}</p> : null;
}

function DraftHeader({ state, canUnlock, busy, onUnlock, t }: {
  state: DraftState; canUnlock: boolean; busy: Busy; onUnlock: () => void; t: T;
}) {
  const { formatDateTime } = useFormat();
  const lock = state.lock;
  return (
    <>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <Badge tone="neutral">{state.live ? t("liveVersion", state.live.version_num) : t("noLive")}</Badge>
        {state.draft && <Badge tone="warning">{t("draftBanner", state.draft.version_num)}</Badge>}
        {lock && <Badge tone="accent">{t("locked")}</Badge>}
        {lock && canUnlock && <Button variant="secondary" onClick={onUnlock} disabled={busy !== ""}>{t("unlock")}</Button>}
      </div>
      {lock && <p style={NOTE}>{t("lockedBy", lock.by ?? "—", lock.at ? formatDateTime(lock.at) : "—")}</p>}
    </>
  );
}

function FieldBox({ f, value, readOnly, onChange }: {
  f: (typeof FIELDS)[number]; value: string; readOnly: boolean; onChange: (v: string) => void;
}) {
  const tf = useT(FIELD);
  const over = f.limit !== undefined && value.length > f.limit;
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, fontWeight: 600, color: K.muted,
        textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>
        <span>{tf(f.key)}</span>
        {f.limit && <span style={{ color: over ? K.danger : K.muted2 }}>{value.length}/{f.limit}</span>}
      </div>
      {f.rows ? (
        <Textarea value={value} rows={f.rows} readOnly={readOnly} onChange={e => onChange(e.target.value)}
          style={{ width: "100%", fontFamily: f.list ? mono : undefined, fontSize: 13 }} />
      ) : (
        <Input value={value} readOnly={readOnly} onChange={e => onChange(e.target.value)} style={{ width: "100%" }} />
      )}
    </div>
  );
}

interface Actions { save: () => void; checks: () => void; refresh: () => void; publish: () => void; discard: () => void }

function DraftActions({ draft, canEdit, canPublish, dirty, reasonReady, busy, on, t }: {
  draft: Draft | null; canEdit: boolean; canPublish: boolean; dirty: boolean; reasonReady: boolean; busy: Busy;
  on: Actions; t: T;
}) {
  const idle = busy === "";
  const passed = draft?.checks_passed === true;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
      {canEdit && (
        <Button variant="primary" onClick={on.save} disabled={!idle || !reasonReady || (!dirty && !!draft)}>
          {busy === "save" ? t("savingDraft") : t("saveDraft")}
        </Button>
      )}
      {draft && <DraftOnlyActions canEdit={canEdit} canPublish={canPublish} idle={idle} dirty={dirty} passed={passed}
        publishing={busy === "publish"} on={on} t={t} />}
    </div>
  );
}

function DraftOnlyActions({ canEdit, canPublish, idle, dirty, passed, publishing, on, t }: {
  canEdit: boolean; canPublish: boolean; idle: boolean; dirty: boolean; passed: boolean; publishing: boolean;
  on: Actions; t: T;
}) {
  return (
    <>
      {canEdit && <Button variant="secondary" onClick={on.checks} disabled={!idle || dirty}>{t("runChecks")}</Button>}
      <Button variant="secondary" onClick={on.refresh} disabled={!idle}>{t("refresh")}</Button>
      {canPublish && (
        <Button variant="primary" onClick={on.publish} disabled={!idle || dirty || !passed}
          title={passed ? undefined : t("publishNeedsChecks")}>
          {publishing ? t("publishing") : t("publish")}
        </Button>
      )}
      {canEdit && <Button variant="secondary" onClick={on.discard} disabled={!idle}>{t("discard")}</Button>}
    </>
  );
}

function DiffView({ diff, t }: { diff: DraftState["diff"]; t: T }) {
  const tf = useT(FIELD);
  return (
    <div style={{ borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em",
        marginBottom: 8 }}>{t("diffTitle")}</div>
      {diff.length === 0 && <div style={{ fontSize: 12, color: K.muted }}>{t("noChanges")}</div>}
      {diff.map(d => (
        <div key={d.field} style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>{tf(d.field as FieldKey)}</div>
          <div style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap", color: K.body }}>
            {d.ops.map((o, i) => <DiffOpSpan key={i} op={o} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

function DiffOpSpan({ op }: { op: DiffOp }) {
  if (op.op === "delete") return <del style={{ background: K.dangerTint, color: K.danger }}>{op.text}</del>;
  if (op.op === "insert") {
    return <ins style={{ background: K.successSoft, color: K.success, textDecoration: "none" }}>{op.text}</ins>;
  }
  return <span>{op.text}</span>;
}

/** The draft state of a tour: loaded on open and on reload(); `notReady` = the versions API answers 503. */
function useDraftState(tourId: string, t: T) {
  const [state, setState] = useState<DraftState | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [error, setError] = useState("");
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound(n => n + 1), []);
  useEffect(() => {
    let alive = true;
    fetch(`/api/admin/tours/${tourId}/draft`)
      .then(async r => {
        if (!alive) return;
        if (r.status === 503) { setNotReady(true); return; }
        if (!r.ok) { setError(t("loadFailed")); return; }
        const body = await r.json();
        if (alive) { setError(""); setState(body); }
      })
      .catch(() => { if (alive) setError(t("loadFailed")); });
    return () => { alive = false; };
  }, [tourId, t, round]);
  return { state, notReady, error, reload };
}

export default function TourDraftEditor({ tourId, fallback }: { tourId: string; fallback: React.ReactNode }) {
  const t: T = useT(D);
  const { state, notReady, error, reload } = useDraftState(tourId, t);
  if (notReady) return <>{fallback}</>;
  if (error) return <div style={{ padding: 24, color: K.danger }}>{error}</div>;
  if (!state) return <div style={{ padding: 24, color: K.muted }}>{t("loading")}</div>;
  return <DraftForm key={draftKey(state)} tourId={tourId} initial={state} reload={reload} t={t} />;
}

function draftKey(s: DraftState): string {
  return s.draft ? `${s.draft.id}:${s.draft.reason ?? ""}` : `live:${s.live?.id ?? "none"}`;
}

function useDraftCalls(tourId: string, t: T, setBusy: (b: Busy) => void) {
  const toast = useToast();
  return async function call(kind: Busy, url: string, init: RequestInit, ok: (body: unknown) => void) {
    setBusy(kind);
    try {
      const r = await fetch(url, init);
      const body = await r.json().catch(() => ({}));
      if (r.ok) ok(body);
      else toast.error(errText(body, t("failed")), 5000);
    } finally { setBusy(""); }
  };
}

function DraftForm({ tourId, initial, reload, t }: { tourId: string; initial: DraftState; reload: () => void; t: T }) {
  const toast = useToast();
  const { role } = useAdminIdentity();
  const [state, setState] = useState<DraftState>(initial);
  const [texts, setTexts] = useState<Texts>(() => textsOf(initial));
  const [reason, setReason] = useState(initial.draft?.reason ?? "");
  const [busy, setBusy] = useState<Busy>("");
  const call = useDraftCalls(tourId, t, setBusy);
  const canEdit = can(role, "content.write");
  const base = textsOf(state);
  const changed = FIELDS.filter(f => texts[f.key] !== base[f.key]);
  const draft = state.draft;
  const url = `/api/admin/tours/${tourId}`;
  const json = { "Content-Type": "application/json" };
  const done = (key: "discarded" | "unlocked") => () => { toast.success(t(key), 3000); reload(); };
  const on: Actions = {
    save: () => call("save", `${url}/draft`, { method: "PUT", headers: json, body: JSON.stringify(
      { reason, fields: Object.fromEntries(changed.map(f => [f.key, toValue(texts[f.key], f.list)])) }) },
    body => { setState(body as DraftState); toast.success(t("draftSaved"), 3000); }),
    checks: () => call("checks", `${url}/generated/${draft?.id}/revalidate`, { method: "POST" },
      () => toast.success(t("checksQueued"), 4000)),
    refresh: reload,
    publish: () => call("publish", `${url}/draft/publish`, { method: "POST" }, body => {
      toast.success(t("published", (body as { version_num: number }).version_num), 5000);
      reload();
    }),
    discard: () => { if (confirm(t("discardConfirm"))) call("discard", `${url}/draft`, { method: "DELETE" }, done("discarded")); },
  };
  const unlock = () => {
    const why = (prompt(t("unlockPrompt")) ?? "").trim();
    if (why.length >= 3) {
      call("unlock", `${url}/unlock`, { method: "POST", headers: json, body: JSON.stringify({ reason: why }) },
        done("unlocked"));
    }
  };
  return (
    <div>
      <DraftHeader state={state} canUnlock={can(role, "master.admin")} busy={busy} onUnlock={unlock} t={t} />
      <DraftNotes state={state} canEdit={canEdit} t={t} />
      {FIELDS.map(f => (
        <FieldBox key={f.key} f={f} value={texts[f.key]} readOnly={!canEdit}
          onChange={v => setTexts(x => ({ ...x, [f.key]: v }))} />
      ))}
      {canEdit && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>{t("reason")}</div>
          <Input value={reason} onChange={e => setReason(e.target.value)} maxLength={500} style={{ width: "100%" }}
            placeholder={t("reasonHint")} />
        </div>
      )}
      <DraftActions draft={draft} canEdit={canEdit} canPublish={can(role, "content.publish") || can(role, "review.act")}
        dirty={changed.length > 0} reasonReady={reasonOk(reason)} busy={busy} on={on} t={t} />
      {draft && <DraftChecks draft={draft} diff={state.diff} t={t} />}
    </div>
  );
}

function DraftNotes({ state, canEdit, t }: { state: DraftState; canEdit: boolean; t: T }) {
  const editors = state.draft?.editors ?? [];
  return (
    <>
      <Note show={!state.draft} text={t("noDraft")} />
      <Note show={editors.length > 0} text={t("editedBy", editors.join(", "))} />
      <Note show={state.two_person_publish} text={t("twoPerson")} />
      <Note show={!canEdit} text={t("readOnly")} />
    </>
  );
}

function DraftChecks({ draft, diff, t }: { draft: Draft; diff: DraftState["diff"]; t: T }) {
  return (
    <>
      <div style={{ fontSize: 12, marginBottom: 12, color: checksTone(draft.checks_passed) }}>
        {t(checksKey(draft.checks_passed))}
      </div>
      <DiffView diff={diff} t={t} />
    </>
  );
}

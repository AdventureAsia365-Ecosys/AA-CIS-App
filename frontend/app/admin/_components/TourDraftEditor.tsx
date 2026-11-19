"use client";

// app/admin/_components/TourDraftEditor.tsx — AA-1146: the Rewrite tab of the tour drawer edits a DRAFT version, never
// the live text. Save draft (PUT /api/admin/tours/{id}/draft, reason required) → the diff against the live version →
// Run checks (the existing re-validation of that version) → Publish (POST …/draft/publish: the one gate, which locks
// the published text against automatic rewrites). Unlock needs master.admin. Before the database update the API answers
// 503 and the drawer keeps its old inline editor (`fallback`). Buttons follow can() (the backend re-checks every call).

import React, { useCallback, useEffect, useState } from "react";
import { K, Badge, Button, Input, Textarea, mono, useAdminIdentity, useFormat, useT, useToast } from "../../_kit";
import { can } from "@/lib/rbac";
import { FIELD } from "../review/messages";
import { D } from "./tourDraft.messages";

type FieldKey =
  | "aa_name" | "aa_subtitle" | "aa_summary" | "aa_description" | "aa_highlights" | "aa_itineraries"
  | "mobile_card_text" | "seo_title" | "seo_meta" | "seo_keywords_used";

// Edited here, in reading order. og_tags stays read-only (as in the Review Queue).
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
interface DiffOp { op: "equal" | "insert" | "delete"; text: string }
interface DraftState {
  live: { id: string; version_num: number; fields: Fields } | null;
  draft: {
    id: string; version_num: number; reason: string | null; editors: string[]; checks_passed: boolean | null;
    fields: Fields;
  } | null;
  diff: { field: string; ops: DiffOp[] }[];
  lock: { at: string | null; by: string | null; reason: string | null } | null;
  two_person_publish: boolean;
}

function asText(v: unknown, list?: boolean): string {
  if (v == null) return "";
  if (Array.isArray(v)) return v.map(String).join("\n");
  if (list && typeof v === "string") {
    try { const p = JSON.parse(v); if (Array.isArray(p)) return p.map(String).join("\n"); } catch { /* text */ }
  }
  return String(v);
}

function toValue(text: string, list?: boolean): unknown {
  return list ? text.split("\n").map(s => s.trim()).filter(Boolean) : text;
}

function errText(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  const m = (d as { message?: unknown })?.message;
  return typeof m === "string" ? m : fallback;
}

export default function TourDraftEditor({ tourId, fallback }: { tourId: string; fallback: React.ReactNode }) {
  const t = useT(D);
  const tf = useT(FIELD);
  const toast = useToast();
  const { role } = useAdminIdentity();
  const { formatDateTime } = useFormat();
  const [state, setState] = useState<DraftState | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [error, setError] = useState("");
  const [texts, setTexts] = useState<Record<FieldKey, string>>({} as Record<FieldKey, string>);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "checks" | "publish" | "discard" | "unlock">("");

  const canEdit = can(role, "content.write");
  const canPublish = can(role, "content.publish") || can(role, "review.act");
  const canUnlock = can(role, "master.admin");

  const apply = useCallback((s: DraftState) => {
    setState(s);
    const src = s.draft?.fields ?? s.live?.fields ?? {};
    const next = {} as Record<FieldKey, string>;
    for (const f of FIELDS) next[f.key] = asText(src[f.key], f.list);
    setTexts(next);
    setReason(s.draft?.reason ?? "");
  }, []);

  // A reload is a new `round` (the fetch runs in the effect, its answer set from the promise).
  const [round, setRound] = useState(0);
  const load = useCallback(() => setRound(n => n + 1), []);

  useEffect(() => {
    let alive = true;
    fetch(`/api/admin/tours/${tourId}/draft`)
      .then(async r => {
        if (!alive) return;
        if (r.status === 503) { setNotReady(true); return; }
        if (!r.ok) { setError(t("loadFailed")); return; }
        const body = await r.json();
        if (alive) { setError(""); apply(body); }
      })
      .catch(() => { if (alive) setError(t("loadFailed")); });
    return () => { alive = false; };
  }, [tourId, apply, t, round]);

  if (notReady) return <>{fallback}</>;
  if (error) return <div style={{ padding: 24, color: K.danger }}>{error}</div>;
  if (!state) return <div style={{ padding: 24, color: K.muted }}>{t("loading")}</div>;

  const base = state.draft?.fields ?? state.live?.fields ?? {};
  const changed = FIELDS.filter(f => texts[f.key] !== asText(base[f.key], f.list));
  const draft = state.draft;

  async function call(kind: typeof busy, url: string, init: RequestInit, ok: (body: unknown) => void) {
    setBusy(kind);
    try {
      const r = await fetch(url, init);
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { toast.error(errText(body, t("failed")), 5000); return; }
      ok(body);
    } finally { setBusy(""); }
  }

  const saveDraft = () => call("save", `/api/admin/tours/${tourId}/draft`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason, fields: Object.fromEntries(changed.map(f => [f.key, toValue(texts[f.key], f.list)])) }),
  }, body => { apply(body as DraftState); toast.success(t("draftSaved"), 3000); });

  const runChecks = () => draft && call("checks", `/api/admin/tours/${tourId}/generated/${draft.id}/revalidate`,
    { method: "POST" }, () => toast.success(t("checksQueued"), 4000));

  const publish = () => call("publish", `/api/admin/tours/${tourId}/draft/publish`, { method: "POST" }, body => {
    toast.success(t("published", (body as { version_num: number }).version_num), 5000);
    load();
  });

  const discard = () => {
    if (!confirm(t("discardConfirm"))) return;
    call("discard", `/api/admin/tours/${tourId}/draft`, { method: "DELETE" }, () => {
      toast.success(t("discarded"), 3000);
      load();
    });
  };

  const unlock = () => {
    const why = prompt(t("unlockPrompt"));
    if (!why || why.trim().length < 3) return;
    call("unlock", `/api/admin/tours/${tourId}/unlock`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: why.trim() }),
    }, () => { toast.success(t("unlocked"), 4000); load(); });
  };

  const reasonOk = reason.trim().length >= 3 && reason.trim().length <= 500;

  return (
    <div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <Badge tone="neutral">{state.live ? t("liveVersion", state.live.version_num) : t("noLive")}</Badge>
        {draft && <Badge tone="warning">{t("draftBanner", draft.version_num)}</Badge>}
        {state.lock && <Badge tone="accent">{t("locked")}</Badge>}
        {state.lock && canUnlock && (
          <Button variant="secondary" onClick={unlock} disabled={busy !== ""}>{t("unlock")}</Button>
        )}
      </div>
      {state.lock && (
        <div style={{ fontSize: 12, color: K.muted, marginBottom: 12 }}>
          {t("lockedBy", state.lock.by ?? "—", state.lock.at ? formatDateTime(state.lock.at) : "—")}
        </div>
      )}
      {!draft && <p style={{ fontSize: 12, color: K.muted, margin: "0 0 12px" }}>{t("noDraft")}</p>}
      {draft && draft.editors.length > 0 && (
        <p style={{ fontSize: 12, color: K.muted, margin: "0 0 12px" }}>{t("editedBy", draft.editors.join(", "))}</p>
      )}
      {state.two_person_publish && <p style={{ fontSize: 12, color: K.muted, margin: "0 0 12px" }}>{t("twoPerson")}</p>}
      {!canEdit && <p style={{ fontSize: 12, color: K.muted, margin: "0 0 12px" }}>{t("readOnly")}</p>}

      {FIELDS.map(f => (
        <div key={f.key} style={{ marginBottom: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, fontWeight: 600, color: K.muted,
            textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>
            <span>{tf(f.key)}</span>
            {f.limit && <span style={{ color: texts[f.key]?.length > f.limit ? K.danger : K.muted2 }}>
              {texts[f.key]?.length ?? 0}/{f.limit}</span>}
          </div>
          {f.rows ? (
            <Textarea value={texts[f.key] ?? ""} rows={f.rows} readOnly={!canEdit}
              onChange={e => setTexts(x => ({ ...x, [f.key]: e.target.value }))}
              style={{ width: "100%", fontFamily: f.list ? mono : undefined, fontSize: 13 }} />
          ) : (
            <Input value={texts[f.key] ?? ""} readOnly={!canEdit}
              onChange={e => setTexts(x => ({ ...x, [f.key]: e.target.value }))} style={{ width: "100%" }} />
          )}
        </div>
      ))}

      {canEdit && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>{t("reason")}</div>
          <Input value={reason} onChange={e => setReason(e.target.value)} maxLength={500} style={{ width: "100%" }}
            placeholder={t("reasonHint")} />
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 20 }}>
        {canEdit && (
          <Button variant="primary" onClick={saveDraft} disabled={busy !== "" || !reasonOk || (changed.length === 0 && !!draft)}>
            {busy === "save" ? t("savingDraft") : t("saveDraft")}
          </Button>
        )}
        {draft && canEdit && (
          <Button variant="secondary" onClick={runChecks} disabled={busy !== "" || changed.length > 0}>{t("runChecks")}</Button>
        )}
        {draft && <Button variant="secondary" onClick={load} disabled={busy !== ""}>{t("refresh")}</Button>}
        {draft && canPublish && (
          <Button variant="primary" onClick={publish}
            disabled={busy !== "" || changed.length > 0 || draft.checks_passed !== true}
            title={draft.checks_passed !== true ? t("publishNeedsChecks") : undefined}>
            {busy === "publish" ? t("publishing") : t("publish")}
          </Button>
        )}
        {draft && canEdit && (
          <Button variant="secondary" onClick={discard} disabled={busy !== ""}>{t("discard")}</Button>
        )}
      </div>
      {draft && (
        <div style={{ fontSize: 12, marginBottom: 12, color: draft.checks_passed === true ? K.success
          : draft.checks_passed === false ? K.danger : K.muted }}>
          {draft.checks_passed === true ? t("checksPassed") : draft.checks_passed === false ? t("checksFailed")
            : t("checksPending")}
        </div>
      )}

      {draft && (
        <div style={{ borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em",
            marginBottom: 8 }}>{t("diffTitle")}</div>
          {state.diff.length === 0 ? (
            <div style={{ fontSize: 12, color: K.muted }}>{t("noChanges")}</div>
          ) : state.diff.map(d => (
            <div key={d.field} style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: K.muted, marginBottom: 4 }}>
                {(FIELDS.some(f => f.key === d.field) ? tf(d.field as FieldKey) : d.field)}
              </div>
              <div style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap", color: K.body }}>
                {d.ops.map((o, i) => o.op === "equal" ? <span key={i}>{o.text}</span>
                  : o.op === "delete" ? <del key={i} style={{ background: K.dangerTint, color: K.danger }}>{o.text}</del>
                  : <ins key={i} style={{ background: K.successSoft, color: K.success, textDecoration: "none" }}>{o.text}</ins>)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

"use client";
// app/admin/review/ReviewEditor.tsx
// AA-662 PR 2 — the expanded field editor for one review-queue row. Logic (edit → PATCH → reset
// revalidate → re-validate job → poll → read verdict) is unchanged from the original page.tsx;
// it now calls reviewApi and reports via the kit toast instead of inline state where useful.

import { AlertTriangle, Edit3, Loader2, Save, ShieldCheck, XCircle } from "lucide-react";
import { useState } from "react";
import { A, mono, sans, Btn } from "../_components/adminUi";
import { formatDateTime } from "../../_kit";
import {
  AREA_FIELDS,
  FIELD_LABEL,
  LIST_FIELDS,
  SEO_META_MAX,
  SEO_META_MIN,
  TEXT_FIELDS,
  draftToPatchValue,
  failureMap,
  toDraft,
  type ReviewItem,
} from "./reviewModel";
import {
  fetchRevalidateState,
  patchGenerated,
  pollJob,
  startRevalidate,
} from "./reviewApi";

function RevalidatePill({ state }: { state: boolean | null }) {
  const cfg =
    state === true
      ? { bg: A.greenSoft, color: A.green, label: "Re-validation passed", Icon: ShieldCheck }
      : state === false
        ? { bg: A.redSoft, color: A.red, label: "Re-validation failed", Icon: XCircle }
        : { bg: "var(--aa-amber-bg)", color: "var(--aa-amber-deep)", label: "Needs re-validation", Icon: AlertTriangle };
  const { Icon } = cfg;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontSize: 11,
        fontWeight: 600,
        padding: "3px 9px",
        borderRadius: 20,
        background: cfg.bg,
        color: cfg.color,
      }}
    >
      <Icon size={11} /> {cfg.label}
    </span>
  );
}

function FieldLabel({ field, fails }: { field: string; fails?: { code: string; reason: string }[] }) {
  const failed = !!fails?.length;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
      <span
        style={{
          fontSize: 10,
          fontWeight: 700,
          textTransform: "uppercase",
          letterSpacing: "0.1em",
          color: failed ? A.red : "var(--aa-neutral-fg2)",
        }}
      >
        {FIELD_LABEL[field] || field}
      </span>
      {failed &&
        fails!.map((f, i) => (
          <span
            key={i}
            title={f.code}
            style={{
              fontSize: 10,
              color: A.red,
              background: A.redSoft,
              border: `1px solid ${A.redBorder}`,
              borderRadius: 4,
              padding: "1px 6px",
            }}
          >
            {f.reason}
          </span>
        ))}
    </div>
  );
}

function fieldBoxStyle(failed: boolean): React.CSSProperties {
  return {
    width: "100%",
    boxSizing: "border-box",
    fontFamily: sans,
    fontSize: 13,
    color: A.body,
    padding: "8px 10px",
    borderRadius: 6,
    background: A.card,
    lineHeight: 1.6,
    border: `1px solid ${failed ? "var(--aa-red-border2)" : A.line}`,
    outline: "none",
    resize: "vertical",
  };
}

function ItineraryPreview({ text }: { text: string }) {
  const lines = (text || "").split("\n");
  return (
    <div
      style={{
        fontFamily: sans,
        fontSize: 12,
        color: A.body,
        lineHeight: 1.7,
        padding: "8px 10px",
        borderRadius: 6,
        background: A.bg,
        border: `1px solid ${A.line}`,
        maxHeight: 320,
        overflowY: "auto",
        whiteSpace: "pre-wrap",
      }}
    >
      {lines.map((ln, i) =>
        /^\s*Day\s+\d+/i.test(ln) ? (
          <div key={i} style={{ fontWeight: 700, color: A.ink, marginTop: i ? 10 : 0 }}>
            {ln}
          </div>
        ) : (
          <div key={i}>{ln || "\u00A0"}</div>
        ),
      )}
    </div>
  );
}

export function ReviewEditor({
  item,
  onSaved,
  onRevalidated,
}: {
  item: ReviewItem;
  onSaved: (id: string) => void;
  onRevalidated: (id: string, passed: boolean | null) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>(() => toDraft(item.raw));
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [revalidating, setRevalidating] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const fails = failureMap(item.raw.failures);

  // Note: this editor is mounted with a `key={item.id}` by the parent drawer, so switching rows
  // remounts it with a fresh draft from the lazy initial state above — no reset-in-effect needed
  // (the React Compiler forbids setState in an effect; AGENTS.md).

  function set(field: string, value: string) {
    setDraft((d) => ({ ...d, [field]: value }));
    setDirty((s) => new Set(s).add(field));
    setMsg(null);
  }

  async function save() {
    if (dirty.size === 0) {
      setMsg({ kind: "err", text: "No changes to save." });
      return;
    }
    setSaving(true);
    setMsg(null);
    const body: Record<string, unknown> = {};
    for (const f of dirty) body[f] = draftToPatchValue(f, draft[f]);
    try {
      await patchGenerated(item.raw.tour_id, item.raw.generated_content_id, body);
      setDirty(new Set());
      onSaved(item.id);
      setMsg({ kind: "ok", text: "Saved. Re-validate before approving." });
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : "Save failed." });
    } finally {
      setSaving(false);
    }
  }

  async function revalidate() {
    if (dirty.size > 0) {
      setMsg({ kind: "err", text: "Save your edits first." });
      return;
    }
    setRevalidating(true);
    setMsg(null);
    try {
      const { job_id } = await startRevalidate(item.raw.tour_id, item.raw.generated_content_id);
      const outcome = await pollJob(job_id);
      if (outcome === "succeeded") {
        const passed = await fetchRevalidateState(item.id);
        onRevalidated(item.id, passed);
        setMsg(
          passed === true
            ? { kind: "ok", text: "Re-validation passed. You can approve now." }
            : { kind: "err", text: "Re-validation failed. Fix the flagged fields and try again." },
        );
      } else {
        onRevalidated(item.id, null);
        setMsg({
          kind: "err",
          text:
            outcome === "timeout"
              ? "Re-validation is taking too long — refresh to check."
              : outcome === "interrupted"
                ? "Re-validation was interrupted. Try again."
                : "Re-validation job failed. Try again.",
        });
      }
    } catch (err) {
      setMsg({ kind: "err", text: err instanceof Error ? err.message : "Re-validation error." });
    } finally {
      setRevalidating(false);
    }
  }

  return (
    <div style={{ borderTop: `1px solid ${A.line}`, padding: "18px 20px", background: A.bg }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <RevalidatePill state={item.revalidate_passed} />
        {item.human_edited && (
          <span style={{ fontSize: 11, color: A.muted, display: "inline-flex", alignItems: "center", gap: 4 }}>
            <Edit3 size={11} /> Edited
            {item.edited_at ? ` · ${formatDateTime(item.edited_at)}` : ""}
            {item.reviewed_by ? ` · ${item.reviewed_by}` : ""}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <Btn variant="ghost" size="sm" disabled={saving || dirty.size === 0} onClick={save}>
          {saving ? <Loader2 size={12} className="spin" /> : <Save size={12} />} Save edits
          {dirty.size ? ` (${dirty.size})` : ""}
        </Btn>
        <Btn variant="primary" size="sm" disabled={revalidating || dirty.size > 0} onClick={revalidate}>
          {revalidating ? <Loader2 size={12} className="spin" /> : <ShieldCheck size={12} />} Re-validate
        </Btn>
      </div>

      {msg && (
        <div
          style={{
            fontSize: 12,
            padding: "8px 12px",
            borderRadius: 6,
            marginBottom: 14,
            background: msg.kind === "ok" ? A.greenSoft : A.redSoft,
            color: msg.kind === "ok" ? A.green : A.red,
          }}
        >
          {msg.text}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 20px" }}>
        {TEXT_FIELDS.map((f) => (
          <div key={f} style={{ gridColumn: f === "seo_title" ? "1 / -1" : "auto" }}>
            <FieldLabel field={f} fails={fails[f]} />
            <input value={draft[f]} onChange={(e) => set(f, e.target.value)} style={fieldBoxStyle(!!fails[f])} />
          </div>
        ))}

        {AREA_FIELDS.map((f) => (
          <div key={f} style={{ gridColumn: "1 / -1" }}>
            <FieldLabel field={f} fails={fails[f]} />
            <textarea
              value={draft[f]}
              onChange={(e) => set(f, e.target.value)}
              rows={f === "aa_summary" || f === "aa_description" ? 4 : 2}
              style={fieldBoxStyle(!!fails[f])}
            />
            {f === "seo_meta" &&
              (() => {
                const n = draft.seo_meta.length;
                const inBand = n >= SEO_META_MIN && n <= SEO_META_MAX;
                return (
                  <div style={{ fontSize: 11, marginTop: 3, color: inBand ? A.green : A.red, fontFamily: mono }}>
                    {n} chars · band {SEO_META_MIN}–{SEO_META_MAX}
                  </div>
                );
              })()}
          </div>
        ))}

        {LIST_FIELDS.map((f) => (
          <div key={f}>
            <FieldLabel field={f} fails={fails[f]} />
            <textarea
              value={draft[f]}
              onChange={(e) => set(f, e.target.value)}
              rows={5}
              placeholder="One item per line"
              style={{ ...fieldBoxStyle(!!fails[f]), fontFamily: mono, fontSize: 12 }}
            />
            <div style={{ fontSize: 10, color: A.muted, marginTop: 3 }}>One item per line</div>
          </div>
        ))}

        <div style={{ gridColumn: "1 / -1" }}>
          <FieldLabel field="aa_itineraries" fails={fails.aa_itineraries} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <textarea
              value={draft.aa_itineraries}
              onChange={(e) => set("aa_itineraries", e.target.value)}
              rows={14}
              style={{ ...fieldBoxStyle(!!fails.aa_itineraries), fontFamily: mono, fontSize: 12 }}
            />
            <ItineraryPreview text={draft.aa_itineraries} />
          </div>
        </div>

        <div style={{ gridColumn: "1 / -1" }}>
          <FieldLabel field="og_tags" />
          <pre
            style={{
              margin: 0,
              fontFamily: mono,
              fontSize: 11,
              color: A.muted,
              padding: "8px 10px",
              borderRadius: 6,
              background: A.bg,
              border: `1px solid ${A.line}`,
              overflowX: "auto",
            }}
          >
            {JSON.stringify(item.raw.og_tags ?? {}, null, 2)}
          </pre>
          <div style={{ fontSize: 10, color: A.muted, marginTop: 3 }}>
            Read-only — OG tags are generated, not hand-edited yet.
          </div>
        </div>
      </div>
    </div>
  );
}

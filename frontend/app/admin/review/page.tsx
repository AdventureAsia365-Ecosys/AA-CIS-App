"use client";
// app/admin/review/page.tsx — HITL review queue (AA-234 Part C / AA-241)
// Consumes AA-240 GET /admin/review-queue (full fields + failures[] + 072 audit).
// Lifecycle: edit → PATCH (sets human_edited, resets revalidate_passed=NULL)
//            → POST revalidate (202 + job) → poll /jobs → Approve gated on revalidate_passed===true.

import { useState, useEffect, useCallback, useRef } from "react";
import {
  CheckCircle, XCircle, RotateCcw, ChevronDown, ChevronUp, Filter,
  Edit3, AlertTriangle, Save, ShieldCheck, Loader2, X, Ban,
} from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, mono, sans, Card, Btn, LoadingScreen, Badge, TH, TD } from "../_components/adminUi";

// ── Reviewer identity (temporary until AA-232 per-user auth) ───────────────────
// Stored once in localStorage, sent as x-reviewer-id; BFF forwards it; backend
// writes generated_content.reviewed_by. Empty → backend falls back to "admin".
function getReviewerId(): string {
  if (typeof window === "undefined") return "";
  let id = window.localStorage.getItem("cis_reviewer_id") || "";
  if (!id) {
    const entered = window.prompt("Reviewer name (for the edit audit trail):", "");
    id = (entered || "").trim();
    if (id) window.localStorage.setItem("cis_reviewer_id", id);
  }
  return id;
}

function authHeaders(json = false): Record<string, string> {
  const h: Record<string, string> = {};
  const rid = (typeof window !== "undefined" && window.localStorage.getItem("cis_reviewer_id")) || "";
  if (rid) h["x-reviewer-id"] = rid;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

// ── Field model ───────────────────────────────────────────────────────────────
// Mirrors backend _ALLOWED_GC_FIELDS (11). og_tags is read-only in v1 (data empty).
const TEXT_FIELDS = ["aa_name", "aa_subtitle", "seo_title"] as const;       // single-line
const AREA_FIELDS = ["aa_summary", "aa_description", "mobile_card_text", "seo_meta"] as const; // textarea
const LIST_FIELDS = ["aa_highlights", "seo_keywords_used"] as const;        // newline = item
// aa_itineraries → raw textarea + Day preview. og_tags → read-only.

const FIELD_LABEL: Record<string, string> = {
  aa_name: "Name",
  aa_subtitle: "Subtitle",
  aa_summary: "Summary",
  aa_description: "Description",
  aa_highlights: "Highlights",
  aa_itineraries: "Itinerary",
  mobile_card_text: "Mobile card text",
  seo_title: "SEO title",
  seo_meta: "SEO meta",
  seo_keywords_used: "SEO keywords",
  og_tags: "OG tags",
};

const SEO_META_MIN = 140;
const SEO_META_MAX = 155;

// AA-626 — classify a gate failure code by nature so the reviewer can tell, at a glance, a tour
// that is "unfairly" held (high score, only style/brand nits) from one that is genuinely wrong.
//   red   = product-truth / structural: a real correctness problem, blocks for a reason
//   amber = brand / style / SEO surface: often fixable or nudge-able, does not mean the tour is wrong
//   gray  = anything else / low-quality catch-all
const PRODUCT_TRUTH_CODES = new Set([
  "MISSING_FIELD", "ITINERARY_DAY_COUNT_MISMATCH", "ITINERARY_MEAL_TIME_INVENTED",
  "ITINERARY_STILL_COMPRESSED", "FABRICATED", "NOVEL_NUMERIC_CLAIM",
]);
const BRAND_STYLE_CODES = new Set([
  "FORBIDDEN_WORD", "META_TOO_SHORT", "SEO_META_TOO_LONG", "META_INCOMPLETE_SENTENCE",
  "HIGHLIGHTS_TOO_GENERIC", "HIGHLIGHTS_OPTIONAL_LANGUAGE", "DFS_INTENT_UNDERUSED",
]);

type Severity = "red" | "amber" | "gray";

function codeSeverity(code: string): Severity {
  const c = (code || "").toUpperCase();
  if (PRODUCT_TRUTH_CODES.has(c)) return "red";
  if (BRAND_STYLE_CODES.has(c)) return "amber";
  return "gray";
}

// The worst severity across a set of codes drives the row's overall reason color.
function worstSeverity(codes: string[]): Severity {
  let sev: Severity = "gray";
  for (const c of codes) {
    const s = codeSeverity(c);
    if (s === "red") return "red";
    if (s === "amber") sev = "amber";
  }
  return sev;
}

const SEV_STYLE: Record<Severity, { bg: string; color: string; border: string }> = {
  red:   { bg: A.redSoft, color: A.red, border: "#FECACA" },
  amber: { bg: A.amberSoft, color: "#92400E", border: "#FDE68A" },
  gray:  { bg: "#F3F4F6", color: "#4B5563", border: "#E5E7EB" },
};

function asList(v: any): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === "string") {
    try { const p = JSON.parse(v); return Array.isArray(p) ? p.map(String) : []; }
    catch { return v.trim() ? [v] : []; }
  }
  return [];
}

// Build the editable draft from a raw API row.
function toDraft(r: any) {
  return {
    aa_name: r.aa_name || "",
    aa_subtitle: r.aa_subtitle || "",
    aa_summary: r.aa_summary || "",
    aa_description: r.aa_description || "",
    aa_highlights: asList(r.aa_highlights).join("\n"),
    aa_itineraries: r.aa_itineraries || "",
    mobile_card_text: r.mobile_card_text || "",
    seo_title: r.seo_title || "",
    seo_meta: r.seo_meta || "",
    seo_keywords_used: asList(r.seo_keywords_used).join("\n"),
  } as Record<string, string>;
}

// Convert a draft field back to the PATCH wire value (lists → arrays).
function draftToPatchValue(field: string, raw: string): any {
  if ((LIST_FIELDS as readonly string[]).includes(field)) {
    return raw.split("\n").map(s => s.trim()).filter(Boolean);
  }
  return raw;
}

// ── Per-field failure index ───────────────────────────────────────────────────
// failures: [{field, code, reason}] from AA-240, re-derived live on current content.
function failureMap(failures: any[]): Record<string, { code: string; reason: string }[]> {
  const m: Record<string, { code: string; reason: string }[]> = {};
  for (const f of failures || []) {
    if (!f?.field) continue;
    (m[f.field] ||= []).push({ code: f.code, reason: f.reason });
  }
  return m;
}

// ── Validation-state pill ─────────────────────────────────────────────────────
function RevalidatePill({ state }: { state: boolean | null }) {
  const cfg = state === true
    ? { bg: A.greenSoft, color: A.green, label: "Re-validation passed", Icon: ShieldCheck }
    : state === false
      ? { bg: A.redSoft, color: A.red, label: "Re-validation failed", Icon: XCircle }
      : { bg: "#FEF3C7", color: "#92400E", label: "Needs re-validation", Icon: AlertTriangle };
  const { Icon } = cfg;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, fontWeight: 600,
      padding: "3px 9px", borderRadius: 20, background: cfg.bg, color: cfg.color,
    }}>
      <Icon size={11} /> {cfg.label}
    </span>
  );
}

// ── Field label + inline failure reasons ──────────────────────────────────────
function FieldLabel({ field, fails }: { field: string; fails?: { code: string; reason: string }[] }) {
  const failed = !!fails?.length;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
      <span style={{
        fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.1em",
        color: failed ? A.red : "#6B7280",
      }}>{FIELD_LABEL[field] || field}</span>
      {failed && fails!.map((f, i) => (
        <span key={i} title={f.code} style={{
          fontSize: 10, color: A.red, background: A.redSoft, border: "1px solid #FECACA",
          borderRadius: 4, padding: "1px 6px",
        }}>{f.reason}</span>
      ))}
    </div>
  );
}

function fieldBoxStyle(failed: boolean): React.CSSProperties {
  return {
    width: "100%", boxSizing: "border-box", fontFamily: sans, fontSize: 13, color: A.body,
    padding: "8px 10px", borderRadius: 6, background: A.card, lineHeight: 1.6,
    border: `1px solid ${failed ? "#FCA5A5" : A.line}`,
    outline: "none", resize: "vertical" as const,
  };
}

// ── Itinerary preview (read-only, highlights "Day N" headers) ──────────────────
function ItineraryPreview({ text }: { text: string }) {
  const lines = (text || "").split("\n");
  return (
    <div style={{
      fontFamily: sans, fontSize: 12, color: A.body, lineHeight: 1.7,
      padding: "8px 10px", borderRadius: 6, background: A.bg, border: `1px solid ${A.line}`,
      maxHeight: 320, overflowY: "auto", whiteSpace: "pre-wrap" as const,
    }}>
      {lines.map((ln, i) =>
        /^\s*Day\s+\d+/i.test(ln)
          ? <div key={i} style={{ fontWeight: 700, color: A.ink, marginTop: i ? 10 : 0 }}>{ln}</div>
          : <div key={i}>{ln || "\u00A0"}</div>
      )}
    </div>
  );
}

// ── Editor body ───────────────────────────────────────────────────────────────
function ReviewEditor({ item, onSaved, onRevalidated }: {
  item: any;
  onSaved: (id: string, draft: Record<string, string>) => void;
  onRevalidated: (id: string, passed: boolean | null) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>(() => toDraft(item.raw));
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [revalidating, setRevalidating] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const fails = failureMap(item.raw.failures);

  // reset local draft if the underlying row changes (e.g. after refetch)
  useEffect(() => { setDraft(toDraft(item.raw)); setDirty(new Set()); }, [item.raw]);

  function set(field: string, value: string) {
    setDraft(d => ({ ...d, [field]: value }));
    setDirty(s => new Set(s).add(field));
    setMsg(null);
  }

  async function save() {
    if (dirty.size === 0) { setMsg({ kind: "err", text: "No changes to save." }); return; }
    setSaving(true); setMsg(null);
    const body: Record<string, any> = {};
    for (const f of dirty) body[f] = draftToPatchValue(f, draft[f]);
    try {
      const res = await fetch(
        `/api/admin/tours/${item.raw.tour_id}/generated/${item.raw.generated_content_id}`,
        { method: "PATCH", headers: authHeaders(true), body: JSON.stringify(body) },
      );
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `Save failed (${res.status})`);
      }
      setDirty(new Set());
      onSaved(item.id, draft);                 // marks human_edited, revalidate_passed=null in parent
      setMsg({ kind: "ok", text: "Saved. Re-validate before approving." });
    } catch (err: any) {
      setMsg({ kind: "err", text: err.message || "Save failed." });
    } finally { setSaving(false); }
  }

  async function revalidate() {
    if (dirty.size > 0) { setMsg({ kind: "err", text: "Save your edits first." }); return; }
    setRevalidating(true); setMsg(null);
    try {
      const res = await fetch(
        `/api/admin/tours/${item.raw.tour_id}/generated/${item.raw.generated_content_id}/revalidate`,
        { method: "POST", headers: authHeaders() },
      );
      if (res.status !== 202) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `Could not start re-validation (${res.status})`);
      }
      const { job_id } = await res.json();
      const outcome = await pollJob(job_id);
      if (outcome === "succeeded") {
        // Job finished; the verdict lives on the row, not the job. Refetch it.
        const passed = await fetchRevalidateState(item.id);
        onRevalidated(item.id, passed);
        setMsg(passed === true
          ? { kind: "ok", text: "Re-validation passed. You can approve now." }
          : { kind: "err", text: "Re-validation failed. Fix the flagged fields and try again." });
      } else {
        onRevalidated(item.id, null);
        setMsg({ kind: "err", text:
          outcome === "timeout" ? "Re-validation is taking too long — refresh to check."
          : outcome === "interrupted" ? "Re-validation was interrupted. Try again."
          : "Re-validation job failed. Try again." });
      }
    } catch (err: any) {
      setMsg({ kind: "err", text: err.message || "Re-validation error." });
    } finally { setRevalidating(false); }
  }

  return (
    <div style={{ borderTop: `1px solid ${A.line}`, padding: "18px 20px", background: A.bg }}>
      {/* action bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <RevalidatePill state={item.revalidate_passed} />
        {item.human_edited && (
          <span style={{ fontSize: 11, color: A.muted, display: "inline-flex", alignItems: "center", gap: 4 }}>
            <Edit3 size={11} /> Edited{item.edited_at ? ` · ${new Date(item.edited_at).toLocaleString()}` : ""}
            {item.reviewed_by ? ` · ${item.reviewed_by}` : ""}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <Btn variant="ghost" size="sm" disabled={saving || dirty.size === 0} onClick={save}>
          {saving ? <Loader2 size={12} className="spin" /> : <Save size={12} />} Save edits{dirty.size ? ` (${dirty.size})` : ""}
        </Btn>
        <Btn variant="primary" size="sm" disabled={revalidating || dirty.size > 0} onClick={revalidate}>
          {revalidating ? <Loader2 size={12} className="spin" /> : <ShieldCheck size={12} />} Re-validate
        </Btn>
      </div>

      {msg && (
        <div style={{
          fontSize: 12, padding: "8px 12px", borderRadius: 6, marginBottom: 14,
          background: msg.kind === "ok" ? A.greenSoft : A.redSoft,
          color: msg.kind === "ok" ? A.green : A.red,
        }}>{msg.text}</div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 20px" }}>
        {/* single-line text */}
        {TEXT_FIELDS.map(f => (
          <div key={f} style={{ gridColumn: f === "seo_title" ? "1 / -1" : "auto" }}>
            <FieldLabel field={f} fails={fails[f]} />
            <input value={draft[f]} onChange={e => set(f, e.target.value)} style={fieldBoxStyle(!!fails[f])} />
          </div>
        ))}

        {/* textareas */}
        {AREA_FIELDS.map(f => (
          <div key={f} style={{ gridColumn: "1 / -1" }}>
            <FieldLabel field={f} fails={fails[f]} />
            <textarea
              value={draft[f]} onChange={e => set(f, e.target.value)} rows={f === "aa_summary" || f === "aa_description" ? 4 : 2}
              style={fieldBoxStyle(!!fails[f])}
            />
            {f === "seo_meta" && (() => {
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

        {/* newline lists */}
        {LIST_FIELDS.map(f => (
          <div key={f}>
            <FieldLabel field={f} fails={fails[f]} />
            <textarea
              value={draft[f]} onChange={e => set(f, e.target.value)} rows={5}
              placeholder="One item per line"
              style={{ ...fieldBoxStyle(!!fails[f]), fontFamily: mono, fontSize: 12 }}
            />
            <div style={{ fontSize: 10, color: A.muted, marginTop: 3 }}>One item per line</div>
          </div>
        ))}

        {/* itinerary: raw edit + live Day preview */}
        <div style={{ gridColumn: "1 / -1" }}>
          <FieldLabel field="aa_itineraries" fails={fails.aa_itineraries} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <textarea
              value={draft.aa_itineraries} onChange={e => set("aa_itineraries", e.target.value)} rows={14}
              style={{ ...fieldBoxStyle(!!fails.aa_itineraries), fontFamily: mono, fontSize: 12 }}
            />
            <ItineraryPreview text={draft.aa_itineraries} />
          </div>
        </div>

        {/* og_tags: read-only in v1 (data empty until pipeline populates) */}
        <div style={{ gridColumn: "1 / -1" }}>
          <FieldLabel field="og_tags" />
          <pre style={{
            margin: 0, fontFamily: mono, fontSize: 11, color: A.muted, padding: "8px 10px",
            borderRadius: 6, background: A.bg, border: `1px solid ${A.line}`, overflowX: "auto",
          }}>
            {JSON.stringify(item.raw.og_tags ?? {}, null, 2)}
          </pre>
          <div style={{ fontSize: 10, color: A.muted, marginTop: 3 }}>Read-only — OG tags are generated, not hand-edited yet.</div>
        </div>
      </div>
    </div>
  );
}

// Poll GET /admin/jobs/{id} until terminal. The job row carries lifecycle only
// (queued/running/succeeded/failed/interrupted) — NOT the revalidate outcome.
// revalidate_passed lives on generated_content, written by _revalidate_job, so
// the caller must refetch the row after a successful job to read the verdict.
type JobOutcome = "succeeded" | "failed" | "interrupted" | "timeout";
async function pollJob(jobId: string): Promise<JobOutcome> {
  const deadline = Date.now() + 120_000; // 2 min ceiling
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch(`/api/admin/jobs/${jobId}`, { headers: authHeaders() });
    if (!res.ok) continue;
    const job = await res.json();
    if (job.status === "succeeded") return "succeeded";
    if (job.status === "failed") return "failed";
    if (job.status === "interrupted") return "interrupted";
  }
  return "timeout";
}

// Refetch a single row from the queue to read its current revalidate_passed.
// Returns true/false/null (null = not found, e.g. it left the pending queue).
async function fetchRevalidateState(reviewId: string): Promise<boolean | null> {
  const res = await fetch(`/api/admin/review-queue`, { headers: authHeaders() });
  if (!res.ok) return null;
  const d = await res.json();
  const row = (d.data || []).find((r: any) => String(r.id) === reviewId);
  if (!row) return null;
  return row.revalidate_passed === true ? true : row.revalidate_passed === false ? false : null;
}

// ── Reason chips (color-coded by failure nature) ──────────────────────────────
function ReasonChips({ codes }: { codes: string[] }) {
  if (!codes.length) {
    return <span style={{ fontSize: 11, color: A.muted2 }}>—</span>;
  }
  const shown = codes.slice(0, 3);
  const extra = codes.length - shown.length;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 5, alignItems: "center" }}>
      {shown.map((c, i) => {
        const s = SEV_STYLE[codeSeverity(c)];
        return (
          <span key={i} title={c} style={{
            fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 20,
            background: s.bg, color: s.color, border: `1px solid ${s.border}`,
            whiteSpace: "nowrap" as const,
          }}>{c}</span>
        );
      })}
      {extra > 0 && <span style={{ fontSize: 10.5, color: A.muted }}>+{extra}</span>}
    </div>
  );
}

// ── Row (table) ───────────────────────────────────────────────────────────────
// One table row per review_queue entry (= one failed version). Click to expand the full
// editor (same ReviewEditor as before — edit / save / re-validate, plus content view).
function ReviewRow({ item, expanded, onToggle, onApprove, onReject, onDismiss, onRegenerate, onSaved, onRevalidated, colSpan }: {
  item: any;
  expanded: boolean;
  onToggle: () => void;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
  onDismiss: (id: string) => void;
  onRegenerate: (item: any) => void;
  onSaved: (id: string, draft: Record<string, string>) => void;
  onRevalidated: (id: string, passed: boolean | null) => void;
  colSpan: number;
}) {
  const failCount = (item.raw.failures || []).length;
  const scoreColor = item.score >= 7 ? A.green : item.score >= 5 ? A.amber : A.red;

  // Approve gate (AA-234 Part A semantics):
  // - if edited at all → must have a passed re-validation
  // - if never edited → allow when it already cleared (score≥7 and no live failures)
  const canApprove = item.human_edited
    ? item.revalidate_passed === true
    : (item.score >= 7 && failCount === 0);
  const approveHint = item.human_edited && item.revalidate_passed !== true
    ? "Re-validate after editing" : failCount > 0 ? "Resolve flagged fields" : "";

  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <>
      <tr
        onClick={onToggle}
        style={{ cursor: "pointer", background: expanded ? A.goldTint : A.card }}
      >
        {/* expand chevron */}
        <td style={{ ...TD, width: 34, paddingLeft: 16 }}>
          {expanded ? <ChevronUp size={14} style={{ color: A.muted }} /> : <ChevronDown size={14} style={{ color: A.muted }} />}
        </td>
        {/* tour name + edited badge */}
        <td style={{ ...TD, fontWeight: 600, color: A.ink, maxWidth: 280 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.name}</span>
            {item.human_edited && (
              <span style={{ fontSize: 10, color: A.muted, border: `1px solid ${A.line}`, borderRadius: 4, padding: "1px 6px", display: "inline-flex", alignItems: "center", gap: 3, flexShrink: 0 }}>
                <Edit3 size={9} /> Edited
              </span>
            )}
          </div>
        </td>
        {/* country */}
        <td style={{ ...TD, color: A.muted, whiteSpace: "nowrap" as const }}>{item.country}</td>
        {/* version */}
        <td style={TD}>
          {item.version_num != null
            ? <Badge color="blue">v{item.version_num}</Badge>
            : <span style={{ color: A.muted2 }}>—</span>}
        </td>
        {/* written date */}
        <td style={{ ...TD, color: A.muted, whiteSpace: "nowrap" as const, fontSize: 12 }}>{item.date || "—"}</td>
        {/* score */}
        <td style={TD}>
          <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14, color: scoreColor }}>
            {item.score.toFixed(1)}
          </span>
        </td>
        {/* reasons (color-coded) */}
        <td style={{ ...TD, maxWidth: 300 }}><ReasonChips codes={item.codes} /></td>
        {/* actions */}
        <td style={{ ...TD, whiteSpace: "nowrap" as const }} onClick={stop}>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <button onClick={() => onRegenerate(item)} title="Regenerate (re-runs the full pipeline, real cost)"
              style={{ padding: 7, border: `1px solid ${A.line}`, borderRadius: 8, background: "none", cursor: "pointer", color: A.muted, display: "flex" }}>
              <RotateCcw size={13} />
            </button>
            <button onClick={() => onDismiss(item.id)} title="Dismiss — drop this stale failed version from the queue (no edit, no publish)"
              style={{ padding: 7, border: `1px solid ${A.line}`, borderRadius: 8, background: "none", cursor: "pointer", color: A.muted, display: "flex" }}>
              <Ban size={13} />
            </button>
            <Btn variant="danger" size="sm" onClick={() => onReject(item.id)}>
              <XCircle size={12} /> Reject
            </Btn>
            <span title={approveHint} style={{ display: "inline-flex" }}>
              <Btn variant={canApprove ? "primary" : "ghost"} size="sm" disabled={!canApprove}
                onClick={() => onApprove(item.id)}>
                <CheckCircle size={12} /> Approve
              </Btn>
            </span>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={colSpan} style={{ padding: 0, borderBottom: `1px solid ${A.line2}` }}>
            <ReviewEditor item={item} onSaved={onSaved} onRevalidated={onRevalidated} />
          </td>
        </tr>
      )}
    </>
  );
}

// ── Regenerate confirm modal (AA-242) ──────────────────────────────────────────
// Reruns the FULL pipeline (real Bedrock cost) → new generated_content version +
// a new review_queue row. If the regenerated version comes back publishable
// (gc.status === "approved"), the CURRENT review row (item.id) is auto-closed as
// 'superseded'. If it lands in HITL instead, nothing is superseded — both the old
// and new versions stay pending so the reviewer can compare and decide.
//
// The publishable verdict is read from the backend gate (gc.status), never guessed
// from a client-side score. onReload = the page's queue refetch.
function RegenerateModal({ item, onClose, onReload }: {
  item: any; onClose: () => void; onReload: () => Promise<void> | void;
}) {
  const presetTier: string = item.raw?.requested_tier || "";   // "haiku" | "sonnet" | ""
  const [tier, setTier] = useState<string>("");                // only used when no preset
  const [running, setRunning] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const effectiveTier = presetTier || tier;                    // never silently defaulted
  const canRun = !running && !!effectiveTier;
  const close = () => { if (!running) onClose(); };            // block dismiss mid-run

  async function doRegenerate() {
    if (!effectiveTier) { setMsg({ kind: "err", text: "Choose a model tier first." }); return; }
    setRunning(true); setMsg(null);
    try {
      // a. kick off the async pipeline run for THIS tour (fresh batch_id each time)
      const res = await fetch(`/api/admin/run-tour-async`, {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify({
          tour_id: item.raw.tour_id,
          batch_id: crypto.randomUUID(),
          tenant_id: "00000000-0000-0000-0000-000000000001",
          model_tier: effectiveTier,
          allow_auto_upgrade: false,
        }),
      });
      // b. must be 202 accepted or we stop before polling
      if (res.status !== 202) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `Could not start regeneration (${res.status})`);
      }
      // c. + d. reuse the shared generic poller (same one Re-validate uses)
      const { job_id } = await res.json();
      setMsg({ kind: "ok", text: "Regenerating — running the full pipeline, please wait…" });
      const outcome = await pollJob(job_id);
      // e. anything but success → surface it, do NOT supersede, do NOT refetch
      if (outcome !== "succeeded") {
        setMsg({ kind: "err", text:
          outcome === "timeout" ? "Regeneration is taking too long — refresh the queue to check."
          : outcome === "interrupted" ? "Regeneration was interrupted. Try again."
          : "Regeneration job failed. Try again." });
        return;
      }
      // f. locate the freshly-created version: newest review row for the same tour_id
      const qRes = await fetch(`/api/admin/review-queue`, { headers: authHeaders() });
      if (!qRes.ok) throw new Error(`Regeneration finished but the queue could not be reloaded (${qRes.status})`);
      const q = await qRes.json();
      const sameTour = (q.data || []).filter((r: any) => String(r.tour_id) === String(item.raw.tour_id));
      const newest = sameTour.reduce(
        (a: any, b: any) => (!a || new Date(b.created_at) > new Date(a.created_at) ? b : a), null);
      // g. publishable gate = gc.status === "approved" (backend _is_publishable), never a score guess
      if (newest && newest.status === "approved") {
        const sRes = await fetch(`/api/admin/review-queue/${item.id}/supersede`, {
          method: "POST", headers: authHeaders(),
        });
        // 409 = already not pending (raced/closed) — treat as done, not an error
        if (!sRes.ok && sRes.status !== 409) {
          const e = await sRes.json().catch(() => ({}));
          throw new Error(e.detail || `Could not close the old review row (${sRes.status})`);
        }
      }
      // h. (approved) old row now superseded → drops from pending; new one shows.
      // i. (hitl)     nothing superseded → both old + new stay for the reviewer.
      await onReload();
      onClose();
    } catch (err: any) {
      setMsg({ kind: "err", text: err.message || "Regeneration error." });
    } finally {
      setRunning(false);
    }
  }

  return (
    <div onClick={close} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 50,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: A.card, borderRadius: 12, maxWidth: 440, width: "100%", padding: 24,
        border: `1px solid ${A.line}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <AlertTriangle size={20} style={{ color: A.amber }} />
          <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>Regenerate this version?</div>
          <div style={{ flex: 1 }} />
          <button onClick={close} disabled={running} style={{
            border: "none", background: "none", cursor: running ? "not-allowed" : "pointer",
            color: A.muted, display: "flex", opacity: running ? 0.4 : 1,
          }}><X size={18} /></button>
        </div>
        <p style={{ fontSize: 13, color: A.body, lineHeight: 1.6, margin: "0 0 8px" }}>
          This re-runs the <strong>full pipeline</strong> for <strong>{item.name}</strong> on real
          models — it costs actual Bedrock spend. Prefer fixing the fields by hand (Edit) when you can;
          only regenerate when the content is beyond a manual fix.
        </p>
        <p style={{ fontSize: 12, color: A.muted, lineHeight: 1.6, margin: "0 0 16px" }}>
          If the new version comes back publishable, this review row is closed automatically as
          <strong> superseded</strong>. If it doesn't, both versions stay in the queue for you to compare.
        </p>

        {!presetTier ? (
          <div style={{ marginBottom: 18 }}>
            <div style={{ fontSize: 12, color: A.body, marginBottom: 6 }}>
              No model tier was recorded for this tour — choose one:
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              {(["haiku", "sonnet"] as const).map(t => (
                <button key={t} onClick={() => setTier(t)} disabled={running} style={{
                  flex: 1, padding: "8px 10px", borderRadius: 8, cursor: running ? "not-allowed" : "pointer",
                  fontSize: 13, fontFamily: sans, textTransform: "capitalize" as const,
                  border: `1px solid ${tier === t ? A.gold : A.line}`,
                  background: tier === t ? A.gold : A.card,
                  color: tier === t ? "#fff" : A.body,
                }}>{t}</button>
              ))}
            </div>
          </div>
        ) : (
          <div style={{ fontSize: 11, color: A.muted, marginBottom: 18, fontFamily: mono }}>
            Model tier: {presetTier}
          </div>
        )}

        {msg && (
          <div style={{
            fontSize: 12, padding: "8px 12px", borderRadius: 6, marginBottom: 16,
            background: msg.kind === "ok" ? A.greenSoft : A.redSoft,
            color: msg.kind === "ok" ? A.green : A.red,
          }}>{msg.text}</div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <Btn variant="ghost" size="sm" onClick={close} disabled={running}>Cancel</Btn>
          <Btn variant="primary" size="sm" disabled={!canRun} onClick={doRegenerate}>
            {running ? <Loader2 size={12} className="spin" /> : <RotateCcw size={12} />} Regenerate
          </Btn>
        </div>
      </div>
    </div>
  );
}

// ── Row mapper ────────────────────────────────────────────────────────────────
function mapRow(r: any) {
  return {
    id: String(r.id),
    raw: r,
    name: r.aa_name || r.src_name || "Untitled tour",
    country: r.country || "Unknown",
    score: typeof r.score_overall === "number" ? r.score_overall : parseFloat(r.score_overall || "0"),
    date: r.created_at ? new Date(r.created_at).toLocaleDateString() : "",
    human_edited: !!r.human_edited,
    edited_at: r.edited_at || null,
    reviewed_by: r.reviewed_by || null,
    revalidate_passed: r.revalidate_passed === true ? true : r.revalidate_passed === false ? false : null,
    failure_summary: r.failure_summary || "",
    version_num: typeof r.version_num === "number" ? r.version_num : null,
    brand_audit_status: r.brand_audit_status || null,   // manual_check | flagged | null
    // Distinct gate codes for this row, from the per-field failures[] (AA-240).
    codes: [...new Set(((r.failures || []) as any[]).map(f => f?.code).filter(Boolean))] as string[],
  };
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function AdminReviewPage() {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filterCountry, setCountry] = useState("all");
  const [filterScore, setScore] = useState("all");
  const [filterStatus, setFilterStatus] = useState("pending");
  const [total, setTotal] = useState(0);
  const [regenTarget, setRegenTarget] = useState<any>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<"tour" | "country" | "version" | "date" | "score">("date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  // AA-626 follow-up: deep-link from Master Content ("N failed" badge) → ?tour_id=<uuid>
  // pre-filters the queue to that one tour so the reviewer lands on its failed versions.
  const [tourFilter, setTourFilter] = useState<string | null>(null);
  const [tourFilterName, setTourFilterName] = useState<string | null>(null);
  const reviewerInit = useRef(false);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`/api/admin/review-queue?status=${filterStatus}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`Could not load the review queue (${res.status})`);
      const d = await res.json();
      setItems((d.data || []).map(mapRow));
      setTotal(d.pagination?.total ?? (d.data || []).length);
    } catch (err: any) {
      setError(err.message || "Could not load the review queue.");
    } finally { setLoading(false); }
  }, [filterStatus]);

  useEffect(() => {
    if (!reviewerInit.current) {
      reviewerInit.current = true;
      getReviewerId();
      // Read ?tour_id= deep-link once on mount (client component; avoids useSearchParams
      // Suspense boundary). Filtering itself is client-side over the loaded rows.
      if (typeof window !== "undefined") {
        const tid = new URLSearchParams(window.location.search).get("tour_id");
        if (tid) setTourFilter(tid);
      }
    }
    load();
  }, [load]);

  // After a successful save: mark edited, invalidate prior re-validation.
  function onSaved(id: string) {
    setItems(p => p.map(i => i.id === id
      ? { ...i, human_edited: true, revalidate_passed: null, edited_at: new Date().toISOString() }
      : i));
  }
  function onRevalidated(id: string, passed: boolean | null) {
    setItems(p => p.map(i => i.id === id ? { ...i, revalidate_passed: passed } : i));
  }

  // Resolve the tour name for the deep-link filter banner once rows are loaded.
  useEffect(() => {
    if (!tourFilter) return;
    const hit = items.find(i => String(i.raw.tour_id) === tourFilter);
    if (hit) setTourFilterName(hit.name);
  }, [tourFilter, items]);

  async function onApprove(id: string) {
    const res = await fetch(`/api/admin/review-queue/${id}/approve`, { method: "POST", headers: authHeaders() });
    if (!res.ok) { setError(`Approve failed (${res.status}).`); return; }
    setItems(p => p.filter(i => i.id !== id));
    setTotal(t => Math.max(0, t - 1));
  }
  async function onReject(id: string) {
    const res = await fetch(`/api/admin/review-queue/${id}/reject`, { method: "POST", headers: authHeaders() });
    if (!res.ok) { setError(`Reject failed (${res.status}).`); return; }
    setItems(p => p.filter(i => i.id !== id));
    setTotal(t => Math.max(0, t - 1));
  }
  async function onDismiss(id: string) {
    const res = await fetch(`/api/admin/review-queue/${id}/dismiss`, { method: "POST", headers: authHeaders() });
    if (!res.ok) { setError(`Dismiss failed (${res.status}).`); return; }
    setItems(p => p.filter(i => i.id !== id));
    setTotal(t => Math.max(0, t - 1));
  }
  function toggleSort(key: "tour" | "country" | "version" | "date" | "score") {
    if (sortKey === key) { setSortDir(d => (d === "asc" ? "desc" : "asc")); }
    // score & version default to desc (highest first), text/date default to asc
    else { setSortKey(key); setSortDir(key === "score" || key === "version" ? "desc" : "asc"); }
  }

  const countries = [...new Set(items.map(i => i.country))];
  const filtered = items.filter(item => {
    const mt = !tourFilter || String(item.raw.tour_id) === tourFilter;
    const mc = filterCountry === "all" || item.country === filterCountry;
    const ms = filterScore === "all"
      || (filterScore === "critical" && item.score < 5)
      || (filterScore === "low" && item.score >= 5 && item.score < 7);
    return mt && mc && ms;
  }).sort((a, b) => {
    const dir = sortDir === "asc" ? 1 : -1;
    if (sortKey === "score") return (a.score - b.score) * dir;
    if (sortKey === "version") return ((a.version_num ?? 0) - (b.version_num ?? 0)) * dir;
    if (sortKey === "tour") return String(a.name).localeCompare(String(b.name)) * dir;
    if (sortKey === "country") return String(a.country).localeCompare(String(b.country)) * dir;
    // date: compare underlying created_at from raw (fall back to display date)
    const ad = new Date(a.raw.created_at || 0).getTime();
    const bd = new Date(b.raw.created_at || 0).getTime();
    return (ad - bd) * dir;
  });

  const REVIEW_COLS = 8;
  const sortArrow = (key: string) => sortKey === key ? (sortDir === "asc" ? " ↑" : " ↓") : "";

  const selectStyle: React.CSSProperties = {
    padding: "7px 14px", background: A.card, border: `1px solid ${A.line}`,
    borderRadius: 8, color: A.body, fontSize: 13, fontFamily: sans, cursor: "pointer", outline: "none",
  };

  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: sans, background: A.bg }}>
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}.spin{animation:spin .8s linear infinite}`}</style>
      <AdminSidebar />
      <main style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "32px 36px 56px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24 }}>
          <div>
            <div style={{ fontFamily: serif, fontSize: 26, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>Review Queue</div>
            <div style={{ fontSize: 13, color: A.muted, marginTop: 4 }}>Edit, re-validate, then approve. Approval is blocked until an edited tour passes re-validation.</div>
          </div>
          <div style={{ display: "flex", gap: 24 }}>
            <div style={{ textAlign: "center" as const }}>
              <div style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.gold, letterSpacing: "-0.02em" }}>{total}</div>
              <div style={{ fontSize: 11, color: A.muted }}>{filterStatus === "all" ? "Total" : filterStatus[0].toUpperCase() + filterStatus.slice(1)}</div>
            </div>
          </div>
        </div>

        <div style={{ display: "flex", gap: 10, marginBottom: 20, alignItems: "center" }}>
          <Filter size={14} style={{ color: A.muted }} />
          <select value={filterCountry} onChange={e => setCountry(e.target.value)} style={selectStyle}>
            <option value="all">All countries</option>
            {countries.map(c => <option key={c}>{c}</option>)}
          </select>
          <select value={filterScore} onChange={e => setScore(e.target.value)} style={selectStyle}>
            <option value="all">All scores</option>
            <option value="critical">Critical (&lt;5.0)</option>
            <option value="low">Low (5.0–6.9)</option>
          </select>
          <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)} style={selectStyle}>
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="all">All</option>
          </select>
          <div style={{ flex: 1 }} />
          <Btn variant="ghost" size="sm" onClick={load}><RotateCcw size={12} /> Refresh</Btn>
        </div>

        {/* AA-626 follow-up: active deep-link filter from Master Content */}
        {tourFilter && (
          <div style={{
            display: "flex", alignItems: "center", gap: 10, marginBottom: 16,
            padding: "8px 14px", borderRadius: 8, background: A.goldTint,
            border: `1px solid ${A.gold}`, fontSize: 12, color: A.ink,
          }}>
            <Filter size={13} style={{ color: A.gold }} />
            <span>
              Showing failed versions for <strong>{tourFilterName || "one selected tour"}</strong> only.
            </span>
            <button
              onClick={() => { setTourFilter(null); setTourFilterName(null); }}
              style={{
                marginLeft: "auto", border: `1px solid ${A.line}`, borderRadius: 6,
                background: A.card, cursor: "pointer", padding: "4px 10px", fontSize: 12, color: A.body,
                display: "inline-flex", alignItems: "center", gap: 5,
              }}
            >
              <X size={12} /> Clear filter
            </button>
          </div>
        )}

        {error && (
          <div style={{ fontSize: 13, padding: "10px 14px", borderRadius: 8, marginBottom: 16, background: A.redSoft, color: A.red, border: "1px solid #FECACA" }}>
            {error}
          </div>
        )}

        {/* Legend: what the reason-chip colors mean */}
        {!loading && filtered.length > 0 && (
          <div style={{ display: "flex", gap: 16, alignItems: "center", marginBottom: 12, fontSize: 11, color: A.muted }}>
            <span style={{ fontWeight: 600 }}>Reason colors:</span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span style={{ width: 10, height: 10, borderRadius: 3, background: SEV_STYLE.red.bg, border: `1px solid ${SEV_STYLE.red.border}`, display: "inline-block" }} />
              Product-truth / structural (real block)
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span style={{ width: 10, height: 10, borderRadius: 3, background: SEV_STYLE.amber.bg, border: `1px solid ${SEV_STYLE.amber.border}`, display: "inline-block" }} />
              Brand / style / SEO (often fixable)
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span style={{ width: 10, height: 10, borderRadius: 3, background: SEV_STYLE.gray.bg, border: `1px solid ${SEV_STYLE.gray.border}`, display: "inline-block" }} />
              Other
            </span>
          </div>
        )}

        {loading ? <LoadingScreen msg="Loading review queue…" /> : filtered.length === 0 ? (
          <div style={{ textAlign: "center" as const, padding: "60px 0" }}>
            <CheckCircle size={40} style={{ margin: "0 auto 12px", color: A.green, display: "block" }} />
            <div style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink }}>Review queue is empty</div>
            <div style={{ fontSize: 13, color: A.muted, marginTop: 6 }}>Every tour has been reviewed.</div>
          </div>
        ) : (
          <Card style={{ padding: 0, overflow: "visible" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead style={{ position: "sticky", top: 0, zIndex: 5 }}>
                <tr style={{ background: A.line2 }}>
                  <th style={{ ...TH, width: 34, paddingLeft: 16, background: A.line2 }}></th>
                  <th style={{ ...TH, cursor: "pointer", background: A.line2 }} onClick={() => toggleSort("tour")}>Tour{sortArrow("tour")}</th>
                  <th style={{ ...TH, cursor: "pointer", background: A.line2 }} onClick={() => toggleSort("country")}>Country{sortArrow("country")}</th>
                  <th style={{ ...TH, cursor: "pointer", background: A.line2 }} onClick={() => toggleSort("version")}>Version{sortArrow("version")}</th>
                  <th style={{ ...TH, cursor: "pointer", background: A.line2 }} onClick={() => toggleSort("date")}>Written{sortArrow("date")}</th>
                  <th style={{ ...TH, cursor: "pointer", background: A.line2 }} onClick={() => toggleSort("score")}>Score{sortArrow("score")}</th>
                  <th style={{ ...TH, background: A.line2 }}>Reasons</th>
                  <th style={{ ...TH, background: A.line2 }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map(item => (
                  <ReviewRow key={item.id} item={item}
                    expanded={expandedId === item.id}
                    onToggle={() => setExpandedId(prev => prev === item.id ? null : item.id)}
                    onApprove={onApprove} onReject={onReject} onDismiss={onDismiss}
                    onRegenerate={setRegenTarget}
                    onSaved={onSaved} onRevalidated={onRevalidated}
                    colSpan={REVIEW_COLS} />
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </main>

      {regenTarget && (
        <RegenerateModal item={regenTarget} onClose={() => setRegenTarget(null)} onReload={load} />
      )}
    </div>
  );
}

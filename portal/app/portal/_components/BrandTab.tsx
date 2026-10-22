"use client";
// portal/app/portal/_components/BrandTab.tsx
// API: GET  /api/tenant/admin/brand-identity          (AA-424: was /api/admin/brand-identity,
//      POST /api/tenant/admin/brand-identity           which requireAdmin()-gated and 401'd every
//                                                       real tenant session — see AA-423/424.
//                                                       Routed through the tenant proxy so
//                                                       cis_tenant_token reaches the now
//                                                       tenant-JWT-aware backend endpoint.)
//      POST /api/tenant/admin/brand-identity/upload    (AA-441: was /api/admin/brand-identity/
//                                                        upload — same 401 class as above, PLUS
//                                                        the backend itself hardcoded AA-internal
//                                                        tenant_id and expected a JSON
//                                                        {filename,content_type} body → presigned
//                                                        S3 URL, never actually receiving the
//                                                        file this component always sent as
//                                                        multipart. Backend now accepts the
//                                                        multipart file directly and resolves
//                                                        tenant_id from the caller; the tenant
//                                                        proxy now passes multipart through
//                                                        unmangled instead of forcing JSON.)

import { useState, useEffect, useRef } from "react";
import { History, Upload, Check, RotateCcw } from "lucide-react";
import {
  T, serif, sans, mono,
  Btn, Field, LoadingScreen,
  fmtDate,
} from "./ui";
import { CtaReviewNotice, ctaWarningFrom, useCtaReviewCheck, type CtaWarning } from "./CtaReviewNotice";

interface BrandData {
  configured: boolean; system_prompt: string; style_guide: string;
  forbidden_words: string[] | string; version: number; updated_at: string;
  history: { version: number; is_active: boolean; system_prompt: string; style_guide: string; forbidden_words: string[]; updated_at: string }[];
  // AA-557 J.23 — same full field set Admin's own /admin/brand page has, mirrored here so
  // Admin's Tenant→Brand tab (J.24) and this page read/write the SAME shared.tenant_brand_rules
  // row, not 2 separate schemas that need syncing.
  brand_name: string; brand_type: string; core_idea: string;
  customer_segment: string; customer_mindset: string;
  tone_of_voice: string[]; good_examples: string; target_markets: string[];
  default_cta?: string;
}

// Single-line text input, same visual language as Field (textarea) above.
function TextField({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>{label}</label>
      <input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{ width: "100%", padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, outline: "none", boxSizing: "border-box", fontFamily: sans }} />
    </div>
  );
}

// Comma-separated multi-value field (same convention the pre-existing Forbidden Words field
// below already uses) — for Target Markets / Tone of Voice.
function TagListField({ label, value, onChange, placeholder, hint }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; hint?: string;
}) {
  const tags = value.split(",").map(w => w.trim()).filter(Boolean);
  return (
    <div style={{ marginBottom: 16 }}>
      <label style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>{label}</label>
      <input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{ width: "100%", padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, outline: "none", boxSizing: "border-box", fontFamily: sans }} />
      <div style={{ fontSize: 11, color: T.muted2, marginTop: 5 }}>{hint ?? "Comma-separated."}</div>
      {tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
          {tags.map(t => (
            <span key={t} style={{ fontSize: 11, padding: "2px 8px", borderRadius: 20, background: T.goldTint, color: T.amber }}>{t}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export default function BrandTab() {
  const [data, setData]     = useState<BrandData | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving]   = useState(false);
  const [saved, setSaved]     = useState(false);
  const [dirty, setDirty]     = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [viewingV, setViewingV] = useState<number | null>(null);
  const [uploadStatus, setUploadStatus] = useState<"idle" | "uploading" | "done" | "stored" | "error">("idle");
  // AA-741 (S229): what the brief upload found, and what the last save did with the markets.
  const [briefNotes, setBriefNotes] = useState<string[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [marketNote, setMarketNote] = useState<{ countries: string[]; unsupported: string[]; unknown: string[] } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Form state
  const [sp, setSp]   = useState("");
  const [sg, setSg]   = useState("");
  const [fw, setFw]   = useState("");  // comma-separated
  // AA-557 J.23 — the extended field set.
  const [brandName, setBrandName] = useState("");
  const [brandType, setBrandType] = useState("");
  const [coreIdea, setCoreIdea] = useState("");
  const [targetMarkets, setTargetMarkets] = useState("");
  const [customerSegment, setCustomerSegment] = useState("");
  const [customerMindset, setCustomerMindset] = useState("");
  const [toneOfVoice, setToneOfVoice] = useState("");
  const [goodExamples, setGoodExamples] = useState("");
  const [defaultCta, setDefaultCta] = useState("");
  // AA-819 follow-up — a CTA that mentions reviews or ratings is saved as typed, with a warning:
  // live while typing, and from the save response.
  const liveCtaWarning = useCtaReviewCheck(defaultCta);
  const [savedCtaWarning, setSavedCtaWarning] = useState<CtaWarning>(null);

  const load = async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/tenant/admin/brand-identity");
      if (r.ok) {
        const d: BrandData = await r.json();
        setData(d);
        setSp(d.system_prompt ?? "");
        setSg(d.style_guide ?? "");
        const words = Array.isArray(d.forbidden_words) ? d.forbidden_words : (typeof d.forbidden_words === "string" ? JSON.parse(d.forbidden_words || "[]") : []);
        setFw(words.join(", "));
        setBrandName(d.brand_name ?? "");
        setBrandType(d.brand_type ?? "");
        setCoreIdea(d.core_idea ?? "");
        setTargetMarkets((d.target_markets ?? []).join(", "));
        setCustomerSegment(d.customer_segment ?? "");
        setCustomerMindset(d.customer_mindset ?? "");
        setToneOfVoice((d.tone_of_voice ?? []).join(", "));
        setGoodExamples(d.good_examples ?? "");
        setDefaultCta(d.default_cta ?? "");
        setDirty(false);
      }
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  function markDirty() { setDirty(true); setSaved(false); }

  async function save() {
    setSaving(true); setSaved(false); setSaveError(null);
    try {
      const r = await fetch("/api/tenant/admin/brand-identity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_prompt: sp,
          style_guide: sg,
          forbidden_words: fw.split(",").map(w => w.trim()).filter(Boolean),
          brand_name: brandName,
          brand_type: brandType,
          core_idea: coreIdea,
          target_markets: targetMarkets.split(",").map(w => w.trim()).filter(Boolean),
          customer_segment: customerSegment,
          customer_mindset: customerMindset,
          tone_of_voice: toneOfVoice.split(",").map(w => w.trim()).filter(Boolean),
          good_examples: goodExamples,
          default_cta: defaultCta,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (r.ok) {
        setSaved(true); setDirty(false); setViewingV(null); setBriefNotes([]);
        setMarketNote(body.markets ?? null);
        setSavedCtaWarning(ctaWarningFrom(body));
        await load();
      } else {
        // AA-741 (S229): was silently ignored — the tenant saw nothing when a save failed.
        setSaveError(typeof body.detail === "string" ? body.detail : "Could not save. Please try again.");
      }
    } catch {
      setSaveError("Could not save. Please check your connection and try again.");
    } finally { setSaving(false); }
  }

  function restoreVersion(v: NonNullable<BrandData["history"]>[0]) {
    setSp(v.system_prompt ?? ""); setSg(v.style_guide ?? "");
    setFw((v.forbidden_words ?? []).join(", "));
    setDirty(true); setSaved(false); setViewingV(v.version);
  }

  // AA-741 (S229): a .docx brief is READ and pre-fills the form for review (nothing is saved until
  // the tenant presses Save). Before, the upload only stored the file and the form stayed empty.
  // The original file is still archived; a PDF is archived only (automatic reading is .docx).
  async function handleUpload(file: File) {
    setUploadStatus("uploading"); setBriefNotes([]);
    const isDocx = file.name.toLowerCase().endsWith(".docx");
    try {
      if (file.size > 5 * 1024 * 1024) {
        setUploadStatus("error"); setBriefNotes(["The file is larger than 5 MB."]);
        return;
      }
      // Sent as base64 JSON, not multipart: the API Gateway in front of the API corrupts binary
      // multipart bodies (AA-741, S229).
      const file_base64 = await toBase64(file);
      const payload = { filename: file.name, content_type: file.type || (isDocx
        ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/pdf"),
        file_base64 };
      const stored = fetch("/api/tenant/admin/brand-identity/upload", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      }).catch(() => null);
      if (!isDocx) {
        const r = await stored;
        setUploadStatus(r?.ok ? "stored" : "error");
        return;
      }
      const r = await fetch("/api/tenant/admin/brand-identity/parse", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, file_base64 }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setUploadStatus("error");
        setBriefNotes([typeof body.detail === "string" ? body.detail : "Could not read this file."]);
        return;
      }
      const f = body.fields ?? {};
      if (f.brand_name) setBrandName(f.brand_name);
      if (f.brand_type) setBrandType(f.brand_type);
      if (f.core_idea) setCoreIdea(f.core_idea);
      if (f.target_markets?.length) setTargetMarkets(f.target_markets.join(", "));
      if (f.customer_segment) setCustomerSegment(f.customer_segment);
      if (f.customer_mindset) setCustomerMindset(f.customer_mindset);
      if (f.tone_of_voice?.length) setToneOfVoice(f.tone_of_voice.join(", "));
      if (f.style_guide) setSg(f.style_guide);
      if (f.good_examples) setGoodExamples(f.good_examples);
      if (f.forbidden_words?.length) setFw(f.forbidden_words.join(", "));
      const notes: string[] = [...(body.warnings ?? [])];
      const m = body.markets;
      if (m?.unsupported?.length) {
        notes.push(`Market research is not available yet for: ${m.unsupported.join(", ")}. Content `
          + `for these is written with keywords from ${m.supported?.length ? m.supported.join(", ") : "the US"}.`);
      }
      if (m?.unknown?.length) notes.push(`Not recognised as a market: ${m.unknown.join(", ")}.`);
      setBriefNotes(notes);
      setUploadStatus("done");
      markDirty();
      await stored;               // keep the original file archived
    } catch { setUploadStatus("error"); }
  }

  const wordCount = fw.split(",").map(w => w.trim()).filter(Boolean).length;

  if (loading) return <LoadingScreen message="Loading brand identity…" />;

  return (
    <div style={{ display: "grid", gridTemplateColumns: showHistory ? "1fr 300px" : "1fr", gap: 24, alignItems: "start" }}>

      {/* MAIN FORM */}
      <div>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 22 }}>
          <div>
            <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Brand Identity</h2>
            <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
              Rules are appended to AA&apos;s 29 core brand standards on every rewrite. They do not override quality thresholds.
            </p>
          </div>
          {data?.version && (
            <button onClick={() => setShowHistory(!showHistory)}
              style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", borderRadius: 8, fontSize: 12, border: `1px solid ${T.line}`, background: T.card, color: T.muted, cursor: "pointer", fontFamily: sans, fontWeight: 500, flexShrink: 0 }}>
              <History size={13} /> History (v{data.version})
            </button>
          )}
        </div>

        {/* Active version indicator */}
        {data?.configured && data?.version && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 14px", marginBottom: 22, background: "var(--aa-green-bg2)", border: "1px solid var(--aa-green-border)", borderRadius: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: T.green, flexShrink: 0 }} />
            <span style={{ fontSize: 12.5, color: T.green, fontWeight: 600 }}>Active — Version {data.version}</span>
            {data.updated_at && <span style={{ fontSize: 11, color: T.muted, marginLeft: "auto" }}>Last updated {fmtDate(data.updated_at)}</span>}
          </div>
        )}

        {/* Viewing old version banner */}
        {viewingV !== null && (
          <div style={{ padding: "10px 14px", marginBottom: 18, background: T.amberSoft, border: `1px solid ${T.goldSoft}`, borderRadius: 8, display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 13, color: T.amber }}>Viewing v{viewingV} — unsaved changes</span>
            <button onClick={() => { load(); setViewingV(null); }} style={{ marginLeft: "auto", fontSize: 12, color: T.amber, background: "none", border: "none", cursor: "pointer", fontFamily: sans, fontWeight: 600 }}>← Back to active</button>
          </div>
        )}

        {/* Fields — AA-557 J.23: extended to match Admin's own /admin/brand page structure. */}
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, padding: 22, marginBottom: 16 }}>
          <TextField label="Brand Name" value={brandName} onChange={v => { setBrandName(v); markDirty(); }}
            placeholder="e.g. WanderLux Travel" />
          <TextField label="Brand Type" value={brandType} onChange={v => { setBrandType(v); markDirty(); }}
            placeholder="e.g. Luxury private-travel operator" />
          <Field
            label="Core Idea" rows={2}
            placeholder="The single idea every piece of content should reinforce."
            value={coreIdea} onChange={v => { setCoreIdea(v); markDirty(); }}
          />
          <TagListField label="Target Markets" value={targetMarkets} onChange={v => { setTargetMarkets(v); markDirty(); }}
            placeholder="US, UK, Europe" hint="Comma-separated countries or regions. Europe = UK, Germany, France, Netherlands. Sets the keywords and research your content is written for." />
          <Field
            label="Customer Segment" rows={2}
            placeholder="Who buys this — demographics, income, travel habits."
            value={customerSegment} onChange={v => { setCustomerSegment(v); markDirty(); }}
          />
          <Field
            label="Customer Mindset" rows={2}
            placeholder="What they're thinking/feeling when they decide to book."
            value={customerMindset} onChange={v => { setCustomerMindset(v); markDirty(); }}
          />
          <TagListField label="Tone of Voice" value={toneOfVoice} onChange={v => { setToneOfVoice(v); markDirty(); }}
            placeholder="confident, warm, precise" />
          <Field
            label="Writing Style" rows={4}
            placeholder="e.g. Use active voice. Prefer concrete specifics over adjectives. Keep sentences under 25 words."
            value={sg} onChange={v => { setSg(v); markDirty(); }}
          />
          <Field
            label="Good Examples" rows={3}
            placeholder="Paste 1-2 short passages that read exactly the way this brand should sound."
            value={goodExamples} onChange={v => { setGoodExamples(v); markDirty(); }}
          />
          <TextField label="Default Call to Action" value={defaultCta}
            onChange={v => { setDefaultCta(v); setSavedCtaWarning(null); markDirty(); }}
            placeholder="e.g. Ask us for the full itinerary and dates" />
          <CtaReviewNotice warning={liveCtaWarning ?? savedCtaWarning} style={{ marginTop: -6, marginBottom: 16 }} />
          <Field
            label="Should Write (System Prompt)" rows={5}
            placeholder="e.g. We are a luxury private-travel operator for US/UK professionals aged 40–60. Emphasise depth of experience, exclusivity, and cultural immersion."
            value={sp} onChange={v => { setSp(v); markDirty(); }}
            hint={`${sp.length}/2000`} hintColor={sp.length > 1800 ? T.red : T.muted2}
          />
          <div style={{ marginBottom: 0 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
              <label style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>Forbidden Words</label>
              <span style={{ fontSize: 11, color: T.muted2 }}>{wordCount} words</span>
            </div>
            <input value={fw}
              onChange={e => { setFw(e.target.value); markDirty(); }}
              placeholder="cheap, budget, bargain, amazing, incredible, stunning"
              style={{ width: "100%", padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans, outline: "none", boxSizing: "border-box" }} />
            <div style={{ fontSize: 11, color: T.muted2, marginTop: 5 }}>Comma-separated. Applied as post-process validation on all rewrites.</div>
          </div>
        </div>

        {/* Preview */}
        {(sp || sg || fw) && (
          <div style={{ background: T.bg, border: `1px solid ${T.line}`, borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
            <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>How it applies to rewrites</div>
            <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.7 }}>
              {sp && <div style={{ marginBottom: 5 }}><strong style={{ color: T.ink }}>Brand context:</strong> {sp.slice(0, 120)}{sp.length > 120 ? "…" : ""}</div>}
              {sg && <div style={{ marginBottom: 5 }}><strong style={{ color: T.ink }}>Style:</strong> {sg.slice(0, 80)}{sg.length > 80 ? "…" : ""}</div>}
              {fw && (
                <div>
                  <strong style={{ color: T.ink }}>Will avoid:</strong>{" "}
                  {fw.split(",").filter(Boolean).map(w => w.trim()).slice(0, 8).map(w => (
                    <span key={w} style={{ display: "inline-block", margin: "1px 3px", padding: "1px 7px", borderRadius: 20, fontSize: 11, background: T.redSoft, color: T.red }}>{w}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Upload */}
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, padding: 22, marginBottom: 22 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 14 }}>Upload Brand Guide (Optional)</div>
          <input type="file" ref={fileRef} accept=".pdf,.docx" style={{ display: "none" }}
            onChange={e => { const f = e.target.files?.[0]; if (f) handleUpload(f); }} />
          <div onClick={() => fileRef.current?.click()}
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) handleUpload(f); }}
            style={{ border: `2px dashed ${T.goldSoft}`, borderRadius: 8, background: T.goldTint, padding: "28px 20px", textAlign: "center", cursor: "pointer" }}>
            <Upload size={24} color={T.gold} style={{ margin: "0 auto 10px" }} />
            <div style={{ fontSize: 13.5, color: T.ink, fontWeight: 500 }}>
              {uploadStatus === "uploading" ? "Uploading…" :
               uploadStatus === "done"      ? "Brief read — review the fields above, then save" :
               uploadStatus === "stored"    ? "PDF stored — automatic reading works with .docx; fill in the fields above" :
               uploadStatus === "error"     ? "⚠ Upload failed — try again" :
               "Drag PDF or DOCX brand guide here"}
            </div>
            <div style={{ fontSize: 11, color: T.muted2, fontFamily: mono, marginTop: 6 }}>DOCX is read automatically · PDF is stored only · Max 5MB</div>
          </div>
        </div>

        {briefNotes.length > 0 && (
          <div role="status" style={{ padding: "12px 14px", marginBottom: 16, background: T.amberSoft, border: `1px solid ${T.goldSoft}`, borderRadius: 8 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: T.amber, marginBottom: 6 }}>Check before saving</div>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: T.body, lineHeight: 1.6 }}>
              {briefNotes.map(n => <li key={n}>{n}</li>)}
            </ul>
          </div>
        )}

        {/* Save row */}
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <Btn variant="primary" size="lg" disabled={saving || !dirty} onClick={save}>
            {saving ? "Saving…" : "Save as New Version"}
          </Btn>
          {saved && <span style={{ fontSize: 13, color: T.green, fontWeight: 600 }}><Check size={14} style={{ display: "inline" }} /> Saved — active on next pipeline run</span>}
          {dirty && !saved && !saveError && <span style={{ fontSize: 13, color: T.amber }}>● Unsaved changes</span>}
          {saveError && <span role="alert" style={{ fontSize: 13, color: T.red }}>{saveError}</span>}
          {saved && marketNote && (
            <span style={{ fontSize: 12.5, color: T.muted, width: "100%" }}>
              Markets in use: {marketNote.countries.filter(c => !marketNote.unsupported.includes(c)).join(", ") || "US (default)"}
              {marketNote.unsupported.length > 0 && ` · research not available yet for ${marketNote.unsupported.join(", ")}`}
            </span>
          )}
        </div>
      </div>

      {/* VERSION HISTORY SIDEBAR */}
      {showHistory && data?.history && (
        <div style={{ background: T.card, border: `1px solid ${T.line}`, borderRadius: 12, overflow: "hidden", position: "sticky", top: 20 }}>
          <div style={{ padding: "14px 16px", borderBottom: `1px solid ${T.line}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>Version History</div>
            <button onClick={() => setShowHistory(false)} style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, fontSize: 18 }}>×</button>
          </div>
          <div style={{ padding: 14, maxHeight: "70vh", overflowY: "auto" }}>
            {data.history.map(h => (
              <div key={h.version} style={{ borderRadius: 8, marginBottom: 10, background: h.is_active ? "var(--aa-green-bg2)" : T.bg, border: `1px solid ${h.is_active ? "var(--aa-green-border)" : T.line}`, overflow: "hidden" }}>
                <div style={{ padding: "10px 12px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <span style={{ fontSize: 14, fontWeight: 700, color: h.is_active ? T.green : T.ink }}>v{h.version}</span>
                    {h.is_active && <span style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, color: T.green, background: T.greenSoft, padding: "1px 6px", borderRadius: 10 }}>ACTIVE</span>}
                    <div style={{ fontSize: 10.5, color: T.muted2, marginTop: 3, fontFamily: mono }}>{fmtDate(h.updated_at)}</div>
                  </div>
                  {!h.is_active && (
                    <button onClick={() => restoreVersion(h)} style={{ fontSize: 11, padding: "4px 10px", borderRadius: 6, border: `1px solid ${T.goldSoft}`, background: T.goldTint, color: T.amber, cursor: "pointer", fontFamily: sans, fontWeight: 600 }}>
                      <RotateCcw size={10} style={{ display: "inline", marginRight: 3 }} />Restore
                    </button>
                  )}
                </div>
                {h.system_prompt && (
                  <div style={{ padding: "0 12px 10px", borderTop: `1px solid ${T.line}`, paddingTop: 8 }}>
                    <div style={{ fontSize: 10.5, color: T.muted2, marginBottom: 3 }}>Brand context</div>
                    <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.5 }}>
                      {h.system_prompt.slice(0, 100)}{h.system_prompt.length > 100 ? "…" : ""}
                    </div>
                    {h.forbidden_words?.length > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <div style={{ fontSize: 10, color: T.muted2, marginBottom: 3 }}>Forbidden</div>
                        <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
                          {h.forbidden_words.slice(0, 5).map(w => (
                            <span key={w} style={{ fontSize: 10, padding: "1px 7px", background: T.redSoft, color: T.red, borderRadius: 20 }}>{w}</span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
            <div style={{ fontSize: 11, color: T.muted2, lineHeight: 1.5, padding: "0 4px", marginTop: 4 }}>
              Click <strong>Restore</strong> to load a previous version into the editor, then save as a new version.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

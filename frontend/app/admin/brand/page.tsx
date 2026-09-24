"use client";
// app/admin/brand/page.tsx — Brand Identity v3 (AA-129)
// GET  /api/admin/brands         → list brands
// GET  /api/admin/brands/{name}  → brand detail + history (with full version data)
// POST /api/admin/brands         → create brand
// PUT  /api/admin/brands/{name}  → update brand (new version)
// POST /api/admin/brands/{name}/activate → {version: N} → set version as active
// DELETE /api/admin/brands/{name} → soft delete

import { useState, useEffect, useCallback, useRef } from "react";
import { Plus, Trash2, ChevronDown, ChevronUp, RefreshCw, Upload } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, Card, SLabel, Btn } from "../_components/adminUi";

// ─── Types ────────────────────────────────────────────────────────────────────

interface BrandSummary {
  brand_name: string;
  brand_type: string | null;
  core_idea: string | null;
  version: number;
  is_active: boolean;
  updated_at: string | null;
}

interface VersionHistoryEntry {
  version: number;
  is_active: boolean;
  updated_at: string | null;
  brand_type: string;
  core_idea: string;
  customer_segment: string;
  customer_mindset: string;
  tone_of_voice: string[];
  writing_style: string;
  should_write: string;
  forbidden_words: string[];
  target_markets: string[];
  rewrite_language: string;
}

interface BrandDetail {
  brand_name: string;
  brand_type: string;
  core_idea: string;
  customer_segment: string;
  customer_mindset: string;
  tone_of_voice: string[];
  writing_style: string;
  good_examples: string;
  should_write: string;
  forbidden_words: string[];
  target_markets: string[];
  rewrite_language: string;
  version: number;
  is_active: boolean;
  updated_at: string | null;
  history: VersionHistoryEntry[];
}

// ─── Tags input ───────────────────────────────────────────────────────────────

function TagsInput({ value, onChange, placeholder, readOnly }: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  readOnly?: boolean;
}) {
  const [input, setInput] = useState("");
  function add() {
    const trimmed = input.trim();
    if (trimmed && !value.includes(trimmed)) onChange([...value, trimmed]);
    setInput("");
  }
  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
        {value.map(t => (
          <span key={t} style={{
            display: "inline-flex", alignItems: "center", gap: 4,
            padding: "2px 8px", borderRadius: 20, fontSize: 12,
            background: A.goldTint, color: A.gold, fontWeight: 600,
          }}>
            {t}
            {!readOnly && (
              <button onClick={() => onChange(value.filter(x => x !== t))} style={{
                background: "none", border: "none", cursor: "pointer", padding: 0,
                color: A.gold, lineHeight: 1, fontSize: 13,
              }}>×</button>
            )}
          </span>
        ))}
      </div>
      {!readOnly && (
        <div style={{ display: "flex", gap: 6 }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); } }}
            placeholder={placeholder || "Type and press Enter…"}
            style={{
              flex: 1, padding: "7px 10px", border: `1px solid ${A.line}`,
              borderRadius: 6, fontSize: 13, fontFamily: sans,
              background: "#fff", color: A.ink, outline: "none",
            }}
          />
          <Btn variant="secondary" onClick={add} style={{ padding: "7px 12px" }}>Add</Btn>
        </div>
      )}
    </div>
  );
}

// ─── Field helpers ────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <label style={{ display: "block", fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </label>
      {children}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", border: `1px solid ${A.line}`,
  borderRadius: 6, fontSize: 13, fontFamily: sans,
  background: "#fff", color: A.ink, outline: "none", boxSizing: "border-box",
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle, resize: "vertical" as const,
};

// ─── Brand form state ─────────────────────────────────────────────────────────

interface BrandForm {
  brand_name: string;
  brand_type: string;
  core_idea: string;
  target_markets: string[];
  customer_segment: string;
  customer_mindset: string;
  tone_of_voice: string[];
  writing_style: string;
  good_examples: string;
  should_write: string;
  forbidden_words: string[];
  rewrite_language: string;
}

function emptyForm(): BrandForm {
  return {
    brand_name: "", brand_type: "", core_idea: "",
    target_markets: [], customer_segment: "", customer_mindset: "",
    tone_of_voice: [], writing_style: "", good_examples: "",
    should_write: "", forbidden_words: [], rewrite_language: "en",
  };
}

function detailToForm(d: BrandDetail): BrandForm {
  return {
    brand_name: d.brand_name,
    brand_type: d.brand_type || "",
    core_idea: d.core_idea || "",
    target_markets: d.target_markets || [],
    customer_segment: d.customer_segment || "",
    customer_mindset: d.customer_mindset || "",
    tone_of_voice: d.tone_of_voice || [],
    writing_style: d.writing_style || "",
    good_examples: d.good_examples || "",
    should_write: d.should_write || "",
    forbidden_words: d.forbidden_words || [],
    rewrite_language: d.rewrite_language || "en",
  };
}

function versionToForm(brand_name: string, v: VersionHistoryEntry): BrandForm {
  return {
    brand_name,
    brand_type: v.brand_type || "",
    core_idea: v.core_idea || "",
    target_markets: v.target_markets || [],
    customer_segment: v.customer_segment || "",
    customer_mindset: v.customer_mindset || "",
    tone_of_voice: v.tone_of_voice || [],
    writing_style: v.writing_style || "",
    good_examples: "",
    should_write: v.should_write || "",
    forbidden_words: v.forbidden_words || [],
    rewrite_language: v.rewrite_language || "en",
  };
}

// ─── DOCX parser — HTML mode ──────────────────────────────────────────────────

const KNOWN_HEADINGS = [
  "Brand Identity", "Target Market", "Target Markets",
  "Tone of Voice", "Writing Style",
  "Good Example", "Good Examples",
  "Should Write", "Should Not Write",
];

function isHeading(p: string) {
  return KNOWN_HEADINGS.some(h => p.toLowerCase() === h.toLowerCase());
}

function getInlineLabel(p: string): { key: string; value: string } | null {
  const LABELED_KEYS = ["Brand type", "Core idea", "Primary markets", "Customer segment", "Customer mindset"];
  for (const k of LABELED_KEYS) {
    if (p.toLowerCase().startsWith(k.toLowerCase() + ":")) {
      const colon = p.indexOf(":");
      return { key: k.toLowerCase(), value: p.slice(colon + 1).trim() };
    }
  }
  return null;
}

// Convert mammoth HTML to a flat array of clean paragraph strings.
// Normalises <br> → paragraph boundaries and <li> → paragraphs so that
// traits / rules spread across <br>-joined lines become separate entries.
function htmlToParagraphs(html: string): string[] {
  const normalised = html
    .replace(/<br\s*\/?>/gi, "</p><p>")
    .replace(/<\/li>/gi, "</p>")
    .replace(/<li[^>]*>/gi, "<p>");
  const parser = new DOMParser();
  const doc = parser.parseFromString(normalised, "text/html");
  return Array.from(doc.querySelectorAll("p, h1, h2, h3, h4, h5, h6"))
    .map(el => el.textContent?.trim() || "")
    .filter(Boolean);
}

function parseBrandText(paragraphs: string[]): Partial<BrandForm> {
  const result: Partial<BrandForm> = {};

  // Brand name: first non-empty paragraph that is NOT a known heading, NOT a cis_ key, has no ":"
  for (const p of paragraphs) {
    if (!p || /^cis_[A-Za-z0-9]/.test(p) || isHeading(p) || p.includes(":")) continue;
    result.brand_name = p;
    break;
  }

  // Collect subsequent paragraphs after index startIdx until the next heading or labeled field.
  // Stops at a heading or another inline label — returns collected lines joined by sep.
  function collectAfter(startIdx: number, sep = "\n"): string {
    const parts: string[] = [];
    for (let j = startIdx + 1; j < paragraphs.length; j++) {
      const next = paragraphs[j];
      if (!next) continue;
      if (isHeading(next) || getInlineLabel(next)) break;
      parts.push(next);
    }
    return parts.join(sep);
  }

  let i = 0;
  while (i < paragraphs.length) {
    const p = paragraphs[i];
    if (!p) { i++; continue; }

    const labeled = getInlineLabel(p);
    if (labeled) {
      switch (labeled.key) {
        case "brand type":
          // inline value or next paragraph (when label is a standalone bold heading)
          result.brand_type = labeled.value || collectAfter(i, " ");
          break;

        case "core idea":
          // inline value or next paragraph; do NOT collect multiple (one sentence field)
          result.core_idea = labeled.value || collectAfter(i, " ");
          break;

        case "primary markets": {
          const raw = labeled.value || collectAfter(i, ",");
          if (raw) result.target_markets = raw.split(/[,;]/).map(s => s.trim()).filter(Boolean);
          break;
        }

        case "customer segment": {
          // May have inline start + continuation lines (e.g. "25-40 years old\nSolo travellers...")
          const continuation = collectAfter(i);
          result.customer_segment = [labeled.value, continuation].filter(Boolean).join("\n");
          break;
        }

        case "customer mindset": {
          const continuation = collectAfter(i);
          result.customer_mindset = [labeled.value, continuation].filter(Boolean).join("\n");
          break;
        }
      }
      i++;
      continue;
    }

    // Section headings — collect everything until the next known heading
    if (/^tone of voice$/i.test(p)) {
      const traits: string[] = [];
      for (let j = i + 1; j < paragraphs.length; j++) {
        const next = paragraphs[j];
        if (isHeading(next)) break;
        if (next) traits.push(next);  // each paragraph = one trait
      }
      if (traits.length) result.tone_of_voice = traits;
    }

    if (/^writing style$/i.test(p)) {
      const parts: string[] = [];
      for (let j = i + 1; j < paragraphs.length; j++) {
        const next = paragraphs[j];
        if (isHeading(next)) break;
        if (next) parts.push(next);
      }
      if (parts.length) result.writing_style = parts.join("\n");
    }

    if (/^good examples?$/i.test(p)) {
      const parts: string[] = [];
      for (let j = i + 1; j < paragraphs.length; j++) {
        const next = paragraphs[j];
        if (isHeading(next)) break;
        if (next) parts.push(next);
      }
      if (parts.length) result.good_examples = parts.join("\n");
    }

    if (/^should write$/i.test(p)) {
      const parts: string[] = [];
      for (let j = i + 1; j < paragraphs.length; j++) {
        const next = paragraphs[j];
        if (isHeading(next)) break;
        if (next) parts.push(next);
      }
      if (parts.length) result.should_write = parts.join("\n");
    }

    if (/^should not write$/i.test(p)) {
      const words: string[] = [];
      for (let j = i + 1; j < paragraphs.length; j++) {
        const next = paragraphs[j];
        if (isHeading(next)) break;
        if (next) words.push(next);  // each paragraph = one forbidden rule
      }
      if (words.length) result.forbidden_words = words;
    }

    i++;
  }

  return result;
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function AdminBrandPage() {
  const [brands, setBrands]           = useState<BrandSummary[]>([]);
  const [selected, setSelected]       = useState<string | null>(null);
  const [detail, setDetail]           = useState<BrandDetail | null>(null);
  const [form, setForm]               = useState<BrandForm>(emptyForm());
  const [isNew, setIsNew]             = useState(false);
  const [saving, setSaving]           = useState(false);
  const [msg, setMsg]                 = useState<{ text: string; ok: boolean } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [loading, setLoading]         = useState(true);
  const [deleting, setDeleting]       = useState(false);
  const [parsing, setParsing]         = useState(false);
  // viewingVersion: null = editing active version, N = viewing read-only version N
  const [viewingVersion, setViewingVersion] = useState<number | null>(null);
  const [activating, setActivating]   = useState(false);
  const docxRef                       = useRef<HTMLInputElement>(null);

  const loadBrands = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/admin/brands");
      if (r.ok) setBrands((await r.json()).brands ?? []);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { loadBrands(); }, [loadBrands]);

  async function selectBrand(name: string) {
    setSelected(name); setIsNew(false); setMsg(null); setHistoryOpen(false);
    setViewingVersion(null);
    const r = await fetch(`/api/admin/brands/${encodeURIComponent(name)}`);
    if (r.ok) {
      const d: BrandDetail = await r.json();
      setDetail(d);
      setForm(detailToForm(d));
    }
  }

  function startNew() {
    setSelected(null); setDetail(null);
    setForm(emptyForm()); setIsNew(true); setMsg(null);
    setViewingVersion(null);
  }

  function upd<K extends keyof BrandForm>(key: K, val: BrandForm[K]) {
    setForm(f => ({ ...f, [key]: val }));
  }

  function viewVersion(v: VersionHistoryEntry) {
    setViewingVersion(v.version);
    setForm(versionToForm(selected!, v));
    setMsg(null);
  }

  function exitVersionView() {
    setViewingVersion(null);
    if (detail) setForm(detailToForm(detail));
    setMsg(null);
  }

  async function activateVersion(version: number) {
    if (!selected) return;
    if (!confirm(`Set v${version} as the active version for "${selected}"?`)) return;
    setActivating(true); setMsg(null);
    try {
      const r = await fetch(`/api/admin/brands/${encodeURIComponent(selected)}/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version }),
      });
      if (r.ok) {
        await loadBrands();
        await selectBrand(selected);
        setViewingVersion(null);
        setMsg({ text: `v${version} activated ✓`, ok: true });
      } else {
        const e = await r.json().catch(() => ({}));
        setMsg({ text: e.detail || "Activate failed", ok: false });
      }
    } finally { setActivating(false); }
  }

  async function save() {
    setSaving(true); setMsg(null);
    try {
      const payload = { ...form };
      const method  = isNew ? "POST" : "PUT";
      const url     = isNew
        ? "/api/admin/brands"
        : `/api/admin/brands/${encodeURIComponent(selected!)}`;
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        setMsg({ text: isNew ? "Brand created ✓" : "Updated ✓ (new version saved)", ok: true });
        await loadBrands();
        if (isNew) { setIsNew(false); setSelected(form.brand_name); }
        await selectBrand(isNew ? form.brand_name : selected!);
      } else {
        const e = await r.json().catch(() => ({}));
        setMsg({ text: e.detail || "Save failed", ok: false });
      }
    } finally { setSaving(false); }
  }

  async function deleteBrand() {
    if (!selected) return;
    if (!confirm(`Delete brand "${selected}"? This will permanently remove all versions.`)) return;
    setDeleting(true); setMsg(null);
    try {
      const r = await fetch(`/api/admin/brands/${encodeURIComponent(selected)}`, { method: "DELETE" });
      if (r.ok) {
        setSelected(null); setDetail(null); setForm(emptyForm());
        setViewingVersion(null);
        await loadBrands();
      } else {
        const e = await r.json().catch(() => ({}));
        setMsg({ text: e.detail || "Delete failed", ok: false });
      }
    } finally { setDeleting(false); }
  }

  async function handleDocx(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setParsing(true); setMsg(null);
    try {
      const arrayBuf = await file.arrayBuffer();
      const mammoth = await import("mammoth");
      const result = await mammoth.convertToHtml({ arrayBuffer: arrayBuf });
      const paragraphs = htmlToParagraphs(result.value);
      const parsed = parseBrandText(paragraphs);
      const fieldCount = Object.keys(parsed).length;
      setForm(f => ({
        ...f,
        ...(parsed.brand_name          ? { brand_name: parsed.brand_name }             : {}),
        ...(parsed.brand_type          ? { brand_type: parsed.brand_type }             : {}),
        ...(parsed.core_idea           ? { core_idea: parsed.core_idea }               : {}),
        ...(parsed.customer_segment    ? { customer_segment: parsed.customer_segment } : {}),
        ...(parsed.customer_mindset    ? { customer_mindset: parsed.customer_mindset } : {}),
        ...(parsed.writing_style       ? { writing_style: parsed.writing_style }       : {}),
        ...(parsed.good_examples       ? { good_examples: parsed.good_examples }       : {}),
        ...(parsed.should_write        ? { should_write: parsed.should_write }         : {}),
        ...(parsed.target_markets?.length  ? { target_markets: parsed.target_markets }   : {}),
        ...(parsed.tone_of_voice?.length   ? { tone_of_voice: parsed.tone_of_voice }     : {}),
        ...(parsed.forbidden_words?.length ? { forbidden_words: parsed.forbidden_words } : {}),
      }));
      setIsNew(true);
      setMsg({ text: `DOCX parsed — ${fieldCount} field(s) filled. Review and save.`, ok: true });
    } catch {
      setMsg({ text: "DOCX parse failed", ok: false });
    } finally {
      setParsing(false);
      if (docxRef.current) docxRef.current.value = "";
    }
  }

  const canSave = isNew ? form.brand_name.trim().length > 0 : selected != null;
  const isReadOnly = viewingVersion !== null;
  const viewedEntry = isReadOnly && detail
    ? detail.history.find(h => h.version === viewingVersion) ?? null
    : null;

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: A.bg }}>
      <AdminSidebar />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <header style={{ height: 56, background: "#fff", borderBottom: `1px solid ${A.line}`, display: "flex", alignItems: "center", padding: "0 32px", gap: 8, position: "sticky", top: 0, zIndex: 10 }}>
          <span style={{ fontSize: 12, color: A.muted2 }}>Admin /</span>
          <span style={{ fontSize: 12, fontWeight: 500, color: A.body }}>Brand Identity</span>
        </header>

        <main style={{ flex: 1, minWidth: 0, display: "flex", gap: 0, minHeight: 0 }}>
          {/* Left panel — brand list */}
          <div style={{ width: 260, borderRight: `1px solid ${A.line}`, background: "#fff", display: "flex", flexDirection: "column" }}>
            <div style={{ padding: "16px 16px 10px", borderBottom: `1px solid ${A.line}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>Brands</span>
              <div style={{ display: "flex", gap: 4 }}>
                <button onClick={loadBrands} style={{ background: "none", border: "none", cursor: "pointer", color: A.muted, padding: 4 }}>
                  <RefreshCw size={13} />
                </button>
                <button
                  onClick={() => { startNew(); docxRef.current?.click(); }}
                  disabled={parsing}
                  title="Upload DOCX brand brief"
                  style={{
                    background: "#EEF2FF", border: "none", borderRadius: 5, color: "#3730A3",
                    cursor: "pointer", padding: "4px 8px", fontSize: 12, fontWeight: 600,
                    display: "flex", alignItems: "center", gap: 3, opacity: parsing ? 0.6 : 1,
                  }}
                >
                  <Upload size={11} /> {parsing ? "…" : "DOCX"}
                </button>
                <input ref={docxRef} type="file" accept=".docx" style={{ display: "none" }} onChange={handleDocx} />
                <button onClick={startNew} style={{
                  background: A.gold, border: "none", borderRadius: 5, color: "#fff",
                  cursor: "pointer", padding: "4px 8px", fontSize: 12, fontWeight: 600,
                  display: "flex", alignItems: "center", gap: 3,
                }}>
                  <Plus size={11} /> New
                </button>
              </div>
            </div>

            <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
              {loading ? (
                <div style={{ padding: 20, fontSize: 12, color: A.muted }}>Loading…</div>
              ) : brands.length === 0 ? (
                <div style={{ padding: 20, fontSize: 12, color: A.muted }}>No brands yet. Create one →</div>
              ) : brands.map(b => (
                <div
                  key={b.brand_name}
                  onClick={() => selectBrand(b.brand_name)}
                  style={{
                    padding: "12px 16px", cursor: "pointer", borderBottom: `1px solid ${A.line}`,
                    background: selected === b.brand_name ? `${A.gold}18` : "transparent",
                    borderLeft: selected === b.brand_name ? `3px solid ${A.gold}` : "3px solid transparent",
                    transition: "background .1s",
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: 600, color: A.ink }}>{b.brand_name}</div>
                  <div style={{ fontSize: 11, color: A.muted, marginTop: 3, display: "flex", gap: 8 }}>
                    <span>v{b.version}</span>
                    {b.brand_type && <span>{b.brand_type}</span>}
                    {!b.is_active && <span style={{ color: A.red }}>inactive</span>}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Right panel — form */}
          <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
            {!isNew && !selected ? (
              <div style={{ paddingTop: 60, textAlign: "center", color: A.muted, fontSize: 14 }}>
                Select a brand from the left, or click <strong>New</strong> to create one.
              </div>
            ) : (
              <>
                {/* Read-only banner when viewing old version */}
                {isReadOnly && (
                  <div style={{
                    marginBottom: 16, padding: "10px 16px", borderRadius: 8,
                    background: "#FFF7ED", border: "1px solid #FED7AA",
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                  }}>
                    <span style={{ fontSize: 13, color: "#C2410C", fontWeight: 500 }}>
                      Read-only — version {viewingVersion}
                    </span>
                    <button onClick={exitVersionView} style={{
                      background: "none", border: "none", cursor: "pointer",
                      fontSize: 12, color: "#C2410C", fontWeight: 600, textDecoration: "underline",
                    }}>
                      Back to current
                    </button>
                  </div>
                )}

                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 24 }}>
                  <div>
                    <div style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: A.ink, letterSpacing: "-0.01em" }}>
                      {isNew ? "New Brand" : selected}
                    </div>
                    {!isNew && detail && (
                      <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>
                        v{viewedEntry ? viewedEntry.version : detail.version} · {(viewedEntry ? viewedEntry.updated_at : detail.updated_at)?.slice(0, 10) || "—"}
                        {(viewedEntry ? viewedEntry.is_active : detail.is_active)
                          ? <span style={{ color: A.green, marginLeft: 8 }}>● active</span>
                          : <span style={{ color: A.red, marginLeft: 8 }}>● inactive</span>}
                      </div>
                    )}
                  </div>
                  {!isNew && selected && !isReadOnly && (
                    <button onClick={deleteBrand} disabled={deleting} style={{
                      background: "none", border: `1px solid ${A.line}`, borderRadius: 6,
                      cursor: "pointer", color: A.red, padding: "6px 10px",
                      display: "flex", alignItems: "center", gap: 4, fontSize: 12,
                    }}>
                      <Trash2 size={12} /> {deleting ? "Deleting…" : "Delete"}
                    </button>
                  )}
                </div>

                <Card style={{ marginBottom: 20 }}>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
                    <Field label="Brand Name *">
                      <input
                        value={form.brand_name}
                        onChange={e => upd("brand_name", e.target.value)}
                        disabled={!isNew || isReadOnly}
                        placeholder="e.g. Adventure Asia"
                        style={{ ...inputStyle, opacity: (isNew && !isReadOnly) ? 1 : 0.6 }}
                      />
                    </Field>
                    <Field label="Brand Type">
                      <input
                        value={form.brand_type}
                        onChange={e => upd("brand_type", e.target.value)}
                        placeholder="e.g. luxury adventure, family, budget"
                        style={{ ...inputStyle, opacity: isReadOnly ? 0.7 : 1 }}
                        readOnly={isReadOnly}
                      />
                    </Field>
                  </div>

                  <Field label="Core Idea">
                    <textarea
                      value={form.core_idea}
                      onChange={e => upd("core_idea", e.target.value)}
                      rows={2}
                      placeholder="1-2 sentences describing the brand's core positioning"
                      style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <Field label="Target Markets">
                    <TagsInput
                      value={form.target_markets}
                      onChange={v => upd("target_markets", v)}
                      placeholder="Country or region, press Enter…"
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 24px" }}>
                    <Field label="Customer Segment">
                      <textarea
                        value={form.customer_segment}
                        onChange={e => upd("customer_segment", e.target.value)}
                        rows={3}
                        placeholder="Demographics: age, income, profession…"
                        style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                        readOnly={isReadOnly}
                      />
                    </Field>
                    <Field label="Customer Mindset">
                      <textarea
                        value={form.customer_mindset}
                        onChange={e => upd("customer_mindset", e.target.value)}
                        rows={3}
                        placeholder="Psychographics: values, motivations, fears…"
                        style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                        readOnly={isReadOnly}
                      />
                    </Field>
                  </div>

                  <Field label="Tone of Voice">
                    <TagsInput
                      value={form.tone_of_voice}
                      onChange={v => upd("tone_of_voice", v)}
                      placeholder="Trait, press Enter… (e.g. calm, refined)"
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <Field label="Writing Style">
                    <textarea
                      value={form.writing_style}
                      onChange={e => upd("writing_style", e.target.value)}
                      rows={3}
                      placeholder="e.g. Active verbs. No superlatives. Present tense…"
                      style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <Field label="Good Examples">
                    <textarea
                      value={form.good_examples}
                      onChange={e => upd("good_examples", e.target.value)}
                      rows={4}
                      placeholder="Paste example paragraphs that match the brand voice…"
                      style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <Field label="Should Write (System Prompt)">
                    <textarea
                      value={form.should_write}
                      onChange={e => upd("should_write", e.target.value)}
                      rows={5}
                      placeholder="Instructions for the AI: e.g. You are a travel editor for Adventure Asia…"
                      style={{ ...textareaStyle, opacity: isReadOnly ? 0.7 : 1 }}
                      readOnly={isReadOnly}
                    />
                  </Field>

                  <Field label="Forbidden Words">
                    <TagsInput
                      value={form.forbidden_words}
                      onChange={v => upd("forbidden_words", v)}
                      placeholder="Word, press Enter… (e.g. cheap, stunning, curated)"
                      readOnly={isReadOnly}
                    />
                  </Field>

                  {!isReadOnly && (
                    <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
                      <Btn
                        onClick={save}
                        disabled={saving || !canSave}
                        variant="primary"
                        style={{ background: A.gold, border: `1px solid ${A.gold}` }}
                      >
                        {saving ? "Saving…" : isNew ? "Create Brand" : "Save (new version)"}
                      </Btn>
                      {msg && (
                        <span style={{ fontSize: 12, color: msg.ok ? A.green : A.red }}>{msg.text}</span>
                      )}
                    </div>
                  )}

                  {isReadOnly && (
                    <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
                      <Btn
                        onClick={() => activateVersion(viewingVersion!)}
                        disabled={activating || detail?.history.find(h => h.version === viewingVersion)?.is_active}
                        variant="primary"
                        style={{ background: A.gold, border: `1px solid ${A.gold}` }}
                      >
                        {activating ? "Activating…" : `Set v${viewingVersion} as Active`}
                      </Btn>
                      <Btn variant="secondary" onClick={exitVersionView}>Cancel</Btn>
                      {msg && (
                        <span style={{ fontSize: 12, color: msg.ok ? A.green : A.red }}>{msg.text}</span>
                      )}
                    </div>
                  )}
                </Card>

                {/* Version history */}
                {!isNew && detail && detail.history.length > 1 && (
                  <Card>
                    <button
                      onClick={() => setHistoryOpen(!historyOpen)}
                      style={{
                        width: "100%", background: "none", border: "none", cursor: "pointer",
                        display: "flex", justifyContent: "space-between", alignItems: "center",
                        padding: 0, fontFamily: sans,
                      }}
                    >
                      <span style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase" as const, letterSpacing: "0.08em" }}>Version History ({detail.history.length})</span>
                      {historyOpen ? <ChevronUp size={14} color={A.muted} /> : <ChevronDown size={14} color={A.muted} />}
                    </button>
                    {historyOpen && (
                      <div style={{ marginTop: 12 }}>
                        {detail.history.map(v => (
                          <div key={v.version} style={{
                            display: "flex", justifyContent: "space-between", alignItems: "center",
                            padding: "8px 0", borderBottom: `1px solid ${A.line}`,
                          }}>
                            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                              <span style={{ fontSize: 13, fontWeight: 500 }}>v{v.version}</span>
                              <span style={{ fontSize: 11, color: A.muted }}>{v.updated_at?.slice(0, 10) || "—"}</span>
                              {v.is_active && (
                                <span style={{ fontSize: 10, background: A.gold, color: "#fff", borderRadius: 4, padding: "1px 6px" }}>active</span>
                              )}
                            </div>
                            <div style={{ display: "flex", gap: 6 }}>
                              <button
                                onClick={() => viewVersion(v)}
                                style={{
                                  background: "#EEF2FF", border: "none", borderRadius: 5,
                                  color: "#3730A3", cursor: "pointer", padding: "3px 10px",
                                  fontSize: 12, fontWeight: 600,
                                }}
                              >
                                View
                              </button>
                              {!v.is_active && (
                                <button
                                  onClick={() => activateVersion(v.version)}
                                  disabled={activating}
                                  style={{
                                    background: A.goldTint, border: `1px solid ${A.gold}`,
                                    borderRadius: 5, color: A.gold, cursor: "pointer",
                                    padding: "3px 10px", fontSize: 12, fontWeight: 600,
                                    opacity: activating ? 0.6 : 1,
                                  }}
                                >
                                  Set Active
                                </button>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </Card>
                )}
              </>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

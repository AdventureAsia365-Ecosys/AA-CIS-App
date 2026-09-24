"use client";
// app/(internal)/upload/page.tsx
// All pipeline logic preserved — S3 presigned URL, step tracker, history
// Design: brand tokens (app/_brand/tokens.ts) via internalUi

import { useState, useCallback, useEffect } from "react";
import {
  Upload, FileSpreadsheet, CheckCircle, Loader2, X, History, SkipForward, Search,
} from "lucide-react";
import InternalSidebar from "../_components/InternalSidebar";
import { A, serif, mono, sans, Card, SLabel, Btn, LoadingScreen, TopBar, TH, TD } from "../_components/internalUi";

// AA-435: getToken()/API_URL removed — both were dead already (every fetch
// below goes through /api/tenant or /api/admin proxies, which authenticate
// server-side via the httpOnly cis_admin_token cookie, not a client-read
// token), and cis_api_token itself no longer gets set for admin sessions.

const SEO_MODES = [
  { value: "dataforseo",      label: "DataForSEO",      desc: "Live keyword fetch (default)" },
  { value: "custom_keywords", label: "Custom Keywords",  desc: "Tenant keywords, no API call" },
  { value: "disabled",        label: "Disabled",         desc: "Skip SEO step entirely" },
];

const MODEL_TIERS = [
  { value: "haiku",  label: "Haiku",  desc: "Fast · $0.002/tour",  badge: "Default" },
  { value: "sonnet", label: "Sonnet", desc: "Quality · $0.02/tour", badge: "Premium" },
];

const STEPS = [
  { key: "upload",     label: "Uploading to S3",    desc: "Secure upload to Bronze layer" },
  { key: "ingestion",  label: "Parsing Excel",       desc: "Extracting tour rows from sheets" },
  { key: "seo",        label: "SEO Intelligence",    desc: "DataForSEO keyword fetch + cache" },
  { key: "generation", label: "Content Generation",  desc: "LLM rewrite via LangGraph" },
  { key: "validation", label: "Brand Validation",    desc: "Rules + Quality score check" },
  { key: "export",     label: "Export to Catalog",   desc: "Promoting approved tours to gold layer" },
];

function estimateTime(kb: number) {
  const rows = Math.max(1, Math.round(kb / 8));
  const s = 30 + rows * 4.5;
  return s < 60 ? `~${Math.round(s)}s` : `~${Math.round(s / 60)} min`;
}

interface FileEntry {
  file: File; status: "ready" | "uploading" | "done" | "error" | "duplicate";
  activeStep: number; progress: number; estTime: string; errorMsg?: string;
}
interface SourceEntry {
  id: string; filename: string; file_hash: string | null;
  file_size_kb: number | null; row_count: number | null; parsed_at: string | null;
}

// ─── Step Tracker ─────────────────────────────────────────────────────────────
function StepRow({ step, index, activeStep, progress }: {
  step: typeof STEPS[0]; index: number; activeStep: number; progress: number;
}) {
  const done   = index < activeStep;
  const active = index === activeStep;
  return (
    <div style={{ display: "flex", gap: 14, alignItems: "flex-start", paddingBottom: 16 }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
        <div style={{
          width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          background: done ? A.green : active ? A.gold : A.line2,
          color: done || active ? "#fff" : A.muted2,
          fontWeight: 700, fontSize: 11,
          boxShadow: active ? `0 0 0 3px ${A.goldTint}` : "none",
          transition: "all 0.3s",
        }}>
          {done ? <CheckCircle size={13} /> : active ? <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> : index + 1}
        </div>
        {index < STEPS.length - 1 && (
          <div style={{ width: 2, height: 20, marginTop: 3, background: done ? A.green : A.line2, transition: "background 0.3s" }} />
        )}
      </div>
      <div style={{ paddingTop: 4, flex: 1 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, color: done ? A.green : active ? A.gold : A.muted }}>
          {step.label}
        </div>
        <div style={{ fontSize: 11, color: A.muted2 }}>{step.desc}</div>
        {active && (
          <div style={{ marginTop: 6 }}>
            <div style={{ height: 3, background: A.line2, borderRadius: 2, overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${progress}%`, background: `linear-gradient(90deg,${A.gold},#f59e0b)`, borderRadius: 2, transition: "width 0.4s ease" }} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

function BrandIdentityPreview() {
  const [rules, setRules] = useState<any>(null);
  useEffect(() => {
    fetch("/api/tenant/pipeline/brand-identity")
      .then(r => r.ok ? r.json() : null)
      .then(d => setRules(d))
      .catch(() => {});
  }, []);
  const hasPrompt   = rules?.system_prompt?.trim();
  const hasStyle    = rules?.style_guide?.trim();
  const forbidden   = Array.isArray(rules?.forbidden_words) ? rules.forbidden_words : [];
  return (
    <Card style={{ marginTop: 24 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <SLabel>Active Brand Identity</SLabel>
        <a href="/brand" style={{ fontSize: 11, color: A.gold, fontWeight: 600, textDecoration: "none" }}>
          Edit →
        </a>
      </div>
      {rules === null ? (
        <div style={{ fontSize: 12, color: A.muted }}>Loading…</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <span style={{ fontSize: 12, padding: "3px 10px", borderRadius: 999,
              background: hasPrompt ? "#DCFCE7" : A.bg,
              color: hasPrompt ? "#16A34A" : A.muted,
              border: `1px solid ${hasPrompt ? "#86EFAC" : A.line}` }}>
              {hasPrompt ? "✓ System Prompt" : "⚠ No System Prompt"}
            </span>
            <span style={{ fontSize: 12, padding: "3px 10px", borderRadius: 999,
              background: hasStyle ? "#DCFCE7" : A.bg,
              color: hasStyle ? "#16A34A" : A.muted,
              border: `1px solid ${hasStyle ? "#86EFAC" : A.line}` }}>
              {hasStyle ? "✓ Style Guide" : "⚠ No Style Guide"}
            </span>
            <span style={{ fontSize: 12, padding: "3px 10px", borderRadius: 999,
              background: forbidden.length > 0 ? "#FEF9C3" : A.bg,
              color: forbidden.length > 0 ? "#92400E" : A.muted,
              border: `1px solid ${forbidden.length > 0 ? "#FDE68A" : A.line}` }}>
              {forbidden.length} Forbidden Words
            </span>
          </div>
          {!hasPrompt && !hasStyle && (
            <p style={{ fontSize: 12, color: A.muted, margin: 0, lineHeight: 1.5 }}>
              No brand rules configured — pipeline will use AA default standards.{" "}
              <a href="/brand" style={{ color: A.gold }}>Set up Brand Identity</a> to customise output.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

export default function UploadPage() {
  const [dragOver, setDragOver]   = useState(false);
  const [files, setFiles]         = useState<FileEntry[]>([]);
  const [seoMode, setSeoMode]     = useState("dataforseo");
  const [modelTier, setModelTier] = useState("haiku");
  const [isRunning, setIsRunning] = useState(false);
  const [sources, setSources]     = useState<SourceEntry[]>([]);
  const [loadingSrc, setLoadingSrc] = useState(false);
  const [isAdmin, setIsAdmin]     = useState(false);
  const [userName, setUserName]   = useState("Content");

  useEffect(() => {
    const role = document.cookie.split(";").find(c => c.trim().startsWith("cis_role="))?.split("=")[1];
    const name = document.cookie.split(";").find(c => c.trim().startsWith("cis_user="))?.split("=")[1];
    setIsAdmin(role === "admin");
    if (name) setUserName(decodeURIComponent(name));
  }, []);

  const activeFile  = files.find(f => f.status === "uploading") ?? files[files.length - 1];
  const globalStep  = activeFile?.activeStep ?? -1;
  const globalProg  = activeFile?.progress ?? 0;
  const readyCount  = files.filter(f => f.status === "ready").length;
  const doneCount   = files.filter(f => f.status === "done").length;
  const allDone     = files.length > 0 && files.every(f => ["done","error","duplicate"].includes(f.status));
  const canRun      = readyCount > 0 && !isRunning;

  const fetchSources = useCallback(async () => {
    setLoadingSrc(true);
    try {
      const r = await fetch("/api/tenant/v1/pipeline/sources?limit=20");
      if (r.ok) { const d = await r.json(); setSources(d.sources || []); }
    } finally { setLoadingSrc(false); }
  }, []);

  useEffect(() => { fetchSources(); }, [fetchSources]);

  const addFiles = useCallback((newFiles: File[]) => {
    const xlsx = newFiles.filter(f => f.name.match(/\.xlsx?$/i));
    setFiles(prev => {
      const existing = new Set(prev.map(e => e.file.name));
      const entries: FileEntry[] = xlsx.filter(f => !existing.has(f.name)).map(f => ({
        file: f, status: "ready", activeStep: -1, progress: 0, estTime: estimateTime(f.size / 1024),
      }));
      return [...prev, ...entries];
    });
  }, []);

  const updateFile = (i: number, patch: Partial<FileEntry>) =>
    setFiles(prev => prev.map((f, j) => j === i ? { ...f, ...patch } : f));

  const uploadOne = async (entry: FileEntry, index: number) => {
    updateFile(index, { status: "uploading", activeStep: 0, progress: 10 });
    const urlRes = await fetch("/api/admin/upload-url", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: entry.file.name, content_type: entry.file.type || "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", seo_mode: seoMode, model_tier: modelTier }),
    });
    if (!urlRes.ok) throw new Error("Failed to get upload URL");

    const { upload_url, s3_key, bucket } = await urlRes.json();
    updateFile(index, { progress: 50 });
    const upRes = await fetch(upload_url, { method: "PUT", headers: { "Content-Type": entry.file.type }, body: entry.file });
    if (!upRes.ok) throw new Error("S3 upload failed");
    updateFile(index, { activeStep: 0, progress: 100 });

    // Trigger pipeline — parse Excel from S3 + run LLM per tour in background
    await fetch("/api/admin/ingest-s3", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ s3_key, bucket, seo_mode: seoMode, model_tier: modelTier }),
    });

    await new Promise(r => setTimeout(r, 4000));
    const stepDurations = [5000, 5000, 30000, 10000, 8000];
    for (let s = 1; s < STEPS.length; s++) {
      updateFile(index, { activeStep: s, progress: 0 });
      const duration = stepDurations[s - 1] || 10000;
      const tick = 400; const ticks = duration / tick;
      for (let p = 0; p <= ticks; p++) {
        await new Promise(r => setTimeout(r, tick));
        updateFile(index, { progress: Math.min(95, Math.round((p / ticks) * 100)) });
      }
      updateFile(index, { progress: 100 });
    }
    updateFile(index, { status: "done", activeStep: STEPS.length, progress: 100 });
  };

  const runPipeline = async () => {
    if (readyCount === 0 || isRunning) return;
    setIsRunning(true);
    try {
      await Promise.all(files.map((e, i) =>
        e.status === "ready" ? uploadOne(e, i).catch(err => updateFile(i, { status: "error", errorMsg: err.message })) : Promise.resolve()
      ));
      await fetchSources();
    } finally { setIsRunning(false); }
  };

  return (
    <div style={{ display: "flex", minHeight: "100vh", fontFamily: sans, background: A.bg }}>
      <InternalSidebar isAdmin={isAdmin} userName={userName} />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, height: "100vh" }}>
        <TopBar breadcrumb={["Content", "Upload"]} />
        <main style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
          <div style={{ marginBottom: 24 }}>
            <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: A.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
              Upload Tour Content
            </h1>
            <p style={{ fontSize: 13, color: A.muted, margin: 0 }}>
              Upload supplier Excel files → AI pipeline rewrites to Adventure Asia brand standards
            </p>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
            {/* LEFT */}
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {/* Model Tier */}
              <Card>
                <SLabel>Model Tier</SLabel>
                <div style={{ display: "flex", gap: 8 }}>
                  {MODEL_TIERS.map(t => {
                    const active = modelTier === t.value;
                    return (
                      <button key={t.value} onClick={() => setModelTier(t.value)} style={{
                        flex: 1, padding: "8px 10px", borderRadius: 8, cursor: "pointer", fontFamily: sans,
                        border: `1px solid ${active ? A.gold : A.line}`,
                        background: active ? A.goldTint : A.bg,
                        color: active ? A.gold : A.muted,
                        fontSize: 12, fontWeight: active ? 700 : 400, textAlign: "left",
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ fontWeight: 600 }}>{t.label}</span>
                          <span style={{
                            fontSize: 9, padding: "1px 5px", borderRadius: 4,
                            background: active ? A.gold : A.line2,
                            color: active ? "#fff" : A.muted2,
                            fontWeight: 700, letterSpacing: "0.04em",
                          }}>{t.badge}</span>
                        </div>
                        <div style={{ fontSize: 10, marginTop: 2, opacity: 0.8 }}>{t.desc}</div>
                      </button>
                    );
                  })}
                </div>
                {modelTier === "sonnet" && (
                  <div style={{ marginTop: 8, fontSize: 11, color: A.muted, lineHeight: 1.5 }}>
                    Auto-upgrade also active: Haiku runs scoring below 8.5 are retried with Sonnet.
                  </div>
                )}
              </Card>

              {/* SEO Mode */}
              <Card>
                <SLabel>SEO Mode</SLabel>
                <div style={{ display: "flex", gap: 8 }}>
                  {SEO_MODES.map(m => (
                    <button key={m.value} onClick={() => setSeoMode(m.value)} style={{
                      flex: 1, padding: "8px 10px", borderRadius: 8, cursor: "pointer", fontFamily: sans,
                      border: `1px solid ${seoMode === m.value ? A.gold : A.line}`,
                      background: seoMode === m.value ? A.goldTint : A.bg,
                      color: seoMode === m.value ? A.gold : A.muted,
                      fontSize: 12, fontWeight: seoMode === m.value ? 700 : 400, textAlign: "left",
                    }}>
                      <div style={{ fontWeight: 600 }}>{m.label}</div>
                      <div style={{ fontSize: 10, marginTop: 2, opacity: 0.8 }}>{m.desc}</div>
                    </button>
                  ))}
                </div>
              </Card>

              {/* Dropzone */}
              <div
                onDragOver={e => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={e => { e.preventDefault(); setDragOver(false); addFiles(Array.from(e.dataTransfer.files)); }}
                onClick={() => document.getElementById("file-input")?.click()}
                style={{
                  border: `2px dashed ${dragOver ? A.gold : A.line}`,
                  borderRadius: 12, padding: "28px 20px", textAlign: "center",
                  background: dragOver ? A.goldTint : A.card,
                  cursor: "pointer", transition: "all 0.2s",
                }}>
                <input id="file-input" type="file" style={{ display: "none" }} accept=".xlsx,.xls" multiple
                  onChange={e => e.target.files && addFiles(Array.from(e.target.files))} />
                <FileSpreadsheet size={32} style={{ color: A.muted, margin: "0 auto 10px", display: "block" }} />
                <div style={{ color: A.body, fontWeight: 600, fontSize: 14 }}>Drop Excel files here</div>
                <div style={{ color: A.muted, fontSize: 12, marginTop: 4 }}>.xlsx or .xls · Multiple files supported</div>
                <div style={{
                  display: "inline-flex", alignItems: "center", gap: 6, marginTop: 14,
                  padding: "7px 18px", background: A.gold, borderRadius: 8, color: "#fff", fontSize: 13, fontWeight: 600,
                }}>
                  <Upload size={13} /> Browse Files
                </div>
              </div>

              {/* File list */}
              {files.length > 0 && (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {files.map((f, i) => {
                    const kb = (f.file.size / 1024).toFixed(0);
                    const sc = f.status === "done" ? A.green : f.status === "error" ? A.red : f.status === "duplicate" ? A.amber : A.muted;
                    const label = f.status === "done" ? "Done" : f.status === "error" ? (f.errorMsg || "Error") : f.status === "duplicate" ? "Duplicate — skipped" : f.status === "uploading" ? "Processing…" : `Ready ${f.estTime}`;
                    return (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, padding: "9px 12px" }}>
                        <FileSpreadsheet size={15} style={{ color: A.green, flexShrink: 0 }} />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, color: A.ink, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.file.name}</div>
                          <div style={{ fontSize: 11, color: sc, marginTop: 1 }}>{kb} KB · {label}</div>
                        </div>
                        {f.status === "done"      && <CheckCircle size={14} style={{ color: A.green }} />}
                        {f.status === "duplicate" && <SkipForward size={14} style={{ color: A.amber }} />}
                        {f.status === "uploading" && <Loader2 size={14} style={{ color: A.gold, animation: "spin 1s linear infinite" }} />}
                        {f.status === "ready" && !isRunning && (
                          <button onClick={e => { e.stopPropagation(); setFiles(p => p.filter((_,j) => j !== i)); }}
                            style={{ background: "none", border: "none", cursor: "pointer", color: A.muted, padding: 2, display: "flex" }}>
                            <X size={13} />
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Run button */}
              <button onClick={runPipeline} disabled={!canRun} style={{
                padding: "11px 20px", borderRadius: 10, border: "none",
                fontWeight: 700, fontSize: 14, cursor: canRun ? "pointer" : "not-allowed",
                background: allDone ? A.green : canRun ? A.gold : A.line2,
                color: canRun || allDone ? "#fff" : A.muted,
                display: "flex", alignItems: "center", justifyContent: "center", gap: 8, transition: "all 0.2s",
                fontFamily: sans,
              }}>
                {isRunning
                  ? <><Loader2 size={15} style={{ animation: "spin 1s linear infinite" }} /> Processing {files.filter(f => f.status === "uploading").length} file(s)…</>
                  : allDone
                  ? <><CheckCircle size={15} /> Complete — {doneCount} file(s) processed</>
                  : <>Start Pipeline · {readyCount} file{readyCount !== 1 ? "s" : ""} · {modelTier === "sonnet" ? "Sonnet" : "Haiku"} · SEO: {seoMode}</>}
              </button>
            </div>

            {/* RIGHT: Pipeline tracker */}
            <Card>
              <SLabel>Pipeline Progress</SLabel>
              {STEPS.map((step, i) => (
                <StepRow key={step.key} step={step} index={i} activeStep={globalStep} progress={globalProg} />
              ))}
              {allDone && doneCount > 0 && (
                <div style={{ marginTop: 12, padding: 14, background: A.greenSoft, border: "1px solid #86EFAC", borderRadius: 10 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, color: A.green, fontWeight: 600, fontSize: 13 }}>
                    <CheckCircle size={15} /> Pipeline running in background
                  </div>
                  <div style={{ fontSize: 12, color: "#166534", marginTop: 4 }}>Tours appear in Catalog when processing completes (~2–3 min)</div>
                  <a href="/catalog" style={{ display: "inline-block", marginTop: 10, padding: "6px 14px", background: A.gold, borderRadius: 6, color: "#fff", fontSize: 12, fontWeight: 600, textDecoration: "none" }}>
                    Go to Catalog →
                  </a>
                </div>
              )}
              {globalStep === -1 && (
                <div style={{ marginTop: 12, padding: 14, background: A.bg, borderRadius: 10, border: `1px solid ${A.line}` }}>
                  <p style={{ fontSize: 12, color: A.muted, margin: 0, lineHeight: 1.6 }}>
                    Select SEO mode, upload Excel files, then click <strong style={{ color: A.gold }}>Start Pipeline</strong>. Multiple files are processed in parallel.
                  </p>
                </div>
              )}
            </Card>
          </div>

          {/* Brand Identity Preview */}
          <BrandIdentityPreview />

          {/* Upload History */}
          <div style={{ marginTop: 32 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <History size={15} style={{ color: A.muted }} />
                <span style={{ fontSize: 13, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.1em" }}>Upload History</span>
              </div>
              <button onClick={fetchSources} disabled={loadingSrc}
                style={{ fontSize: 12, color: A.gold, background: "none", border: "none", cursor: "pointer", opacity: loadingSrc ? 0.5 : 1 }}>
                {loadingSrc ? "Refreshing…" : "↻ Refresh"}
              </button>
            </div>
            {sources.length === 0 ? (
              <Card><div style={{ padding: 24, textAlign: "center", color: A.muted, fontSize: 13 }}>No uploads yet</div></Card>
            ) : (
              <Card style={{ padding: 0, overflow: "hidden" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead><tr>{["Filename","Rows","Size","Hash","Uploaded"].map(h => <th key={h} style={TH}>{h}</th>)}</tr></thead>
                  <tbody>
                    {sources.map((s, i) => (
                      <tr key={s.id}>
                        <td style={TD}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <FileSpreadsheet size={13} style={{ color: A.green, flexShrink: 0 }} />
                            <span style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.filename}</span>
                          </div>
                        </td>
                        <td style={TD}>{s.row_count ?? "—"}</td>
                        <td style={TD}>{s.file_size_kb ? `${s.file_size_kb} KB` : "—"}</td>
                        <td style={TD}>
                          {s.file_hash
                            ? <code style={{ fontFamily: mono, fontSize: 11, background: A.bg, padding: "2px 6px", borderRadius: 4, color: A.muted }}>{s.file_hash.slice(0,12)}…</code>
                            : "—"}
                        </td>
                        <td style={{ ...TD, color: A.muted, fontSize: 12 }}>
                          {s.parsed_at ? new Date(s.parsed_at).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}

"use client";
import { useState } from "react";
import { T, mono, sans, Btn } from "../ui";
import type { Endpoint } from "./endpoints-config";
import { buildCodeSample, type Language } from "./code-generators";
import { Copy, CheckCircle } from "lucide-react";

const LANGS: { id: Language; label: string }[] = [
  { id: "curl",   label: "cURL" },
  { id: "python", label: "Python" },
  { id: "nodejs", label: "Node.js" },
  { id: "php",    label: "PHP" },
];

interface EndpointPanelProps {
  endpoint: Endpoint;
  params: Record<string, string>;
  onParamChange: (key: string, value: string) => void;
  language: Language;
  onLanguageChange: (lang: Language) => void;
  onSend: () => void;
  loading: boolean;
}

export function EndpointPanel({
  endpoint, params, onParamChange,
  language, onLanguageChange,
  onSend, loading,
}: EndpointPanelProps) {
  const [copied, setCopied] = useState(false);
  const code = buildCodeSample(endpoint, params, language);

  function copyCode() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Endpoint + description */}
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{
            fontSize: 11, fontWeight: 700, fontFamily: mono, padding: "2px 8px", borderRadius: 4,
            background: endpoint.method === "GET" ? "rgba(34,197,94,0.1)" : "rgba(234,179,8,0.1)",
            color: endpoint.method === "GET" ? T.green : T.amber,
          }}>
            {endpoint.method}
          </span>
          <code style={{ fontSize: 13, fontFamily: mono, color: T.ink }}>
            {endpoint.path}
          </code>
        </div>
        <p style={{ fontSize: 12.5, color: T.muted, margin: 0, lineHeight: 1.55, fontFamily: sans }}>
          {endpoint.description}
        </p>
      </div>

      {/* Params */}
      {endpoint.params.length > 0 && (
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 8 }}>
            Parameters
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {endpoint.params.map(p => (
              <div key={p.name} style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: 8, alignItems: "start" }}>
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, fontFamily: mono, color: T.ink }}>
                    {p.name}
                    {p.required && <span style={{ color: T.red, marginLeft: 2 }}>*</span>}
                  </div>
                  <div style={{ fontSize: 10, color: T.muted2 }}>{p.type}</div>
                </div>
                <div>
                  <input
                    value={params[p.name] ?? ""}
                    onChange={e => onParamChange(p.name, e.target.value)}
                    placeholder={p.example}
                    style={{
                      width: "100%", padding: "7px 10px", fontSize: 12,
                      border: `1px solid ${T.line}`, borderRadius: 6,
                      fontFamily: mono, color: T.body, background: T.bg,
                      outline: "none", boxSizing: "border-box",
                    }}
                  />
                  <div style={{ fontSize: 10, color: T.muted2, marginTop: 2 }}>{p.description}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Code sample */}
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          {/* Language tabs */}
          <div style={{ display: "flex", gap: 4 }}>
            {LANGS.map(l => (
              <button key={l.id} onClick={() => onLanguageChange(l.id)} style={{
                padding: "4px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
                border: `1px solid ${language === l.id ? T.gold : T.line}`,
                background: language === l.id ? T.goldTint : "transparent",
                color: language === l.id ? T.amber : T.muted,
                fontFamily: sans, fontWeight: language === l.id ? 700 : 400,
              }}>
                {l.label}
              </button>
            ))}
          </div>
          <button onClick={copyCode} style={{
            display: "flex", alignItems: "center", gap: 4,
            padding: "4px 10px", fontSize: 11, borderRadius: 4,
            border: `1px solid ${T.line}`, background: "transparent",
            color: copied ? T.green : T.muted, cursor: "pointer", fontFamily: sans,
          }}>
            {copied ? <CheckCircle size={11} /> : <Copy size={11} />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <pre style={{
          margin: 0, padding: "12px 14px",
          background: T.ink, borderRadius: 8,
          fontFamily: mono, fontSize: 11.5, color: "#A5D6A7",
          lineHeight: 1.7, overflowX: "auto", whiteSpace: "pre-wrap",
        }}>
          {code}
        </pre>
      </div>

      {/* Send button */}
      <Btn variant="primary" disabled={loading} onClick={onSend} style={{ alignSelf: "flex-start" }}>
        {loading ? "Sending…" : `▶ Send ${endpoint.method}`}
      </Btn>
    </div>
  );
}

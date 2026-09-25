"use client";
// app/(tenant)/portal/_components/ApiTab.tsx

import { useState } from "react";
import { Eye, EyeOff, Copy, CheckCircle, Bell } from "lucide-react";
import { SUPPORT_EMAIL } from "../../../_brand/tokens";
import { T, serif, mono, sans, Card } from "./ui";
import { ApiPlayground } from "./ApiPlayground";

const ENDPOINTS = [
  { method: "GET",   path: "/v1/tours/pool",                color: T.green,  label: "Browse published tour pool (paginated)" },
  { method: "GET",   path: "/v1/tours/my-versions",         color: T.green,  label: "List your rewritten catalog" },
  { method: "GET",   path: "/v1/tours/versions/{id}",       color: T.green,  label: "Get version detail + history" },
  { method: "POST",  path: "/v1/tours/pool/{id}/rewrite",   color: T.gold,   label: "Trigger a tour rewrite" },
  { method: "PATCH", path: "/v1/tours/versions/{id}",       color: "#7C3AED",label: "Approve / reject / edit inline" },
  { method: "GET",   path: "/admin/billing",          color: T.green,  label: "Quota, spend, activity" },
  { method: "GET",   path: "/admin/brand-identity",   color: T.green,  label: "Get brand rules + version history" },
  { method: "POST",  path: "/admin/brand-identity",   color: T.gold,   label: "Save new brand rules version" },
];

type Section = "overview" | "endpoints" | "webhooks";

const TABS: { id: Section; label: string }[] = [
  { id: "overview",   label: "Overview" },
  { id: "endpoints",  label: "API Playground" },
  { id: "webhooks",   label: "Webhooks" },
];

export default function ApiTab() {
  const [activeSection, setActiveSection] = useState<Section>("overview");
  const [showKey, setShowKey] = useState(false);
  const [copied, setCopied]   = useState(false);

  // Rewrite quota UI removed (AA-428) — feature never had backend enforcement, see AA-489 if rebuilding

  // API key is one-time — show contact message
  const keyPlaceholder = "Contact your Adventure Asia account manager to retrieve your API key.";

  function copy(text: string) {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div style={{ fontFamily: sans }}>
      {/* Tab nav */}
      <div style={{ display: "flex", gap: 2, marginBottom: 24, borderBottom: `1px solid ${T.line}` }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setActiveSection(t.id)} style={{
            padding: "9px 18px", fontSize: 13, fontWeight: 600,
            border: "none", background: "none", cursor: "pointer", fontFamily: sans,
            color: activeSection === t.id ? T.gold : T.muted,
            borderBottom: `2px solid ${activeSection === t.id ? T.gold : "transparent"}`,
            marginBottom: -1, transition: "all .15s",
          }}>
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Overview ── */}
      {activeSection === "overview" && (
      <div style={{ maxWidth: 680 }}>
      <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>API Access</h1>
      <p style={{ fontSize: 13, color: T.muted, marginBottom: 28, lineHeight: 1.6 }}>
        Use your API key to access your rewritten tour catalog programmatically. All requests require <code style={{ fontFamily: mono, background: T.bg, padding: "1px 6px", borderRadius: 4, fontSize: 12 }}>Authorization: Bearer &lt;jwt&gt;</code> — obtain a JWT via <code style={{ fontFamily: mono, background: T.bg, padding: "1px 6px", borderRadius: 4, fontSize: 12 }}>POST /auth/tenant-login</code>.
      </p>

      {/* API Key card */}
      <Card style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 12 }}>API Key</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <code style={{
            flex: 1, padding: "10px 14px", background: T.bg, borderRadius: 8,
            fontSize: 13, color: T.body, letterSpacing: 1, fontFamily: mono,
            border: `1px solid ${T.line}`, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            {showKey ? keyPlaceholder : "•".repeat(40)}
          </code>
          <button onClick={() => setShowKey(s => !s)} title={showKey ? "Hide" : "Show"}
            style={{ width: 38, height: 38, background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, cursor: "pointer", display: "grid", placeItems: "center", color: T.muted }}>
            {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
          <button onClick={() => copy(keyPlaceholder)} title="Copy"
            style={{ width: 38, height: 38, background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, cursor: "pointer", display: "grid", placeItems: "center", color: copied ? T.green : T.muted }}>
            {copied ? <CheckCircle size={14} /> : <Copy size={14} />}
          </button>
        </div>
        <div style={{ marginTop: 12, fontSize: 12, color: T.muted2 }}>
          Your API key was shown once at creation. Contact <a href={`mailto:${SUPPORT_EMAIL}`} style={{ color: T.gold }}>{SUPPORT_EMAIL}</a> to rotate or retrieve it.
        </div>
      </Card>

      {/* cURL example */}
      <Card style={{ marginBottom: 20, background: T.ink, border: `1px solid ${T.ink2}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: "rgba(255,255,255,0.5)" }}>Quick Start</div>
          <button onClick={() => copy(`curl -X POST https://api-cis.lumiguides.it.com/auth/tenant-login \\\n  -H "Content-Type: application/json" \\\n  -d '{"api_key":"<YOUR_KEY"}' | jq .token`)}
            style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 6, padding: "4px 10px", cursor: "pointer", fontSize: 11, color: "rgba(255,255,255,0.7)", fontFamily: sans }}>
            Copy
          </button>
        </div>
        <pre style={{ margin: 0, fontFamily: mono, fontSize: 12, color: "#A5D6A7", lineHeight: 1.7, overflowX: "auto" }}>
{`# 1. Get a JWT
curl -X POST https://api-cis.lumiguides.it.com/auth/tenant-login \\
  -H "Content-Type: application/json" \\
  -d '{"api_key":"<YOUR_KEY>"}' | jq .token

# 2. Browse published tours
curl -H "Authorization: Bearer <JWT>" \\
  https://api-cis.lumiguides.it.com/v1/tours/pool

# 3. Rewrite a tour
curl -X POST -H "Authorization: Bearer <JWT>" \\
  -H "Content-Type: application/json" \\
  -d '{"rewrite_language":"en-US","seo_mode":"standard"}' \\
  https://api-cis.lumiguides.it.com/v1/tours/pool/<TOUR_ID>/rewrite`}
        </pre>
      </Card>

      {/* Endpoint reference */}
      <Card>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>Endpoint Reference</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {ENDPOINTS.map(e => (
            <div key={e.path} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: `1px solid ${T.line2}` }}>
              <span style={{
                fontFamily: mono, fontSize: 10.5, fontWeight: 700,
                padding: "2px 8px", borderRadius: 4, flexShrink: 0,
                background: `${e.color}18`, color: e.color, minWidth: 48, textAlign: "center",
              }}>{e.method}</span>
              <code style={{ fontFamily: mono, fontSize: 12, color: "#1D4ED8", flex: 1 }}>{e.path}</code>
              <span style={{ fontSize: 12, color: T.muted }}>{e.label}</span>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 14, padding: "10px 14px", background: T.bg, borderRadius: 8, fontSize: 12, color: T.muted, fontFamily: mono }}>
          Base URL: <span style={{ color: T.ink }}>https://api-cis.lumiguides.it.com</span>
        </div>
      </Card>
      </div>
      )}

      {/* ── Endpoints / API Playground ── */}
      {activeSection === "endpoints" && (
        <ApiPlayground apiKey={null} />
      )}

      {/* ── Webhooks ── */}
      {activeSection === "webhooks" && (
        <div style={{ maxWidth: 560 }}>
          <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>Webhooks</h2>
          <p style={{ fontSize: 13, color: T.muted, marginBottom: 24, lineHeight: 1.6 }}>
            Receive real-time notifications when tour content is updated in your catalog.
          </p>
          <div style={{ padding: "20px 24px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 10 }}>
            <div style={{ fontSize: 12, color: T.muted, lineHeight: 1.6 }}>
              <Bell size={12} color={T.gold} style={{ verticalAlign: -1 }} /> <strong>Coming soon</strong> — webhook delivery is on the roadmap.<br />
              Contact your Adventure Asia account manager to be notified when webhooks are enabled.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

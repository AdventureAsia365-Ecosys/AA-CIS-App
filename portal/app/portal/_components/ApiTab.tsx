"use client";
// portal/app/portal/_components/ApiTab.tsx
// AA-760 (screenshot audit): the page h1 comes first, the section tabs under it (they rendered above
// the h1); no masked "key" field — the portal has no key to show (the eye revealed, and Copy copied,
// a sentence about contacting support), so the card says so and offers a request mail; the Quick
// Start Copy copies exactly the snippet shown (one const); long lines wrap on a phone.

import { useState } from "react";
import { Copy, CheckCircle, Bell, Mail } from "lucide-react";
import { alpha } from "@/app/_kit";
import { SUPPORT_EMAIL } from "@/app/_brand/tokens";
import { T, serif, mono, sans, Card, PageHeader } from "./ui";
import { ApiPlayground } from "./ApiPlayground";

const API_BASE = "https://api-cis.lumiguides.it.com";

const QUICKSTART = `# 1. Get a JWT
curl -X POST ${API_BASE}/auth/tenant-login \\
  -H "Content-Type: application/json" \\
  -d '{"api_key":"<YOUR_KEY>"}' | jq .token

# 2. Browse published tours
curl -H "Authorization: Bearer <JWT>" \\
  ${API_BASE}/v1/tours/pool

# 3. Rewrite a tour
curl -X POST -H "Authorization: Bearer <JWT>" \\
  -H "Content-Type: application/json" \\
  -d '{"rewrite_language":"en-US","seo_mode":"standard"}' \\
  ${API_BASE}/v1/tours/pool/<TOUR_ID>/rewrite`;

const codeChip: React.CSSProperties = {
  fontFamily: mono, background: T.bg, padding: "1px 6px", borderRadius: 4, fontSize: 12, whiteSpace: "nowrap",
};

const ENDPOINTS = [
  { method: "GET",   path: "/v1/tours/pool",                color: T.green,  label: "Browse published tour pool (paginated)" },
  { method: "GET",   path: "/v1/tours/my-versions",         color: T.green,  label: "List your rewritten catalog" },
  { method: "GET",   path: "/v1/tours/versions/{id}",       color: T.green,  label: "Get version detail + history" },
  { method: "POST",  path: "/v1/tours/pool/{id}/rewrite",   color: T.gold,   label: "Trigger a tour rewrite" },
  { method: "PATCH", path: "/v1/tours/versions/{id}",       color: "var(--aa-purple-strong)", label: "Approve / reject / edit inline" },
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
  const [copied, setCopied]   = useState(false);

  // Rewrite quota UI removed (AA-428) — feature never had backend enforcement, see AA-489 if rebuilding

  function copy(text: string) {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div style={{ fontFamily: sans }}>
      <PageHeader title="API Access" sub="Use your API key to work with your rewritten tours from your own systems." />

      {/* Tab nav */}
      <div role="tablist" aria-label="API sections" style={{ display: "flex", gap: 2, marginBottom: 24, borderBottom: `1px solid ${T.line}`, overflowX: "auto" }}>
        {TABS.map(t => (
          <button key={t.id} role="tab" aria-selected={activeSection === t.id} onClick={() => setActiveSection(t.id)} style={{
            whiteSpace: "nowrap", flexShrink: 0,
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
      <p style={{ fontSize: 13, color: T.muted, marginTop: 0, marginBottom: 28, lineHeight: 1.8 }}>
        All requests require <code style={codeChip}>Authorization: Bearer &lt;jwt&gt;</code> — obtain a JWT
        via <code style={codeChip}>POST /auth/tenant-login</code>.
      </p>

      {/* API Key card */}
      <Card style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 12 }}>API Key</div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <p style={{ flex: "1 1 260px", margin: 0, fontSize: 13, color: T.body, lineHeight: 1.6 }}>
            Your API key is not shown here: it was shown once, when it was issued. To get a new one,
            email <a href={`mailto:${SUPPORT_EMAIL}`} style={{ color: T.goldDeep, fontWeight: 600 }}>{SUPPORT_EMAIL}</a>.
          </p>
          <a href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent("New API key request")}`}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "7px 14px", borderRadius: 999, border: `1px solid ${T.line}`, background: T.card, color: T.ink3, fontSize: 12.5, fontWeight: 600, textDecoration: "none", whiteSpace: "nowrap" }}>
            <Mail size={13} aria-hidden /> Request a new key
          </a>
        </div>
      </Card>

      {/* cURL example */}
      <Card style={{ marginBottom: 20, background: T.darkPanel, border: `1px solid ${T.darkPanel2}` }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: "rgba(255,255,255,0.5)" }}>Quick Start</div>
          <button onClick={() => copy(QUICKSTART)} aria-label="Copy the Quick Start commands"
            style={{ display: "inline-flex", alignItems: "center", gap: 5, background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 6, padding: "4px 10px", cursor: "pointer", fontSize: 11, color: "rgba(255,255,255,0.7)", fontFamily: sans }}>
            {copied ? <CheckCircle size={12} aria-hidden /> : <Copy size={12} aria-hidden />} {copied ? "Copied" : "Copy"}
          </button>
        </div>
        {/* Wraps instead of scrolling sideways (a phone cut the URLs at the card edge, no cue). */}
        <pre style={{ margin: 0, fontFamily: mono, fontSize: 12, color: "#A5D6A7", lineHeight: 1.7, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {QUICKSTART}
        </pre>
      </Card>

      {/* Endpoint reference */}
      <Card>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: T.muted, marginBottom: 16 }}>Endpoint Reference</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {ENDPOINTS.map(e => (
            <div key={e.method + e.path} style={{ display: "flex", alignItems: "center", gap: "6px 12px", flexWrap: "wrap", padding: "10px 0", borderBottom: `1px solid ${T.line2}` }}>
              <span style={{
                fontFamily: mono, fontSize: 10.5, fontWeight: 700,
                padding: "2px 8px", borderRadius: 4, flexShrink: 0,
                // AA-760 — alpha() (a var() cannot take a hex alpha suffix: `${var}18` was an invalid colour).
                background: alpha(e.color, 10), color: e.color, minWidth: 48, textAlign: "center",
              }}>{e.method}</span>
              <code style={{ fontFamily: mono, fontSize: 12, color: "var(--aa-blue-link)", flex: "1 1 200px", overflowWrap: "anywhere" }}>{e.path}</code>
              <span style={{ fontSize: 12, color: T.muted }}>{e.label}</span>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 14, padding: "10px 14px", background: T.bg, borderRadius: 8, fontSize: 12, color: T.muted, fontFamily: mono }}>
          Base URL: <span style={{ color: T.ink, overflowWrap: "anywhere" }}>{API_BASE}</span>
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

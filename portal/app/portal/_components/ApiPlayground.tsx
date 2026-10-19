"use client";
import { useState } from "react";
import { T, sans } from "./ui";
import { ENDPOINT_GROUPS } from "./api-playground/endpoints-config";
import { EndpointPanel } from "./api-playground/EndpointPanel";
import { ResponseViewer } from "./api-playground/ResponseViewer";
import { ApiKeyDisplay } from "./api-playground/ApiKeyDisplay";
import type { Language } from "./api-playground/code-generators";

interface PlaygroundResponse {
  status: number;
  statusText: string;
  responseTime: number;
  data: unknown;
}

interface ApiPlaygroundProps {
  apiKey?: string | null;
}

export function ApiPlayground({ apiKey }: ApiPlaygroundProps) {
  const [selectedId, setSelectedId]   = useState("list-tours");
  const [params, setParams]           = useState<Record<string, string>>({});
  const [response, setResponse]       = useState<PlaygroundResponse | null>(null);
  const [loading, setLoading]         = useState(false);
  const [language, setLanguage]       = useState<Language>("curl");

  const allEndpoints = ENDPOINT_GROUPS.flatMap(g => g.endpoints);
  const endpoint     = allEndpoints.find(e => e.id === selectedId) ?? allEndpoints[0];

  async function handleSend() {
    setLoading(true);
    setResponse(null);
    try {
      const res = await fetch("/api/playground/proxy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          endpoint: endpoint.path.replace(":id", params.id ?? ""),
          params,
          method: endpoint.method,
        }),
      });
      setResponse(await res.json());
    } catch {
      setResponse({ status: 0, statusText: "Network Error", responseTime: 0, data: { error: "Could not reach proxy" } });
    } finally {
      setLoading(false);
    }
  }

  function selectEndpoint(id: string) {
    setSelectedId(id);
    setParams({});
    setResponse(null);
  }

  return (
    <div style={{ display: "flex", gap: 20, minHeight: 560, fontFamily: sans }}>
      {/* ── Sidebar ── */}
      <div style={{ width: 200, flexShrink: 0, display: "flex", flexDirection: "column", gap: 16 }}>
        <ApiKeyDisplay apiKey={apiKey} />

        {ENDPOINT_GROUPS.map(group => (
          <div key={group.group}>
            <p style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, margin: "0 0 6px" }}>
              {group.icon} {group.group}
            </p>
            {group.endpoints.map(ep => (
              <button
                key={ep.id}
                onClick={() => selectEndpoint(ep.id)}
                style={{
                  display: "block", width: "100%", textAlign: "left",
                  padding: "7px 10px", borderRadius: 6, fontSize: 12,
                  cursor: "pointer", fontFamily: sans, marginBottom: 2,
                  background: ep.id === selectedId ? T.goldTint : "transparent",
                  border: `1px solid ${ep.id === selectedId ? T.goldSoft : "transparent"}`,
                  color: ep.id === selectedId ? T.amber : T.muted,
                  fontWeight: ep.id === selectedId ? 600 : 400,
                }}
              >
                <span style={{ fontSize: 10, fontFamily: "monospace", marginRight: 5,
                  color: ep.method === "GET" ? T.green : T.amber }}>
                  {ep.method}
                </span>
                {ep.title}
              </button>
            ))}
          </div>
        ))}
      </div>

      {/* ── Main ── */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <EndpointPanel
          endpoint={endpoint}
          params={params}
          onParamChange={(k, v) => setParams(p => ({ ...p, [k]: v }))}
          language={language}
          onLanguageChange={setLanguage}
          onSend={handleSend}
          loading={loading}
        />
        <ResponseViewer
          response={response}
          example={endpoint.exampleResponse}
          loading={loading}
        />
      </div>
    </div>
  );
}

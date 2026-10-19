"use client";
import { T, mono, sans } from "../ui";

interface PlaygroundResponse {
  status: number;
  statusText: string;
  responseTime: number;
  data: unknown;
}

interface ResponseViewerProps {
  response: PlaygroundResponse | null;
  example: unknown;
  loading: boolean;
}

export function ResponseViewer({ response, example, loading }: ResponseViewerProps) {
  if (loading) {
    return (
      <div style={container}>
        <div style={{ fontSize: 12, color: T.muted, display: "flex", alignItems: "center", gap: 8, fontFamily: sans }}>
          <span style={{ display: "inline-block", width: 10, height: 10, border: `2px solid ${T.gold}`, borderTopColor: "transparent", borderRadius: "50%", animation: "cis-spin 0.8s linear infinite" }} />
          Sending request…
        </div>
      </div>
    );
  }

  if (!response) {
    return (
      <div style={container}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 10 }}>
          Example Response
        </div>
        <pre style={pre}>{JSON.stringify(example, null, 2)}</pre>
      </div>
    );
  }

  const isOk  = response.status >= 200 && response.status < 300;
  const color = isOk ? T.green : T.red;

  return (
    <div style={container}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color, fontFamily: mono }}>
          {response.status} {response.statusText}
        </span>
        <span style={{ fontSize: 11, color: T.muted2, fontFamily: mono }}>
          {response.responseTime}ms
        </span>
      </div>
      <pre style={pre}>{JSON.stringify(response.data, null, 2)}</pre>
    </div>
  );
}

const container: React.CSSProperties = {
  background: T.ink,
  borderRadius: 8,
  padding: "14px 16px",
  minHeight: 120,
};

const pre: React.CSSProperties = {
  margin: 0,
  fontFamily: mono,
  fontSize: 11.5,
  color: "#A5D6A7",
  lineHeight: 1.7,
  overflowX: "auto",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

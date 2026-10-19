"use client";
import { useState } from "react";
import { Eye, EyeOff, Copy, CheckCircle } from "lucide-react";
import { T, mono, sans } from "../ui";

interface ApiKeyDisplayProps {
  apiKey?: string | null;
}

export function ApiKeyDisplay({ apiKey }: ApiKeyDisplayProps) {
  const [show, setShow]     = useState(false);
  const [copied, setCopied] = useState(false);

  const displayKey = apiKey ?? null;
  const masked     = "•".repeat(28);

  function copy() {
    if (!displayKey) return;
    navigator.clipboard.writeText(displayKey).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div style={{ padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8 }}>
      <div style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>
        Your API Key
      </div>
      {displayKey ? (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <code style={{ flex: 1, fontSize: 11, fontFamily: mono, color: T.body, wordBreak: "break-all" }}>
            {show ? displayKey : masked}
          </code>
          <button onClick={() => setShow(s => !s)} style={iconBtn}>
            {show ? <EyeOff size={12} /> : <Eye size={12} />}
          </button>
          <button onClick={copy} style={{ ...iconBtn, color: copied ? T.green : T.muted2 }}>
            {copied ? <CheckCircle size={12} /> : <Copy size={12} />}
          </button>
        </div>
      ) : (
        <p style={{ fontSize: 11, color: T.muted2, margin: 0, lineHeight: 1.5 }}>
          Contact your Adventure Asia account manager to retrieve your API key.
        </p>
      )}
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  width: 26, height: 26, background: "none",
  border: `1px solid ${T.line}`, borderRadius: 6,
  cursor: "pointer", display: "grid", placeItems: "center",
  color: T.muted2, flexShrink: 0,
};

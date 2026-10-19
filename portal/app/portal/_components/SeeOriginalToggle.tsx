"use client";
import { useState } from "react";
import { T, sans, parseHighlights } from "./ui";

interface SeeOriginalProps {
  summary: string | null;
  seoTitle: string | null;
  seoMeta: string | null;
  highlightsRaw: string | null;
  itineraries: string | null;
}

export function SeeOriginalToggle({ summary, seoTitle, seoMeta, highlightsRaw, itineraries }: SeeOriginalProps) {
  const [open, setOpen] = useState(false);
  const highlights = parseHighlights(highlightsRaw ?? "");
  const hasContent = summary || seoTitle || seoMeta || highlights.length > 0 || itineraries;
  if (!hasContent) return null;

  return (
    <div style={{ borderTop: `1px dashed ${T.line}`, paddingTop: 14, marginTop: 6 }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          fontSize: 12, color: T.muted2,
          background: "none", border: "none", cursor: "pointer",
          fontFamily: sans, padding: 0,
        }}
      >
        <span style={{ fontSize: 10 }}>{open ? "▲" : "▼"}</span>
        <span>{open ? "Hide" : "See"} original content from Adventure Asia</span>
      </button>

      {open && (
        <div style={{
          marginTop: 12, borderRadius: 8,
          background: T.bg, border: `1px solid ${T.line}`,
          padding: "14px 16px",
        }}>
          <p style={{ fontSize: 11, color: T.muted2, fontStyle: "italic", marginBottom: 14, lineHeight: 1.5 }}>
            Original AA content — your version above has been rewritten to match your brand.
          </p>
          {summary  && <OrigField label="Summary">{summary}</OrigField>}
          {seoTitle && <OrigField label="SEO Title">{seoTitle}</OrigField>}
          {seoMeta  && <OrigField label="SEO Meta">{seoMeta}</OrigField>}
          {highlights.length > 0 && (
            <OrigField label="Highlights">
              {highlights.map((h, i) => (
                <div key={i} style={{ marginBottom: 3 }}>• {h}</div>
              ))}
            </OrigField>
          )}
          {itineraries && (
            <OrigField label="Itinerary">
              <div style={{ whiteSpace: "pre-wrap", maxHeight: 220, overflowY: "auto" }}>
                {itineraries}
              </div>
            </OrigField>
          )}
        </div>
      )}
    </div>
  );
}

function OrigField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 5 }}>
        {label}
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6 }}>{children}</div>
    </div>
  );
}

"use client";
// portal/app/portal/_components/CatalogDrawer.tsx
// AA-662 PR 3 — the My Content detail/edit drawer, split out of CatalogTab. Uses the kit Drawer +
// react-query (version detail, which also carries the Master text) + mutations (save edit, request rewrite). The
// compare editors (summary / highlights / itinerary) and SeeOriginalToggle are unchanged.
// AA-668 (S239): the drawer shows a finished version only. "Request Rewrite" and a version that is
// still being written or failed open the Writer panel instead (TourRewritePanel.tsx): live
// progress, Retry, and the result — the inline LiveWriter / failed states that lived here moved.

import { useQuery } from "@tanstack/react-query";
import { Download, Save, X } from "lucide-react";
import { useMemo, useState } from "react";
import { T, mono, sans, Btn, LoadingScreen, parseContent, parseHighlights, fmtDateTime } from "./ui";
import { AiContentNotice } from "./AiContentNotice";
import { SeeOriginalToggle } from "./SeeOriginalToggle";
import { fetchVersionDetail, type Version } from "./catalogApi";

function TripFact({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{value}</div>
    </div>
  );
}

function CompareRow({ label, yours, onEdit }: { label: string; yours: string; onEdit: (v: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(yours);
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{label}</span>
        {!editing && (
          <button
            onClick={() => {
              setEditing(true);
              setVal(yours);
            }}
            style={{ background: "none", border: "none", cursor: "pointer", color: T.gold, fontSize: 10, fontFamily: sans }}
          >
            <span aria-hidden>✏</span> Edit
          </button>
        )}
      </div>
      {editing ? (
        <div>
          <textarea
            value={val}
            onChange={(e) => setVal(e.target.value)}
            rows={3}
            style={{ width: "100%", fontSize: 11.5, border: `1px solid ${T.gold}`, borderRadius: 6, padding: "8px 10px", resize: "vertical", fontFamily: sans, outline: "none", boxSizing: "border-box", color: T.body }}
          />
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <Btn size="sm" variant="primary" onClick={() => { onEdit(val); setEditing(false); }}>
              Save
            </Btn>
            <Btn size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Btn>
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
          {yours || "—"}
        </div>
      )}
    </div>
  );
}

function HighlightsCompare({ yours, onChange }: { yours: string[]; onChange: (v: string[]) => void }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted, marginBottom: 6 }}>
        Highlights
      </div>
      <div style={{ padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours.map((h, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "flex-start" }}>
            <span style={{ color: T.gold, fontWeight: 700, flexShrink: 0 }}>•</span>
            <input
              value={h}
              onChange={(e) => {
                const n = [...yours];
                n[i] = e.target.value;
                onChange(n);
              }}
              aria-label={`Highlight ${i + 1}`}
              style={{ flex: 1, fontSize: 11, border: `1px solid ${T.line}`, borderRadius: 4, padding: "2px 6px", fontFamily: sans, outline: "none", background: "transparent" }}
            />
            <button
              onClick={() => onChange(yours.filter((_, j) => j !== i))}
              aria-label={`Remove highlight ${i + 1}`}
              title="Remove"
              style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2, padding: 0, flexShrink: 0 }}
            >
              <span aria-hidden>×</span>
            </button>
          </div>
        ))}
        <button
          onClick={() => onChange([...yours, ""])}
          style={{ fontSize: 11, color: T.gold, background: "none", border: "none", cursor: "pointer", fontFamily: sans }}
        >
          + Add
        </button>
      </div>
    </div>
  );
}

function ItineraryCompare({ yours, expand, setExpand }: { yours: string; expand: boolean; setExpand: (v: boolean) => void }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>Itinerary</div>
        <button
          onClick={() => setExpand(!expand)}
          style={{ fontSize: 11, color: T.gold, background: T.goldTint, border: `1px solid ${T.goldSoft}`, borderRadius: 4, padding: "2px 10px", cursor: "pointer", fontFamily: sans, fontWeight: 600 }}
        >
          {expand ? "▲ Collapse" : "▼ Expand"}
        </button>
      </div>
      <div style={{ fontSize: 11.5, color: T.body, lineHeight: 1.6, maxHeight: expand ? "none" : 100, overflow: expand ? "visible" : "hidden", whiteSpace: "pre-wrap", padding: "8px 10px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 6 }}>
        {yours || "—"}
      </div>
    </div>
  );
}

export type CatalogDrawerBody = {
  group: Version;
  // callbacks owned by the parent (which runs the mutations against react-query)
  saving: boolean;
  saveOk: boolean;
  dirty: boolean;
  exporting: boolean;
  rewriting: boolean;
  onSave: (edited: Record<string, unknown>) => void;
  onRequestRewrite: () => void;
  onExportDocx: (versionId: string) => void;
  onDirty: () => void;
  onClose: () => void;
};

/**
 * The drawer body. Keyed by group.id in the parent so switching tours / picking up a new version
 * remounts it with a fresh editor (no reset-in-effect; React Compiler rule, AGENTS.md).
 */
export function CatalogDrawer(props: CatalogDrawerBody) {
  const { group, saving, saveOk, dirty, exporting, rewriting } = props;

  // Version detail (the parent only opens this drawer on a finished version).
  // AA-836 (D4) — "See original content" reads the Master text from this same response: GET
  // /v1/tours/versions/{id} joins published_tours and returns aa_summary / aa_highlights /
  // aa_itineraries (tenant pool, RLS-scoped). The old extra call to GET /v1/tours/pool/{id} hit a
  // route that never existed (404 + console error on every open) and then fell back to these
  // same fields anyway.
  const { data: detail, isLoading } = useQuery({
    queryKey: ["my-version-detail", group.id],
    queryFn: () => fetchVersionDetail(group.id),
  });

  // Editor state, initialised from detail once it loads (lazy-ish: derived initial via useMemo of
  // the parsed content, then held in local state so edits stick). We key the whole drawer by
  // group.id in the parent, so this component is fresh per version.
  const parsed = useMemo(
    () => (detail ? (parseContent(detail.rewritten_content) as Record<string, unknown> | null) : null),
    [detail],
  );
  const [editSummary, setEditSummary] = useState<string | null>(null);
  const [editHighlights, setEditHighlights] = useState<string[] | null>(null);
  const [expandItin, setExpandItin] = useState(true);

  // Resolve the current editor values: local state if the user has touched it, else from data.
  const summaryVal = editSummary ?? (parsed?.summary as string) ?? detail?.aa_summary ?? "";
  const highlightsVal =
    editHighlights ??
    (Array.isArray(parsed?.highlights) ? (parsed!.highlights as string[]) : parseHighlights(detail?.aa_highlights ?? ""));

  function markSummary(v: string) {
    setEditSummary(v);
    props.onDirty();
  }
  function markHighlights(v: string[]) {
    setEditHighlights(v);
    props.onDirty();
  }

  function doSave() {
    props.onSave({
      name: detail?.aa_name ?? group.aa_name,
      subtitle: detail?.aa_subtitle ?? group.aa_subtitle,
      summary: summaryVal,
      highlights: highlightsVal,
      seo_title: detail?.aa_seo_title ?? "",
      seo_meta: detail?.aa_seo_meta ?? "",
    });
  }

  // ── Header ──
  const header = (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%" }}>
      {/* The tour name is the kit Drawer title (CatalogTab.tsx), so only the language stays here. */}
      <div style={{ fontSize: 11.5, color: T.muted, fontFamily: mono }}>{group.rewrite_language}</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {dirty && (
          <Btn variant="primary" size="sm" disabled={saving} onClick={doSave}>
            <Save size={12} /> {saving ? "Saving…" : "Save"}
          </Btn>
        )}
        {saveOk && !dirty && <span style={{ fontSize: 12, color: T.green, fontWeight: 600 }}>✓ Saved</span>}
        <button onClick={props.onClose} aria-label="Close" title="Close" style={{ background: "none", border: "none", cursor: "pointer", color: T.muted2 }}>
          <X size={16} aria-hidden />
        </button>
      </div>
    </div>
  );

  if (isLoading || !detail) {
    return (
      <>
        {header}
        <LoadingScreen message="Loading your content…" />
      </>
    );
  }

  const itinYours = String(parsed?.itineraries ?? "");
  const showItin = Boolean(detail.aa_itineraries || parsed?.itineraries);

  return (
    <>
      {header}
      <div style={{ marginTop: 16 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 12, fontSize: 11, color: T.muted2, fontFamily: mono }}>
          <span>Created: {fmtDateTime(detail.created_at)}</span>
          {detail.edited_at && <span>· Edited: {fmtDateTime(detail.edited_at)}</span>}
        </div>

        <CompareRow label="Summary" yours={summaryVal} onEdit={markSummary} />
        <HighlightsCompare yours={highlightsVal} onChange={markHighlights} />
        {showItin && <ItineraryCompare yours={itinYours} expand={expandItin} setExpand={setExpandItin} />}

        <SeeOriginalToggle
          summary={String(detail.aa_summary ?? "")}
          seoTitle={null}
          seoMeta={null}
          highlightsRaw={String(detail.aa_highlights ?? "")}
          itineraries={String(detail.aa_itineraries ?? "") || null}
        />

        {(detail.duration || detail.inclusions || detail.exclusions) && (
          <div style={{ paddingTop: 14, marginTop: 14, borderTop: `1px solid ${T.line}` }}>
            <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted, marginBottom: 10 }}>
              Trip Details
            </div>
            {detail.duration && <TripFact label="Duration" value={detail.duration} />}
            {detail.inclusions && <TripFact label="Inclusions" value={detail.inclusions} />}
            {detail.exclusions && <TripFact label="Exclusions" value={detail.exclusions} />}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", flexWrap: "wrap", gap: 8, paddingTop: 14, marginTop: 14, borderTop: `1px solid ${T.line}` }}>
          {/* AA-819 — AUP notice beside Export DOCX; takes the free space, wraps above the buttons on phones. */}
          <AiContentNotice style={{ flex: "1 1 260px", minWidth: 0 }} />
          <Btn variant="ghost" disabled={exporting} onClick={() => props.onExportDocx(detail.id)}>
            <Download size={12} /> {exporting ? "Exporting…" : "Export DOCX"}
          </Btn>
          <Btn variant="secondary" disabled={rewriting} onClick={props.onRequestRewrite}>
            {rewriting ? "Starting…" : "Request Rewrite"}
          </Btn>
        </div>
      </div>
    </>
  );
}

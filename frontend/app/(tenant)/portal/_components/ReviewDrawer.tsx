"use client";
// app/(tenant)/portal/_components/ReviewDrawer.tsx
// AA-669 — the My Content detail view, shown in the kit Drawer when a row is opened. Carries over
// the copy-lock, tenant-safe context, flag banner, inline edit and export actions from the old
// ReviewList card (AA-501/AA-519/AA-614) unchanged in behaviour.

import { Download, Flag, HelpCircle, Loader2, Pencil, Save, Search, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { T, sans, mono, Btn } from "./ui";
import { type ReviewFlag, type ReviewItem } from "./reviewListApi";

export function ReviewDrawer({
  item,
  onSave,
  onExport,
  saving,
  onCopyAttempt,
}: {
  item: ReviewItem;
  onSave: (pieceId: string, text: string) => Promise<void>;
  onExport: (item: ReviewItem, format: "text" | "html", mode?: "document" | "fragment") => void;
  saving: boolean;
  onCopyAttempt: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.content_text ?? "");
  const editable = item.content_text !== null;

  async function doSave() {
    await onSave(item.piece_id, draft);
    setEditing(false);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, fontFamily: sans }}>
      {/* content */}
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={14}
          style={{
            width: "100%",
            padding: "14px 16px",
            borderRadius: 10,
            border: `1px solid ${T.gold}`,
            background: "#fff",
            fontSize: 13.5,
            lineHeight: 1.6,
            color: T.body,
            fontFamily: sans,
            resize: "vertical",
            outline: "none",
            boxSizing: "border-box",
          }}
        />
      ) : item.ready_state === "in_progress" ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.bg, fontSize: 13, color: T.muted }}>
          <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> Still writing — check back in a moment.
        </div>
      ) : item.ready_state === "not_ready" && !item.content_text ? (
        <div style={{ padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.bg, fontSize: 13, color: T.muted, lineHeight: 1.5 }}>
          This piece isn&rsquo;t ready to publish yet. Our team is on it — check back soon.
        </div>
      ) : (
        <div
          onCopy={(e) => { e.preventDefault(); onCopyAttempt(); }}
          onCut={(e) => { e.preventDefault(); onCopyAttempt(); }}
          onContextMenu={(e) => { e.preventDefault(); onCopyAttempt(); }}
          style={{
            padding: "14px 16px",
            borderRadius: 10,
            border: `1px solid ${T.line}`,
            background: T.card,
            whiteSpace: "pre-wrap",
            fontSize: 13.5,
            lineHeight: 1.6,
            color: T.body,
            userSelect: "none",
            WebkitUserSelect: "none",
            MozUserSelect: "none",
          }}
        >
          {item.content_text}
          {item.ready_state === "not_ready" && (
            <div style={{ marginTop: 10, fontSize: 11.5, color: T.muted, fontStyle: "italic" }}>
              Draft above isn&rsquo;t ready to publish yet — our team is reviewing it.
            </div>
          )}
        </div>
      )}

      {/* actions */}
      {editable &&
        (editing ? (
          <div style={{ display: "flex", gap: 8 }}>
            <Btn variant="primary" size="sm" disabled={saving} onClick={doSave}>
              <Save size={12} /> {saving ? "Saving…" : "Save"}
            </Btn>
            <Btn variant="ghost" size="sm" disabled={saving} onClick={() => { setEditing(false); setDraft(item.content_text ?? ""); }}>
              <X size={12} /> Cancel
            </Btn>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Btn variant="secondary" size="sm" onClick={() => setEditing(true)}>
              <Pencil size={12} /> Edit
            </Btn>
            {item.channel === "blog" ? (
              <>
                <Btn variant="secondary" size="sm" onClick={() => onExport(item, "text")}>
                  <Download size={12} /> Export text
                </Btn>
                <Btn variant="secondary" size="sm" onClick={() => onExport(item, "html", "document")}>
                  <Download size={12} /> Export HTML file
                </Btn>
                <Btn variant="secondary" size="sm" onClick={() => onExport(item, "html", "fragment")}>
                  <Download size={12} /> Export HTML for CMS
                </Btn>
              </>
            ) : (
              <Btn variant="secondary" size="sm" onClick={() => onExport(item, "text")}>
                <Download size={12} /> Export
              </Btn>
            )}
          </div>
        ))}

      <FlagBanner flags={item.flags} />
      <ContextSection item={item} />
    </div>
  );
}

function FlagBanner({ flags }: { flags: ReviewFlag[] }) {
  if (flags.length === 0) return null;
  return (
    <div style={{ padding: "10px 12px", borderRadius: 8, border: `1px solid ${T.amber}`, background: T.amberSoft, fontSize: 12, lineHeight: 1.6, color: T.body }}>
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontWeight: 600, color: T.ink, marginBottom: 4 }}>
        <Flag size={12} color={T.amber} /> Flagged for your review — doesn&rsquo;t block publishing
      </div>
      {flags.flatMap((f) => f.violations).map((v, i) => (
        <div key={i} style={{ color: T.muted }}>{v}</div>
      ))}
    </div>
  );
}

function ContextRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 6, fontSize: 12, color: T.body }}>
      <span style={{ color: T.muted2, marginTop: 1 }}>{icon}</span>
      <span><span style={{ color: T.muted2 }}>{label}:</span> {value}</span>
    </div>
  );
}

function ContextSection({ item }: { item: ReviewItem }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 14, borderTop: `1px solid ${T.line}` }}>
      <div style={{ fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted2 }}>
        Where this came from
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {item.goal && <ContextRow icon={<Sparkles size={12} />} label="Goal" value={item.goal.label} />}
        {item.tour && <ContextRow icon={<Search size={12} />} label="Tour" value={`${item.tour.name} (${item.tour.destination})`} />}
      </div>

      {item.angle && (
        <div style={{ padding: "10px 12px", borderRadius: 8, background: T.bg, fontSize: 12, lineHeight: 1.6, color: T.body }}>
          <div style={{ fontWeight: 600, color: T.ink, marginBottom: 2 }}>Angle: {item.angle.name}</div>
          <div style={{ color: T.muted }}>{item.angle.why_it_works}</div>
          <div style={{ marginTop: 4, fontSize: 11, color: T.muted2 }}>
            {item.angle.formula_fit} · {item.angle.best_final_style}
          </div>
        </div>
      )}

      {item.atom && (
        <div style={{ padding: "10px 12px", borderRadius: 8, background: T.bg, fontSize: 12, lineHeight: 1.6, color: T.body }}>
          <div style={{ fontWeight: 600, color: T.ink, marginBottom: 2 }}>Source atom</div>
          <div>{item.atom.text}</div>
          {(item.atom.activity_type || item.atom.emotional_hook || item.atom.season_note) && (
            <div style={{ marginTop: 4, fontSize: 11, color: T.muted2 }}>
              {[item.atom.activity_type, item.atom.emotional_hook, item.atom.season_note].filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
      )}

      {item.dfs_paa_snapshot &&
        (item.dfs_paa_snapshot.people_also_ask.length > 0 || item.dfs_paa_snapshot.related_keywords.length > 0) && (
          <div style={{ padding: "10px 12px", borderRadius: 8, background: T.bg, fontSize: 12, lineHeight: 1.6, color: T.body }}>
            <div style={{ display: "flex", alignItems: "center", gap: 5, fontWeight: 600, color: T.ink, marginBottom: 4 }}>
              <HelpCircle size={12} /> Search context used
            </div>
            {item.dfs_paa_snapshot.people_also_ask.length > 0 && (
              <div style={{ color: T.muted }}>Travelers also ask: {item.dfs_paa_snapshot.people_also_ask.join("; ")}</div>
            )}
            {item.dfs_paa_snapshot.related_keywords.length > 0 && (
              <div style={{ marginTop: 2, fontSize: 11, color: T.muted2 }}>
                Related terms: {item.dfs_paa_snapshot.related_keywords.join(", ")}
              </div>
            )}
          </div>
        )}

      {item.cta && (
        <div style={{ fontSize: 11.5, color: T.muted, fontFamily: mono }}>CTA: {item.cta}</div>
      )}
    </div>
  );
}

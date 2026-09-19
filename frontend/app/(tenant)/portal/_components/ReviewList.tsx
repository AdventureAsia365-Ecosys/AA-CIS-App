"use client";
// app/(tenant)/portal/_components/ReviewList.tsx — AA-501 [T10 review screen]
//
// The screen that sits between T10's automatic quality gate and T11 publish (there was
// previously NOTHING here — "Write Content" and "Publish" sat directly adjacent in the sidebar).
// Shows every piece the tenant has written (any channel, any status) with full write context
// (atom/tour/goal/angle/DFS-PAA) — but DELIBERATELY never gate_ledger/repair_log/held_reason.
// This is stricter than AngleGateTab.tsx's own end-of-flow card (which does show held_reason) —
// a deliberate divergence Nghiệp confirmed for this screen, not an inconsistency to fix.
//
// API: GET /api/tenant/v1/content-writing/reviews — same /api/tenant/[...path] proxy convention
// every other real tenant-portal fetch uses (see PublishPendingList.tsx).

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  ChevronDown, ChevronUp, Download, FileText, Flag, HelpCircle, Loader2, Milestone, Pencil,
  Save, Search, Sparkles, X,
} from "lucide-react";
import { T, sans, mono, Card, CardHead, Badge, Btn, EmptyState, fmtDateTime, StickyBar } from "./ui";
import type { BadgeVariant } from "./ui";
import { usePortalShell } from "./PortalShellContext";

interface ReviewGoal { key: string; label: string }
interface ReviewAngle { name: string; why_it_works: string; formula_fit: string; best_final_style: string }
interface ReviewAtom { text: string; activity_type: string | null; emotional_hook: string | null; season_note: string | null }
interface ReviewTour { name: string; destination: string }
interface DfsPaaSnapshot { relevance: string; people_also_ask: string[]; related_keywords: string[] }
// AA-519 Việc 5 — a non-blocking gate's own result shape (services/acp_content_writing/
// quality_gates.py::GateResultLite), narrowed to what's tenant-safe (never the full ledger).
interface ReviewFlag { gate: string; violations: string[] }

type ReadyState = "ready" | "in_progress" | "not_ready";

interface ReviewItem {
  request_id: string;
  piece_id: string;
  channel: string;
  ready_state: ReadyState;
  content_text: string | null;
  goal: ReviewGoal | null;
  angle: ReviewAngle | null;
  atom: ReviewAtom | null;
  tour: ReviewTour | null;
  dfs_paa_snapshot: DfsPaaSnapshot | null;
  cta: string | null;
  created_at: string | null;
  // AA-519 Việc 4 — null for a Segment pick or pre-Slate request (not a Route-aware piece).
  route_hub_name: string | null;
  route_segment_count: number | null;
  // AA-519 Việc 5 — [] when nothing was flagged (ADR 0023 flag-not-block).
  flags: ReviewFlag[];
}

const READY_STATE_META: Record<ReadyState, { label: string; variant: BadgeVariant }> = {
  ready: { label: "Ready", variant: "success" },
  in_progress: { label: "Writing…", variant: "info" },
  not_ready: { label: "Not Ready Yet", variant: "warning" },
};

function channelLabel(channel: string): string {
  return channel.charAt(0).toUpperCase() + channel.slice(1);
}

export function ReviewList() {
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [channelFilter, setChannelFilter] = useState<string>("all");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { showToast } = usePortalShell();

  // AA-569 — the "Open in My Content" link from AngleGateWizard.tsx's confirmation popup lands
  // here as ?piece=<piece_id>. Auto-expand + scroll to that exact card once items have loaded,
  // so "bấm link → mở đúng bài" is a real, verified behavior, not just a list landing page.
  const searchParams = useSearchParams();
  const focusPieceId = searchParams.get("piece");
  const scrolledToFocusRef = useRef(false);

  // AA-569 — hand-edit state, lifted here (not per-card local state) since saving must update
  // the shared `items` array the whole list renders from.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetch("/api/tenant/v1/content-writing/reviews")
      .then(r => (r.ok ? r.json() : { data: [] }))
      .then(d => setItems(d.data || []))
      .catch(() => setItems([]));
  }, []);

  useEffect(() => {
    if (!items || !focusPieceId || scrolledToFocusRef.current) return;
    if (!items.some(it => it.piece_id === focusPieceId)) return;
    setExpanded(prev => new Set(prev).add(focusPieceId));
    scrolledToFocusRef.current = true;
    requestAnimationFrame(() => {
      document.getElementById(`review-card-${focusPieceId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, [items, focusPieceId]);

  function startEdit(item: ReviewItem) {
    setEditingId(item.piece_id);
    setDraftText(item.content_text ?? "");
  }

  function cancelEdit() {
    setEditingId(null);
    setDraftText("");
  }

  function saveEdit(pieceId: string) {
    setSaving(true);
    fetch(`/api/tenant/v1/content-writing/pieces/${pieceId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content_text: draftText }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((updated: { content_text: string }) => {
        setItems(prev => prev ? prev.map(it => it.piece_id === pieceId ? { ...it, content_text: updated.content_text } : it) : prev);
        setEditingId(null);
        showToast("Saved");
      })
      .catch(() => showToast("Couldn't save — try again."))
      .finally(() => setSaving(false));
  }

  // AA-569 — real browser download: fetch the export endpoint (proxy already forwards the
  // correct upstream Content-Type; it does NOT forward Content-Disposition, so a plain <a href>
  // to the API would just open inline — building the Blob/download client-side sidesteps that
  // entirely and works regardless of what headers the proxy does or doesn't pass through).
  // AA-614 — `mode` only applies to blog html: "document" = a full standalone .html file (open in
  // a browser as-is), "fragment" = a bare <article> to paste into a CMS/page builder. The backend
  // (v1_content_writing.export_piece) validates mode and audit-logs it per tenant/channel so admin
  // can see document-vs-fragment usage. text export ignores mode (every non-blog channel is text).
  async function exportPiece(item: ReviewItem, format: "text" | "html", mode: "document" | "fragment" = "document") {
    const qs = format === "html" ? `format=html&mode=${mode}` : "format=text";
    const r = await fetch(`/api/tenant/v1/content-writing/pieces/${item.piece_id}/export?${qs}`);
    if (!r.ok) { showToast("Export failed — try again."); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const safeTitle = (item.angle?.name || item.channel || "content").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "content";
    const ext = format === "html" ? (mode === "fragment" ? "fragment.html" : "html") : "txt";
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safeTitle}.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  const channels = useMemo(() => {
    if (!items) return [];
    const seen = new Map<string, number>();
    for (const it of items) seen.set(it.channel, (seen.get(it.channel) || 0) + 1);
    return Array.from(seen.entries());
  }, [items]);

  const visible = useMemo(() => {
    if (!items) return [];
    return channelFilter === "all" ? items : items.filter(it => it.channel === channelFilter);
  }, [items, channelFilter]);

  function toggle(pieceId: string) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(pieceId)) next.delete(pieceId); else next.add(pieceId);
      return next;
    });
  }

  return (
    <Card>
      {/* AA-524 — sticky, bleeding out to Card's own edges (negative margin = Card's default
          padding) so it reads as one continuous bar rather than floating detached from the
          card's border once the list scrolls underneath it. bleedTop (StickyBar's default 28)
          additionally covers <main>'s own top padding — see StickyBar's own comment. */}
      <StickyBar
        background={T.card}
        style={{
          marginTop: -22, marginLeft: -22, marginRight: -22,
          paddingTop: 22, paddingLeft: 22, paddingRight: 22, paddingBottom: 14,
          borderRadius: "12px 12px 0 0",
          borderBottom: channels.length > 1 ? `1px solid ${T.line2}` : undefined,
        }}
      >
        <CardHead title="My Content" />
        {channels.length > 1 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: -4 }}>
            <FilterPill label="All" count={items?.length ?? 0} active={channelFilter === "all"} onClick={() => setChannelFilter("all")} />
            {channels.map(([ch, count]) => (
              <FilterPill key={ch} label={channelLabel(ch)} count={count} active={channelFilter === ch} onClick={() => setChannelFilter(ch)} />
            ))}
          </div>
        )}
      </StickyBar>
      {items === null ? (
        <div style={{ padding: 24, textAlign: "center", color: T.muted, fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center", gap: 8, marginTop: 14 }}>
          <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} /> Loading…
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<FileText size={28} color={T.muted2} />}
          title="Nothing written yet"
          sub="Once you've written content from Social Content, it'll show up here for you to review before publishing." // AA-576 (was "in Write Content" — stale after that nav item was removed)
        />
      ) : (
        <div style={{ marginTop: 14 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {visible.map(item => (
              <ReviewCard
                key={item.piece_id}
                item={item}
                expanded={expanded.has(item.piece_id)}
                onToggle={() => toggle(item.piece_id)}
                highlighted={item.piece_id === focusPieceId}
                editing={editingId === item.piece_id}
                draftText={draftText}
                onDraftChange={setDraftText}
                saving={saving && editingId === item.piece_id}
                onStartEdit={() => startEdit(item)}
                onCancelEdit={cancelEdit}
                onSaveEdit={() => saveEdit(item.piece_id)}
                onExport={(format, mode) => exportPiece(item, format, mode)}
                onCopyAttempt={() => showToast("Use Export to download this content.")}
              />
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function FilterPill({ label, count, active, onClick }: {
  label: string; count: number; active: boolean; onClick: () => void;
}) {
  return (
    <button onClick={onClick} style={{
      display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 12px", borderRadius: 999,
      border: `1px solid ${active ? T.gold : T.line}`, background: active ? T.goldTint : T.card,
      color: active ? T.ink : T.muted, fontSize: 12, fontWeight: 600, cursor: "pointer",
      fontFamily: sans,
    }}>
      {label} <span style={{ opacity: 0.7, fontVariantNumeric: "tabular-nums" }}>{count}</span>
    </button>
  );
}

function ReviewCard({
  item, expanded, onToggle, highlighted, editing, draftText, onDraftChange, saving,
  onStartEdit, onCancelEdit, onSaveEdit, onExport, onCopyAttempt,
}: {
  item: ReviewItem; expanded: boolean; onToggle: () => void; highlighted: boolean;
  editing: boolean; draftText: string; onDraftChange: (v: string) => void; saving: boolean;
  onStartEdit: () => void; onCancelEdit: () => void; onSaveEdit: () => void;
  onExport: (format: "text" | "html", mode?: "document" | "fragment") => void; onCopyAttempt: () => void;
}) {
  const stateMeta = READY_STATE_META[item.ready_state];
  const title = item.angle?.name || "Untitled";
  const editable = item.content_text !== null; // AA-569 — mirrors the backend's own approved/held gate

  return (
    <div id={`review-card-${item.piece_id}`} style={{ border: `1px solid ${highlighted ? T.gold : T.line}`, borderRadius: 10, overflow: "hidden" }}>
      <button onClick={onToggle} style={{
        width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between",
        gap: 12, padding: "12px 16px", background: "none", border: "none", cursor: "pointer",
        textAlign: "left", fontFamily: sans,
      }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3, flexWrap: "wrap" }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: T.ink }}>{title}</span>
            <Badge variant="default">{channelLabel(item.channel)}</Badge>
            <Badge variant={stateMeta.variant}>{stateMeta.label}</Badge>
            {/* AA-519 Việc 5 — flag is a SEPARATE badge alongside Ready, never a replacement for
                it (ADR 0023: a flag never blocks). */}
            {item.flags.length > 0 && (
              <Badge variant="warning">
                <Flag size={11} style={{ verticalAlign: -2, marginRight: 3 }} />
                Flagged
              </Badge>
            )}
            {/* AA-519 Việc 4 — Route-aware piece, distinct from a plain single-atom one. */}
            {item.route_segment_count != null && (
              <Badge variant="gold">
                <Milestone size={11} style={{ verticalAlign: -2, marginRight: 3 }} />
                Route: {item.route_hub_name ?? "—"} · {item.route_segment_count} Segments
              </Badge>
            )}
          </div>
          <div style={{ fontSize: 11.5, color: T.muted2, fontFamily: sans }}>
            {item.tour ? `${item.tour.name} · ${item.tour.destination} · ` : ""}
            {fmtDateTime(item.created_at)}
          </div>
        </div>
        {expanded ? <ChevronUp size={16} color={T.muted} /> : <ChevronDown size={16} color={T.muted} />}
      </button>

      {expanded && (
        <div style={{ padding: "0 16px 16px", display: "flex", flexDirection: "column", gap: 14 }}>
          <ContentBlock item={item} editing={editing} draftText={draftText} onDraftChange={onDraftChange} onCopyAttempt={onCopyAttempt} />
          {editable && (
            <ActionRow
              channel={item.channel} editing={editing} saving={saving}
              onEdit={onStartEdit} onCancel={onCancelEdit} onSave={onSaveEdit} onExport={onExport}
            />
          )}
          <FlagBanner flags={item.flags} />
          <ContextSection item={item} />
        </div>
      )}
    </div>
  );
}

// AA-569 — Edit/Save/Export controls. Only ever rendered when `editable` (item.content_text !==
// null, i.e. approved/held) — same boundary the PATCH/export backend endpoints enforce.
// AA-614 — blog gets two HTML export shapes (full document vs CMS fragment); every other channel
// is a single text export.
function ActionRow({ channel, editing, saving, onEdit, onCancel, onSave, onExport }: {
  channel: string; editing: boolean; saving: boolean;
  onEdit: () => void; onCancel: () => void; onSave: () => void;
  onExport: (format: "text" | "html", mode?: "document" | "fragment") => void;
}) {
  if (editing) {
    return (
      <div style={{ display: "flex", gap: 8 }}>
        <Btn variant="primary" size="sm" disabled={saving} onClick={onSave}>
          <Save size={12} /> {saving ? "Saving…" : "Save"}
        </Btn>
        <Btn variant="ghost" size="sm" disabled={saving} onClick={onCancel}>
          <X size={12} /> Cancel
        </Btn>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <Btn variant="secondary" size="sm" onClick={onEdit}><Pencil size={12} /> Edit</Btn>
      {channel === "blog" ? (
        <>
          <Btn variant="secondary" size="sm" onClick={() => onExport("text")}><Download size={12} /> Export text</Btn>
          <Btn variant="secondary" size="sm" onClick={() => onExport("html", "document")}><Download size={12} /> Export HTML file</Btn>
          <Btn variant="secondary" size="sm" onClick={() => onExport("html", "fragment")}><Download size={12} /> Export HTML for CMS</Btn>
        </>
      ) : (
        <Btn variant="secondary" size="sm" onClick={() => onExport("text")}><Download size={12} /> Export</Btn>
      )}
    </div>
  );
}

function ContentBlock({ item, editing, draftText, onDraftChange, onCopyAttempt }: {
  item: ReviewItem; editing: boolean; draftText: string; onDraftChange: (v: string) => void;
  onCopyAttempt: () => void;
}) {
  if (editing) {
    // AA-614 — the edit textarea is deliberately NOT copy-locked: the tenant is authoring their
    // own text here, and copy/paste within their own draft is normal editing. The copy-lock only
    // guards the read-only display view below, whose whole point is "export, don't copy-paste".
    return (
      <textarea
        value={draftText}
        onChange={e => onDraftChange(e.target.value)}
        rows={12}
        style={{
          width: "100%", padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.gold}`,
          background: "#fff", fontSize: 13.5, lineHeight: 1.6, color: T.body, fontFamily: sans,
          resize: "vertical", outline: "none", boxSizing: "border-box",
        }}
      />
    );
  }
  if (item.ready_state === "in_progress") {
    return (
      <div style={{
        display: "flex", alignItems: "center", gap: 8, padding: "14px 16px", borderRadius: 10,
        border: `1px solid ${T.line}`, background: T.bg, fontSize: 13, color: T.muted,
      }}>
        <Loader2 size={14} style={{ animation: "spin 1s linear infinite" }} />
        Still writing — check back in a moment.
      </div>
    );
  }
  if (item.ready_state === "not_ready" && !item.content_text) {
    return (
      <div style={{
        padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.bg,
        fontSize: 13, color: T.muted, lineHeight: 1.5,
      }}>
        This piece isn&rsquo;t ready to publish yet. Our team is on it — check back soon.
      </div>
    );
  }
  // AA-614 — copy-lock the read-only content view. The tenant must EXPORT (which is audit-logged
  // per tenant/channel, AA-613) rather than select-drag-copy the text out, so admin can actually
  // count how much content leaves the platform. This is a UX deterrent, not hard DRM — a
  // determined user can still read the DOM — but it turns the easy path (highlight + Ctrl+C /
  // right-click Copy) into the audited Export button. Guards: userSelect:none blocks the drag
  // selection itself; onCopy/onCut/onContextMenu are belt-and-suspenders for keyboard copy and
  // the context menu, each nudging the tenant toward Export.
  return (
    <div
      onCopy={(e) => { e.preventDefault(); onCopyAttempt(); }}
      onCut={(e) => { e.preventDefault(); onCopyAttempt(); }}
      onContextMenu={(e) => { e.preventDefault(); onCopyAttempt(); }}
      style={{
        padding: "14px 16px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.card,
        whiteSpace: "pre-wrap", fontSize: 13.5, lineHeight: 1.6, color: T.body, fontFamily: sans,
        userSelect: "none", WebkitUserSelect: "none", MozUserSelect: "none", msUserSelect: "none",
      }}
    >
      {item.content_text}
      {item.ready_state === "not_ready" && (
        <div style={{ marginTop: 10, fontSize: 11.5, color: T.muted, fontStyle: "italic" }}>
          Draft above isn&rsquo;t ready to publish yet — our team is reviewing it.
        </div>
      )}
    </div>
  );
}

// AA-519 Việc 5 — the "note" ADR 0023 requires every flag to carry, shown to the tenant
// directly (unlike gate_ledger's other 8 gates, which stay admin-only). Doesn't block anything —
// this piece can still be Ready/published, see the Badge next to it in ReviewCard's header.
function FlagBanner({ flags }: { flags: ReviewFlag[] }) {
  if (flags.length === 0) return null;
  return (
    <div style={{
      padding: "10px 12px", borderRadius: 8, border: `1px solid ${T.amber}`,
      background: "#FBEFD6", fontSize: 12, lineHeight: 1.6, color: T.body,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontWeight: 600, color: T.ink, marginBottom: 4 }}>
        <Flag size={12} color={T.amber} /> Flagged for your review — doesn&rsquo;t block publishing
      </div>
      {flags.flatMap(f => f.violations).map((v, i) => (
        <div key={i} style={{ color: T.muted }}>{v}</div>
      ))}
    </div>
  );
}

function ContextSection({ item }: { item: ReviewItem }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: T.muted2 }}>
        Where this came from
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {item.goal && (
          <ContextRow icon={<Sparkles size={12} />} label="Goal" value={item.goal.label} />
        )}
        {item.tour && (
          <ContextRow icon={<Search size={12} />} label="Tour" value={`${item.tour.name} (${item.tour.destination})`} />
        )}
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

      {item.dfs_paa_snapshot && (item.dfs_paa_snapshot.people_also_ask.length > 0 || item.dfs_paa_snapshot.related_keywords.length > 0) && (
        <div style={{ padding: "10px 12px", borderRadius: 8, background: T.bg, fontSize: 12, lineHeight: 1.6, color: T.body }}>
          <div style={{ display: "flex", alignItems: "center", gap: 5, fontWeight: 600, color: T.ink, marginBottom: 4 }}>
            <HelpCircle size={12} /> Search context used
          </div>
          {item.dfs_paa_snapshot.people_also_ask.length > 0 && (
            <div style={{ color: T.muted }}>
              Travelers also ask: {item.dfs_paa_snapshot.people_also_ask.join("; ")}
            </div>
          )}
          {item.dfs_paa_snapshot.related_keywords.length > 0 && (
            <div style={{ marginTop: 2, fontSize: 11, color: T.muted2 }}>
              Related terms: {item.dfs_paa_snapshot.related_keywords.join(", ")}
            </div>
          )}
        </div>
      )}

      {item.cta && (
        <div style={{ fontSize: 11.5, color: T.muted, fontFamily: mono }}>
          CTA: {item.cta}
        </div>
      )}
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

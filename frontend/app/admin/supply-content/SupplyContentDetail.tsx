"use client";
// app/admin/supply-content/SupplyContentDetail.tsx
// AA-835 — /admin/supply-content/<entity id>: one AA-Booking hotel or service as the reviewer sees it. This is the
// page catalog.v_supply_content.edit_url names (CIS 278) and Booking's "Edit in CIS" link opens.
//
//   Status         — the content status and what Booking shows now, the facts version, the Booking link
//   Draft to review — the open draft (text, photos with their rights state, the diff against the published or last
//                    reviewed version), why publish would be refused, Publish / Reject (review.act), Edit
//                    (content.write)
//   Published       — the version Booking shows (Republish after an unpublish / stale when allowed, Unpublish)
//   Photos          — every photo Booking sent, with its rights / copy state (publish needs "Cleared and copied")
//   Facts           — the Booking facts the draft was written from (read-only; change them in Booking)
//   History         — every version, newest first, with who wrote / reviewed it and the review note
//
// API (CIS BFF → api/routers/admin_supply_content.py; rules in services/supply_content/review.py):
//   GET  /{id}                                     detail
//   POST /{id}/versions                            an edit: a new human draft             content.write
//   POST /{id}/versions/{vid}/publish {note?}      approve and publish                    review.act
//   POST /{id}/versions/{vid}/reject  {note?}      reject a draft                         review.act
//   POST /{id}/unpublish                           hide it from Booking                   review.act
//   POST /{id}/draft                               queue a new LLM draft (paid, capped)   content.write
// Write controls are hidden with can() (UI only; the backend re-checks every call and the BFF refuses any write of
// the read-only viewer). No polling (S240): reload after an action, Refresh by hand.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import {
  Badge,
  Button,
  K,
  Modal,
  PageHeader,
  adminHeaders,
  formatDateTime,
  mono,
  useAdminIdentity,
  useToast, AdminMain,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { BOOKING_ADMIN_URL } from "@/lib/apps";
import {
  MAX_DESCRIPTION_CHARS,
  PHOTO_STATE,
  STATUS_HINT,
  STATUS_LABEL,
  STATUS_TONE,
  TYPE_LABEL,
  VERSION_TONE,
  bookingHref,
  factRows,
  factText,
  formFrom,
  formProblems,
  isRepublish,
  photoState,
  publishBlockers,
  supplyCall,
  versionBody,
  type Detail,
  type EditForm,
  type Media,
  type Version,
} from "@/lib/supplyContent";

type Pending =
  | { kind: "publish" | "reject"; version: Version }
  | { kind: "unpublish" }
  | { kind: "draft" }
  | null;

export default function SupplyContentDetail({ entityId }: { entityId: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const canWrite = can(role, "content.write");
  const canReview = can(role, "review.act");
  const [pending, setPending] = useState<Pending>(null);
  const [editing, setEditing] = useState<Version | null | "new">(null);

  const key = ["supply-content", entityId];
  const q = useQuery({
    queryKey: key,
    queryFn: async () => {
      const r = await supplyCall<Detail>("GET", `/${encodeURIComponent(entityId)}`, undefined, adminHeaders());
      if (!r.ok) throw Object.assign(new Error(r.message), { status: r.status });
      return r.body;
    },
    retry: false,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: key });
    void qc.invalidateQueries({ queryKey: ["supply-content-list"] });
  };

  const d = q.data;
  const title = d ? (d.entity.title || d.entity.source_id) : "Supply content";
  const crumbs = [{ label: "CIS Content" }, { label: "Assets" }, { label: "Supply content", href: "/admin/supply-content" }, { label: d ? title : "…" }];

  if (q.isError) {
    const status = (q.error as { status?: number }).status;
    return (
      <Shell>
        <PageHeader title="Supply content" breadcrumbs={crumbs.slice(0, 3)} />
        <div data-testid="supply-error" style={{ ...card, color: status === 404 ? K.body : K.danger }}>
          {status === 404 || status === 422
            ? "No hotel or service with this id. It may not have reached CIS from the Booking feed yet."
            : `Could not load it: ${(q.error as Error).message}`}
          <div style={{ marginTop: 10 }}><Link href="/admin/supply-content" style={{ color: K.accentDeep }}>Back to the list</Link></div>
        </div>
      </Shell>
    );
  }
  if (!d) return <Shell><PageHeader title="Supply content" breadcrumbs={crumbs} /><div style={{ ...card, color: K.muted }}>Loading…</div></Shell>;

  const { entity, current_draft: draft, published, media } = d;
  // A stale entity (newer facts applied) or an unpublished one whose facts moved on can never take the same
  // version back (review.publish answers 409 outdated), so offer "Publish again" only when nothing blocks it.
  const republishBlockers = published && isRepublish(entity, published) ? publishBlockers(entity, published, media) : null;
  const republishable = republishBlockers !== null && republishBlockers.length === 0;
  const editBase = draft ?? published ?? null;

  return (
    <Shell>
      <PageHeader
        title={title}
        breadcrumbs={crumbs}
        description={`${TYPE_LABEL[entity.entity_type]}${entity.service_type ? ` · ${entity.service_type}` : ""}${entity.country_code ? ` · ${entity.country_code}` : ""} · Booking id ${entity.source_id}`}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={q.isFetching}>
              <RefreshCw size={13} /> {q.isFetching ? "Loading…" : "Refresh"}
            </Button>
            <a href={bookingHref(BOOKING_ADMIN_URL, d.edit_in_booking_path)} target="_blank" rel="noreferrer"
               data-testid="supply-booking-link" style={linkBtn}>
              Edit facts in Booking <ExternalLink size={12} />
            </a>
          </>
        }
      />

      <section data-testid="supply-status" style={card}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 6 }}>
          <Badge tone={STATUS_TONE[entity.content_status]}>{STATUS_LABEL[entity.content_status]}</Badge>
          {!entity.active && <Badge tone="danger" dot={false}>Inactive in Booking</Badge>}
          <span style={{ fontSize: 12, color: K.muted }}>
            Facts v{entity.applied_supply_version}{published ? ` · published v${published.content_version}` : ""} · updated {formatDateTime(entity.updated_at)}
          </span>
        </div>
        <div style={{ fontSize: 12.5, color: K.body }}>{STATUS_HINT[entity.content_status]}</div>
        {(canWrite || canReview) && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
            {canWrite && entity.active && (
              <Button size="sm" variant="secondary" onClick={() => setEditing(editBase ?? "new")}>
                {editBase ? `Edit (new version from v${editBase.content_version})` : "Write the text"}
              </Button>
            )}
            {canWrite && entity.active && (
              <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "draft" })}
                      title="Queue a new model draft for the current facts (one paid call, capped per day)">
                Regenerate draft
              </Button>
            )}
            {canReview && published && republishable && (
              <Button size="sm" variant="primary" onClick={() => setPending({ kind: "publish", version: published })}>
                Publish v{published.content_version} again
              </Button>
            )}
            {canReview && (entity.content_status === "published" || entity.content_status === "stale") && (
              <Button size="sm" variant="danger" onClick={() => setPending({ kind: "unpublish" })}>Unpublish</Button>
            )}
          </div>
        )}
        {canReview && published && republishBlockers && republishBlockers.length > 0 && (
          <div data-testid="supply-republish-blocked" title={`Why v${published.content_version} cannot be published again`}>
            <Blockers items={republishBlockers} />
          </div>
        )}
      </section>

      <Section title={draft ? `Draft to review · v${draft.content_version}` : "Draft to review"}>
        {!draft ? (
          <div style={{ ...card, color: K.muted, fontSize: 12.5 }}>
            No open draft.{entity.content_status === "stale" || entity.content_status === "draft" ? " A draft is queued when Booking's facts change; Regenerate queues one now." : ""}
          </div>
        ) : (
          <div data-testid="supply-draft" style={card}>
            <VersionMeta v={draft} />
            <VersionBody v={draft} media={media} />
            <Blockers items={publishBlockers(entity, draft, media)} />
            {d.diff.length > 0 && (
              <details style={{ marginTop: 10 }} open>
                <summary style={{ fontSize: 12, fontWeight: 600, cursor: "pointer", color: K.ink }}>
                  Changes against {d.diff_base_version != null ? `v${d.diff_base_version}` : "nothing (first version)"}
                </summary>
                <DiffView lines={d.diff} />
              </details>
            )}
            {canReview && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                <span data-testid="supply-publish">
                  <Button size="sm" variant="primary" onClick={() => setPending({ kind: "publish", version: draft })}>Publish</Button>
                </span>
                <span data-testid="supply-reject">
                  <Button size="sm" variant="danger" onClick={() => setPending({ kind: "reject", version: draft })}>Reject</Button>
                </span>
              </div>
            )}
          </div>
        )}
      </Section>

      {published && (
        <Section title={`What Booking shows · v${published.content_version}${entity.content_status === "published" ? "" : " (not served now)"}`}>
          <div data-testid="supply-published" style={card}>
            <VersionMeta v={published} />
            <VersionBody v={published} media={media} />
          </div>
        </Section>
      )}

      <Section title={`Photos · ${media.length}`}>
        <PhotoGrid media={media} />
      </Section>

      <Section title="Booking facts (read-only)">
        <FactsTable facts={entity.facts} />
      </Section>

      <Section title={`History · ${d.versions.length} version${d.versions.length === 1 ? "" : "s"}`}>
        <History versions={d.versions} publishedId={entity.published_version_id} media={media} />
      </Section>

      {pending && (
        <ActionModal key={pending.kind + ("version" in pending ? pending.version.id : "")} pending={pending}
          entityId={entity.id} onClose={() => setPending(null)}
          onDone={(msg) => { setPending(null); toast.success(msg); refresh(); }}
          onError={(msg) => toast.error(msg, 8000)} />
      )}
      {editing && (
        <EditModal key={editing === "new" ? "new" : editing.id} entityId={entity.id} base={editing === "new" ? null : editing}
          media={media} onClose={() => setEditing(null)}
          onSaved={(v) => { setEditing(null); toast.success(`Draft v${v.content_version} saved — review it below.`); refresh(); }} />
      )}
    </Shell>
  );
}

// ── layout bits ────────────────────────────────────────────────────────────────────────────────────────────────────

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <AdminMain>
      {children}
    </AdminMain>
  );
}

const card: React.CSSProperties = {
  background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px", minWidth: 0,
};

const linkBtn: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 12px", borderRadius: 999, fontSize: 11,
  fontWeight: 600, color: K.accentDeep, border: `1px solid ${K.accentBorder}`, textDecoration: "none",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{ marginTop: 18 }}>
      <h2 style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: K.muted, margin: "0 0 6px" }}>{title}</h2>
      {children}
    </section>
  );
}

function VersionMeta({ v }: { v: Version }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 11.5, color: K.muted, marginBottom: 8 }}>
      <Badge tone={VERSION_TONE[v.status]} dot={false}>{v.status}</Badge>
      <span>{v.origin === "llm" ? "Written by the model" : `Edited by ${v.created_by ?? "staff"}`}</span>
      <span>· facts v{v.supply_version}</span>
      <span>· {formatDateTime(v.created_at)}</span>
      {v.reviewed_by && <span>· reviewed by {v.reviewed_by} {formatDateTime(v.reviewed_at)}</span>}
    </div>
  );
}

function VersionBody({ v, media }: { v: Version; media: Media[] }) {
  const byId = new Map(media.map((m) => [m.media_id, m]));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13, color: K.body, minWidth: 0 }}>
      <p style={{ margin: 0, lineHeight: 1.6, overflowWrap: "anywhere" }}>{v.description}</p>
      {v.highlights.length > 0 && (
        <div>
          <div style={subhead}>Highlights</div>
          <ul style={list}>{v.highlights.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
      )}
      {v.good_to_know.length > 0 && (
        <div>
          <div style={subhead}>Good to know</div>
          <ul style={list}>{v.good_to_know.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
      )}
      {v.photos.length > 0 && (
        <div>
          <div style={subhead}>Photos in this version</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {v.photos.map((p) => {
              const m = byId.get(p.media_id);
              const st = PHOTO_STATE[photoState(m)];
              return (
                <figure key={p.media_id} style={{ margin: 0, width: 132, minWidth: 0 }}>
                  <Thumb m={m} />
                  <figcaption style={{ fontSize: 11, color: K.muted, marginTop: 4, overflowWrap: "anywhere" }}>
                    <span title={st.hint}><Badge tone={st.tone} dot={false}>{st.label}</Badge></span>
                    {p.caption && <div>{p.caption}</div>}
                  </figcaption>
                </figure>
              );
            })}
          </div>
        </div>
      )}
      {v.review_note && <div style={{ fontSize: 12, color: K.muted }}>Review note: {v.review_note}</div>}
    </div>
  );
}

const subhead: React.CSSProperties = { fontSize: 11.5, fontWeight: 600, color: K.ink, marginBottom: 2 };
const list: React.CSSProperties = { margin: 0, paddingLeft: 18, lineHeight: 1.55, overflowWrap: "anywhere" };

function Thumb({ m, height = 88 }: { m?: Media; height?: number }) {
  if (!m?.url_600) {
    return <div style={{ width: "100%", height, borderRadius: 6, background: K.line2, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: K.muted }}>no copy</div>;
  }
  // eslint-disable-next-line @next/next/no-img-element -- CIS CDN copy of a rights-cleared photo, fixed size
  return <img src={m.url_600} alt="" loading="lazy" style={{ width: "100%", height, objectFit: "cover", borderRadius: 6, display: "block", background: K.line2 }} />;
}

function Blockers({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <div style={{ marginTop: 10, fontSize: 12, color: K.success }}>Ready to publish: facts current, every photo cleared.</div>;
  }
  return (
    <div data-testid="supply-blockers" style={{ marginTop: 10, fontSize: 12, background: K.amberSoft, color: K.ink, borderRadius: 8, padding: "8px 10px" }}>
      <b>Publish would be refused:</b>
      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{items.map((b, i) => <li key={i}>{b}</li>)}</ul>
    </div>
  );
}

function DiffView({ lines }: { lines: string[] }) {
  return (
    <pre data-testid="supply-diff" style={{ margin: "6px 0 0", fontFamily: mono, fontSize: 11.5, lineHeight: 1.5, background: K.bg, border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 320, overflow: "auto" }}>
      {lines.map((l, i) => {
        const add = l.startsWith("+") && !l.startsWith("+++");
        const del = l.startsWith("-") && !l.startsWith("---");
        return (
          <div key={i} style={{ background: add ? K.successSoft : del ? K.dangerSoft : "transparent", color: add ? K.success : del ? K.danger : l.startsWith("@@") ? K.muted : K.body }}>
            {l || " "}
          </div>
        );
      })}
    </pre>
  );
}

function PhotoGrid({ media }: { media: Media[] }) {
  if (media.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>Booking sent no photo for this entity.</div>;
  return (
    <div data-testid="supply-photos" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10 }}>
      {media.map((m) => {
        const st = PHOTO_STATE[photoState(m)];
        return (
          <div key={m.media_id} data-photo-state={photoState(m)} style={{ ...card, padding: 8 }}>
            <Thumb m={m} />
            <div style={{ marginTop: 6 }} title={st.hint}><Badge tone={st.tone} dot={false}>{st.label}</Badge></div>
            <div style={{ fontSize: 10.5, color: K.muted, marginTop: 4, overflowWrap: "anywhere" }}>
              {m.width && m.height ? `${m.width}×${m.height}` : "size unknown"}{m.copied_at ? ` · copied ${formatDateTime(m.copied_at)}` : ""}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function FactsTable({ facts }: { facts: Record<string, unknown> }) {
  const rows = factRows(facts);
  if (rows.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>No facts.</div>;
  return (
    <div style={{ ...card, display: "grid", gridTemplateColumns: "minmax(0, 180px) minmax(0, 1fr)", gap: "4px 14px", fontSize: 12.5 }}>
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted, overflowWrap: "anywhere" }}>{k}</span>
          <span style={{ color: K.body, overflowWrap: "anywhere" }}>{factText(v)}</span>
        </div>
      ))}
    </div>
  );
}

function History({ versions, publishedId, media }: { versions: Version[]; publishedId: string | null; media: Media[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (versions.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>No version yet.</div>;
  return (
    <div data-testid="supply-history" style={{ ...card, padding: 0 }}>
      {versions.map((v, i) => (
        <div key={v.id} style={{ borderTop: i ? `1px solid ${K.line}` : "none", padding: "8px 14px" }}>
          <button type="button" onClick={() => setOpen(open === v.id ? null : v.id)} aria-expanded={open === v.id}
                  style={{ all: "unset", cursor: "pointer", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 12.5, color: K.ink, width: "100%" }}>
            <b>v{v.content_version}</b>
            <Badge tone={VERSION_TONE[v.status]} dot={false}>{v.status}</Badge>
            {v.id === publishedId && <Badge tone="success" dot={false}>published</Badge>}
            <span style={{ color: K.muted, fontSize: 11.5 }}>
              {v.origin === "llm" ? "model" : v.created_by ?? "staff"} · facts v{v.supply_version} · {formatDateTime(v.created_at)}
              {v.reviewed_by ? ` · reviewed by ${v.reviewed_by}` : ""}
            </span>
          </button>
          {open === v.id && (
            <div style={{ marginTop: 8 }}><VersionBody v={v} media={media} /></div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── actions ────────────────────────────────────────────────────────────────────────────────────────────────────────

const ACTION_COPY = {
  publish: { title: "Publish this version?", confirm: "Publish", body: "Booking shows this text and its photos from now on. The previous published version and any other open draft are superseded." },
  reject: { title: "Reject this draft?", confirm: "Reject", body: "The draft is closed. Booking keeps showing what it shows now." },
  unpublish: { title: "Unpublish?", confirm: "Unpublish", body: "Booking stops showing CIS text for this entity. The approved version is kept and can be published again." },
  draft: { title: "Queue a new model draft?", confirm: "Queue draft", body: "One paid model call (about $0.02) for the current facts, within the daily cap and budget. The draft shows here when the job finishes; reload the page." },
} as const;

function ActionModal({ pending, entityId, onClose, onDone, onError }: {
  pending: NonNullable<Pending>; entityId: string; onClose: () => void; onDone: (msg: string) => void; onError: (msg: string) => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const copy = ACTION_COPY[pending.kind];
  const withNote = pending.kind === "publish" || pending.kind === "reject";
  const id = encodeURIComponent(entityId);

  const run = async () => {
    setBusy(true);
    setError(null);
    const path = pending.kind === "publish" || pending.kind === "reject"
      ? `/${id}/versions/${encodeURIComponent(pending.version.id)}/${pending.kind}`
      : `/${id}/${pending.kind}`;
    const r = await supplyCall<unknown>("POST", path, withNote ? { note: note.trim() || null } : undefined, adminHeaders());
    setBusy(false);
    if (!r.ok) { setError(r.message); onError(r.message); return; }
    onDone(pending.kind === "publish" ? "Published — Booking shows it now."
      : pending.kind === "reject" ? "Draft rejected."
      : pending.kind === "unpublish" ? "Unpublished — Booking shows no CIS text now."
      : "Draft queued — reload in a minute.");
  };

  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} title={copy.title} width={500}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <span data-testid={`supply-confirm-${pending.kind}`}>
            <Button variant={pending.kind === "reject" || pending.kind === "unpublish" ? "danger" : "primary"} onClick={() => void run()} disabled={busy}>
              {busy ? "Working…" : copy.confirm}
            </Button>
          </span>
        </>
      }>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13, color: K.body }}>
        <div>{copy.body}</div>
        {withNote && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: K.muted }}>
            Note (optional, kept with the version)
            <textarea value={note} maxLength={1000} rows={3} onChange={(e) => setNote(e.target.value)} style={field} />
          </label>
        )}
        {error && <div role="alert" style={{ color: K.danger, fontSize: 12.5 }}>{error}</div>}
      </div>
    </Modal>
  );
}

const field: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", fontSize: 13, lineHeight: 1.5, background: K.card, color: K.ink,
  border: `1px solid ${K.line}`, borderRadius: 8, padding: "7px 9px", fontFamily: "inherit", resize: "vertical",
};

function EditModal({ entityId, base, media, onClose, onSaved }: {
  entityId: string; base: Version | null; media: Media[]; onClose: () => void; onSaved: (v: Version) => void;
}) {
  const [form, setForm] = useState<EditForm>(() => formFrom(base));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = formProblems(form);
  const set = (patch: Partial<EditForm>) => setForm((f) => ({ ...f, ...patch }));
  const togglePhoto = (id: string) =>
    set({ photoIds: form.photoIds.includes(id) ? form.photoIds.filter((x) => x !== id) : [...form.photoIds, id] });

  const save = async () => {
    if (problems.length) return;
    setBusy(true);
    setError(null);
    const r = await supplyCall<Version>("POST", `/${encodeURIComponent(entityId)}/versions`, versionBody(form, base), adminHeaders());
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    onSaved(r.body);
  };

  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} width={640}
      title={base ? `New version from v${base.content_version}` : "Write the text"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <span data-testid="supply-edit-save">
            <Button variant="primary" onClick={() => void save()} disabled={busy || problems.length > 0}>{busy ? "Saving…" : "Save as draft"}</Button>
          </span>
        </>
      }>
      <form data-testid="supply-edit" onSubmit={(e) => { e.preventDefault(); void save(); }} style={{ display: "flex", flexDirection: "column", gap: 12, fontSize: 12.5 }}>
        <div style={{ color: K.muted }}>Saving writes a new draft for the current facts; nothing changes in Booking until it is published.</div>
        <label style={lbl}>Description ({form.description.trim().length} / {MAX_DESCRIPTION_CHARS})
          <textarea value={form.description} rows={6} maxLength={MAX_DESCRIPTION_CHARS + 200} onChange={(e) => set({ description: e.target.value })} style={field} />
        </label>
        <label style={lbl}>Highlights (one per line, up to 5)
          <textarea value={form.highlights} rows={4} onChange={(e) => set({ highlights: e.target.value })} style={field} />
        </label>
        <label style={lbl}>Good to know (one per line, up to 3)
          <textarea value={form.goodToKnow} rows={3} onChange={(e) => set({ goodToKnow: e.target.value })} style={field} />
        </label>
        {media.length > 0 && (
          <fieldset style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", margin: 0, minWidth: 0 }}>
            <legend style={{ fontSize: 12, color: K.muted, padding: "0 4px" }}>Photos (only cleared and copied photos can be published)</legend>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {media.map((m) => {
                const st = PHOTO_STATE[photoState(m)];
                return (
                  <label key={m.media_id} style={{ display: "flex", gap: 8, alignItems: "center", minWidth: 0 }}>
                    <input type="checkbox" checked={form.photoIds.includes(m.media_id)} onChange={() => togglePhoto(m.media_id)} />
                    <span style={{ width: 56, flexShrink: 0 }}><Thumb m={m} height={40} /></span>
                    <Badge tone={st.tone} dot={false}>{st.label}</Badge>
                    <span style={{ fontFamily: mono, fontSize: 10.5, color: K.muted, overflowWrap: "anywhere", minWidth: 0 }}>{m.media_id.slice(0, 8)}</span>
                  </label>
                );
              })}
            </div>
          </fieldset>
        )}
        {problems.length > 0 && <ul style={{ margin: 0, paddingLeft: 18, color: K.amber }}>{problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
        {error && <div role="alert" style={{ color: K.danger }}>{error}</div>}
      </form>
    </Modal>
  );
}

const lbl: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 4, color: K.ink, fontWeight: 600, fontSize: 12 };

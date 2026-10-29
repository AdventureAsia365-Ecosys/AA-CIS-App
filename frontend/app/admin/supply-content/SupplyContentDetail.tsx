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
  mono,
  useAdminIdentity,
  useFormat,
  useT,
  useToast, AdminMain,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { BOOKING_ADMIN_URL } from "@/lib/apps";
import {
  MAX_DESCRIPTION_CHARS,
  STATUS_TONE,
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
import { SC } from "./messages";
import { useSupplyText } from "./useSupplyText";

type Pending =
  | { kind: "publish" | "reject"; version: Version }
  | { kind: "unpublish" }
  | { kind: "draft" }
  | null;

export default function SupplyContentDetail({ entityId }: { entityId: string }) {
  const t = useT(SC);
  const st = useSupplyText();
  const { formatDateTime } = useFormat();
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
      const r = await supplyCall<Detail>("GET", `/${encodeURIComponent(entityId)}`, undefined, adminHeaders(), st.words);
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
  const title = d ? (d.entity.title || d.entity.source_id) : t("title");
  const crumbs = [{ label: t("cisContent") }, { label: t("assets") }, { label: t("title"), href: "/admin/supply-content" }, { label: d ? title : "…" }];

  if (q.isError) {
    const status = (q.error as { status?: number }).status;
    return (
      <Shell>
        <PageHeader title={t("title")} breadcrumbs={crumbs.slice(0, 3)} />
        <div data-testid="supply-error" style={{ ...card, color: status === 404 ? K.body : K.danger }}>
          {status === 404 || status === 422
            ? t("notFound")
            : t("loadError", (q.error as Error).message)}
          <div style={{ marginTop: 10 }}><Link href="/admin/supply-content" style={{ color: K.accentDeep }}>{t("backToList")}</Link></div>
        </div>
      </Shell>
    );
  }
  if (!d) return <Shell><PageHeader title={t("title")} breadcrumbs={crumbs} /><div style={{ ...card, color: K.muted }}>{t("loading")}</div></Shell>;

  const { entity, current_draft: draft, published, media } = d;
  // A stale entity (newer facts applied) or an unpublished one whose facts moved on can never take the same
  // version back (review.publish answers 409 outdated), so offer "Publish again" only when nothing blocks it.
  const republishBlockers = published && isRepublish(entity, published) ? publishBlockers(entity, published, media, st.words) : null;
  const republishable = republishBlockers !== null && republishBlockers.length === 0;
  const editBase = draft ?? published ?? null;

  return (
    <Shell>
      <PageHeader
        title={title}
        breadcrumbs={crumbs}
        description={`${st.type[entity.entity_type]}${entity.service_type ? ` · ${entity.service_type}` : ""}${entity.country_code ? ` · ${entity.country_code}` : ""}${t("bookingId", entity.source_id)}`}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={q.isFetching}>
              <RefreshCw size={13} /> {q.isFetching ? t("loading") : t("refresh")}
            </Button>
            <a href={bookingHref(BOOKING_ADMIN_URL, d.edit_in_booking_path)} target="_blank" rel="noreferrer"
               data-testid="supply-booking-link" style={linkBtn}>
              {t("editInBooking")} <ExternalLink size={12} />
            </a>
          </>
        }
      />

      <section data-testid="supply-status" style={card}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 6 }}>
          <Badge tone={STATUS_TONE[entity.content_status]}>{st.status[entity.content_status]}</Badge>
          {!entity.active && <Badge tone="danger" dot={false}>{t("inactiveInBooking")}</Badge>}
          <span style={{ fontSize: 12, color: K.muted }}>
            {t("factsLine", entity.applied_supply_version, published ? t("publishedV", published.content_version) : "", formatDateTime(entity.updated_at))}
          </span>
        </div>
        <div style={{ fontSize: 12.5, color: K.body }}>{st.hint[entity.content_status]}</div>
        {(canWrite || canReview) && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10 }}>
            {canWrite && entity.active && (
              <Button size="sm" variant="secondary" onClick={() => setEditing(editBase ?? "new")}>
                {editBase ? t("editFrom", editBase.content_version) : t("writeText")}
              </Button>
            )}
            {canWrite && entity.active && (
              <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "draft" })}
                      title={t("regenerateTitle")}>
                {t("regenerate")}
              </Button>
            )}
            {canReview && published && republishable && (
              <Button size="sm" variant="primary" onClick={() => setPending({ kind: "publish", version: published })}>
                {t("publishAgain", published.content_version)}
              </Button>
            )}
            {canReview && (entity.content_status === "published" || entity.content_status === "stale") && (
              <Button size="sm" variant="danger" onClick={() => setPending({ kind: "unpublish" })}>{t("unpublish")}</Button>
            )}
          </div>
        )}
        {canReview && published && republishBlockers && republishBlockers.length > 0 && (
          <div data-testid="supply-republish-blocked" title={t("whyNotAgain", published.content_version)}>
            <Blockers items={republishBlockers} />
          </div>
        )}
      </section>

      <Section title={draft ? t("draftToReviewV", draft.content_version) : t("draftToReview")}>
        {!draft ? (
          <div style={{ ...card, color: K.muted, fontSize: 12.5 }}>
            {t("noOpenDraft")}{entity.content_status === "stale" || entity.content_status === "draft" ? t("draftQueuedHint") : ""}
          </div>
        ) : (
          <div data-testid="supply-draft" style={card}>
            <VersionMeta v={draft} />
            <VersionBody v={draft} media={media} />
            <Blockers items={publishBlockers(entity, draft, media, st.words)} />
            {d.diff.length > 0 && (
              <details style={{ marginTop: 10 }} open>
                <summary style={{ fontSize: 12, fontWeight: 600, cursor: "pointer", color: K.ink }}>
                  {t("changesAgainst", d.diff_base_version != null ? `v${d.diff_base_version}` : t("firstVersion"))}
                </summary>
                <DiffView lines={d.diff} />
              </details>
            )}
            {canReview && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
                <span data-testid="supply-publish">
                  <Button size="sm" variant="primary" onClick={() => setPending({ kind: "publish", version: draft })}>{t("publish")}</Button>
                </span>
                <span data-testid="supply-reject">
                  <Button size="sm" variant="danger" onClick={() => setPending({ kind: "reject", version: draft })}>{t("reject")}</Button>
                </span>
              </div>
            )}
          </div>
        )}
      </Section>

      {published && (
        <Section title={t("whatBookingShows", published.content_version, entity.content_status === "published" ? "" : t("notServed"))}>
          <div data-testid="supply-published" style={card}>
            <VersionMeta v={published} />
            <VersionBody v={published} media={media} />
          </div>
        </Section>
      )}

      <Section title={t("photosCount", media.length)}>
        <PhotoGrid media={media} />
      </Section>

      <Section title={t("factsReadOnly")}>
        <FactsTable facts={entity.facts} />
      </Section>

      <Section title={t("history", d.versions.length)}>
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
          onSaved={(v) => { setEditing(null); toast.success(t("draftSaved", v.content_version)); refresh(); }} />
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
  const t = useT(SC);
  const { formatDateTime } = useFormat();
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 11.5, color: K.muted, marginBottom: 8 }}>
      <Badge tone={VERSION_TONE[v.status]} dot={false}>{t("versionStatus", v.status)}</Badge>
      <span>{v.origin === "llm" ? t("writtenByModel") : t("editedBy", v.created_by ?? t("staff"))}</span>
      <span>{t("factsV", v.supply_version)}</span>
      <span>· {formatDateTime(v.created_at)}</span>
      {v.reviewed_by && <span>{t("reviewedBy", v.reviewed_by, formatDateTime(v.reviewed_at))}</span>}
    </div>
  );
}

function VersionBody({ v, media }: { v: Version; media: Media[] }) {
  const t = useT(SC);
  const sx = useSupplyText();
  const byId = new Map(media.map((m) => [m.media_id, m]));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, fontSize: 13, color: K.body, minWidth: 0 }}>
      <p style={{ margin: 0, lineHeight: 1.6, overflowWrap: "anywhere" }}>{v.description}</p>
      {v.highlights.length > 0 && (
        <div>
          <div style={subhead}>{t("highlights")}</div>
          <ul style={list}>{v.highlights.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
      )}
      {v.good_to_know.length > 0 && (
        <div>
          <div style={subhead}>{t("goodToKnow")}</div>
          <ul style={list}>{v.good_to_know.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
      )}
      {v.photos.length > 0 && (
        <div>
          <div style={subhead}>{t("photosInVersion")}</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {v.photos.map((p) => {
              const m = byId.get(p.media_id);
              const st = sx.photo(photoState(m));
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
      {v.review_note && <div style={{ fontSize: 12, color: K.muted }}>{t("reviewNote", v.review_note)}</div>}
    </div>
  );
}

const subhead: React.CSSProperties = { fontSize: 11.5, fontWeight: 600, color: K.ink, marginBottom: 2 };
const list: React.CSSProperties = { margin: 0, paddingLeft: 18, lineHeight: 1.55, overflowWrap: "anywhere" };

function Thumb({ m, height = 88 }: { m?: Media; height?: number }) {
  const t = useT(SC);
  if (!m?.url_600) {
    return <div style={{ width: "100%", height, borderRadius: 6, background: K.line2, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: K.muted }}>{t("noCopy")}</div>;
  }
  // eslint-disable-next-line @next/next/no-img-element -- CIS CDN copy of a rights-cleared photo, fixed size
  return <img src={m.url_600} alt="" loading="lazy" style={{ width: "100%", height, objectFit: "cover", borderRadius: 6, display: "block", background: K.line2 }} />;
}

function Blockers({ items }: { items: string[] }) {
  const t = useT(SC);
  if (items.length === 0) {
    return <div style={{ marginTop: 10, fontSize: 12, color: K.success }}>{t("readyToPublish")}</div>;
  }
  return (
    <div data-testid="supply-blockers" style={{ marginTop: 10, fontSize: 12, background: K.amberSoft, color: K.ink, borderRadius: 8, padding: "8px 10px" }}>
      <b>{t("publishRefused")}</b>
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
  const t = useT(SC);
  const sx = useSupplyText();
  const { formatDateTime } = useFormat();
  if (media.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>{t("noPhoto")}</div>;
  return (
    <div data-testid="supply-photos" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10 }}>
      {media.map((m) => {
        const st = sx.photo(photoState(m));
        return (
          <div key={m.media_id} data-photo-state={photoState(m)} style={{ ...card, padding: 8 }}>
            <Thumb m={m} />
            <div style={{ marginTop: 6 }} title={st.hint}><Badge tone={st.tone} dot={false}>{st.label}</Badge></div>
            <div style={{ fontSize: 10.5, color: K.muted, marginTop: 4, overflowWrap: "anywhere" }}>
              {m.width && m.height ? `${m.width}×${m.height}` : t("sizeUnknown")}{m.copied_at ? t("copiedAt", formatDateTime(m.copied_at)) : ""}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function FactsTable({ facts }: { facts: Record<string, unknown> }) {
  const t = useT(SC);
  const rows = factRows(facts);
  if (rows.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>{t("noFacts")}</div>;
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
  const t = useT(SC);
  const { formatDateTime } = useFormat();
  const [open, setOpen] = useState<string | null>(null);
  if (versions.length === 0) return <div style={{ ...card, fontSize: 12.5, color: K.muted }}>{t("noVersion")}</div>;
  return (
    <div data-testid="supply-history" style={{ ...card, padding: 0 }}>
      {versions.map((v, i) => (
        <div key={v.id} style={{ borderTop: i ? `1px solid ${K.line}` : "none", padding: "8px 14px" }}>
          <button type="button" onClick={() => setOpen(open === v.id ? null : v.id)} aria-expanded={open === v.id}
                  style={{ all: "unset", cursor: "pointer", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 12.5, color: K.ink, width: "100%" }}>
            <b>v{v.content_version}</b>
            <Badge tone={VERSION_TONE[v.status]} dot={false}>{t("versionStatus", v.status)}</Badge>
            {v.id === publishedId && <Badge tone="success" dot={false}>{t("publishedBadge")}</Badge>}
            <span style={{ color: K.muted, fontSize: 11.5 }}>
              {t("historyLine", v.origin === "llm" ? t("model") : v.created_by ?? t("staff"), v.supply_version, formatDateTime(v.created_at))}
              {v.reviewed_by ? t("reviewedByShort", v.reviewed_by) : ""}
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

// Message keys (messages.ts) of each confirmation.
const ACTION_COPY = {
  publish: { title: "publishTitle", confirm: "publish", body: "publishBody", done: "donePublish" },
  reject: { title: "rejectTitle", confirm: "reject", body: "rejectBody", done: "doneReject" },
  unpublish: { title: "unpublishTitle", confirm: "unpublish", body: "unpublishBody", done: "doneUnpublish" },
  draft: { title: "draftTitle", confirm: "draftConfirm", body: "draftBody", done: "doneDraft" },
} as const;

function ActionModal({ pending, entityId, onClose, onDone, onError }: {
  pending: NonNullable<Pending>; entityId: string; onClose: () => void; onDone: (msg: string) => void; onError: (msg: string) => void;
}) {
  const t = useT(SC);
  const sx = useSupplyText();
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
    const r = await supplyCall<unknown>("POST", path, withNote ? { note: note.trim() || null } : undefined, adminHeaders(), sx.words);
    setBusy(false);
    if (!r.ok) { setError(r.message); onError(r.message); return; }
    onDone(t(copy.done));
  };

  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} title={t(copy.title)} width={500}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <span data-testid={`supply-confirm-${pending.kind}`}>
            <Button variant={pending.kind === "reject" || pending.kind === "unpublish" ? "danger" : "primary"} onClick={() => void run()} disabled={busy}>
              {busy ? t("working") : t(copy.confirm)}
            </Button>
          </span>
        </>
      }>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13, color: K.body }}>
        <div>{t(copy.body)}</div>
        {withNote && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: K.muted }}>
            {t("noteLabel")}
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
  const t = useT(SC);
  const sx = useSupplyText();
  const [form, setForm] = useState<EditForm>(() => formFrom(base));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = formProblems(form, sx.words);
  const set = (patch: Partial<EditForm>) => setForm((f) => ({ ...f, ...patch }));
  const togglePhoto = (id: string) =>
    set({ photoIds: form.photoIds.includes(id) ? form.photoIds.filter((x) => x !== id) : [...form.photoIds, id] });

  const save = async () => {
    if (problems.length) return;
    setBusy(true);
    setError(null);
    const r = await supplyCall<Version>("POST", `/${encodeURIComponent(entityId)}/versions`, versionBody(form, base), adminHeaders(), sx.words);
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    onSaved(r.body);
  };

  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} width={640}
      title={base ? t("newVersionFrom", base.content_version) : t("writeText")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <span data-testid="supply-edit-save">
            <Button variant="primary" onClick={() => void save()} disabled={busy || problems.length > 0}>{busy ? t("saving") : t("saveDraft")}</Button>
          </span>
        </>
      }>
      <form data-testid="supply-edit" onSubmit={(e) => { e.preventDefault(); void save(); }} style={{ display: "flex", flexDirection: "column", gap: 12, fontSize: 12.5 }}>
        <div style={{ color: K.muted }}>{t("editIntro")}</div>
        <label style={lbl}>{t("description", form.description.trim().length, MAX_DESCRIPTION_CHARS)}
          <textarea value={form.description} rows={6} maxLength={MAX_DESCRIPTION_CHARS + 200} onChange={(e) => set({ description: e.target.value })} style={field} />
        </label>
        <label style={lbl}>{t("highlightsLabel")}
          <textarea value={form.highlights} rows={4} onChange={(e) => set({ highlights: e.target.value })} style={field} />
        </label>
        <label style={lbl}>{t("goodToKnowLabel")}
          <textarea value={form.goodToKnow} rows={3} onChange={(e) => set({ goodToKnow: e.target.value })} style={field} />
        </label>
        {media.length > 0 && (
          <fieldset style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 10px", margin: 0, minWidth: 0 }}>
            <legend style={{ fontSize: 12, color: K.muted, padding: "0 4px" }}>{t("photosLegend")}</legend>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {media.map((m) => {
                const st = sx.photo(photoState(m));
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

"use client";
// app/admin/aa-content/AaContentEditor.tsx
// AA-977 (CRM-B5, slice S3) — the editor drawer of one AA content group: the English and Vietnamese copies side by
// side (one item per locale, joined by group_key), each with its draft form, a plain preview, the actions and the
// version history.
//
// API (CIS BFF → api/routers/admin_aa_content.py; every write sends the item's updated_at as expected_updated_at):
//   GET    /api/admin/aa-content/groups/{group_key}      both locales with every version
//   POST   /api/admin/aa-content                          add the missing locale (same kind and group_key)
//   PUT    /api/admin/aa-content/{id}/draft               save the draft (full replace; starts draft n+1 after a publish)
//   DELETE /api/admin/aa-content/{id}/draft?expected_updated_at=   discard the draft (refused for a never-published item)
//   POST   /api/admin/aa-content/{id}/publish|retire|restore      admin only (master.admin)
// Refusals: 422 {code, field} next to the field (visa_needs_official_link on body_markdown), 409 stale = "someone
// else saved" with a Reload button, other 409s as their detail.
//
// The preview is plain text (escaped by React, whitespace kept): no markdown renderer is added for one page. The
// visa guidance mirrors services/aa_content/validate.py so the writer sees the problem before an admin publishes.
// Controls follow the display role (can()); the backend re-checks every call.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  Drawer,
  ErrorState,
  K,
  RADIUS,
  SkeletonBar,
  apiGet,
  formatDateTime,
  mono,
  sans,
  useAdminIdentity,
  useToast,
} from "../../_kit";
import { can } from "@/lib/rbac";
import {
  AA_CONTENT_API,
  BODY_MAX,
  KIND_LABEL,
  LOCALES,
  LOCALE_LABEL,
  aaCall,
  appliesFromForm,
  baselineVersion,
  countryLabel,
  countryOptions,
  formFromVersion,
  formProblems,
  isDirty,
  pendingDraftLabel,
  stateBadge,
  visaSectionsWithoutLink,
  type AaForm,
  type AaGroup,
  type AaItem,
  type AaLocale,
  type AaPublishResult,
  type AaResult,
  type AaVersion,
} from "@/lib/aaContent";

// ── small building blocks (shared with AaContentConsole.tsx) ───────────────────────────────────────────────────────

export const fieldStyle: React.CSSProperties = {
  fontSize: 13.5, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: RADIUS.sm, background: K.card,
  color: K.ink, width: "100%", boxSizing: "border-box", fontFamily: sans, minHeight: 36,
};

export const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, lineHeight: 1.5, fontFamily: sans };

export function Field({ label, htmlFor, hint, error, children }: {
  label: string; htmlFor: string; hint?: React.ReactNode; error?: string | null; children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 0 }}>
      <label htmlFor={htmlFor} style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{label}</label>
      {children}
      {error ? <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>
        : hint ? <span style={{ fontSize: 12, color: K.muted }}>{hint}</span> : null}
    </div>
  );
}

export function InlineError({ message, action }: { message: string; action?: React.ReactNode }) {
  return (
    <div role="alert" style={{
      display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 10px", borderRadius: RADIUS.md,
      background: K.dangerSoft, color: K.danger, fontSize: 12.5, lineHeight: 1.45, fontFamily: sans, flexWrap: "wrap",
    }}>
      <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
      <span style={{ color: K.ink, overflowWrap: "anywhere", flex: "1 1 200px", minWidth: 0 }}>{message}</span>
      {action}
    </div>
  );
}

// ── the drawer ──────────────────────────────────────────────────────────────────────────────────────────────────

export const groupKeyOf = (groupKey: string) => ["aa-content-group", groupKey] as const;

export default function AaContentEditor({ groupKey, onClose, onChanged }: {
  groupKey: string; onClose: () => void; onChanged: () => void;
}) {
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const canWrite = can(role, "content.write");
  const canAdmin = can(role, "master.admin");
  // content_ids with unsaved edits: read only when the drawer is closed (a ref, so typing re-renders nothing here).
  const dirty = useRef<Set<string>>(new Set());

  const group = useQuery({
    queryKey: groupKeyOf(groupKey),
    queryFn: () => apiGet<AaGroup>(`${AA_CONTENT_API}/groups/${encodeURIComponent(groupKey)}`),
    retry: false,
  });

  const reload = () => {
    dirty.current.clear();
    void qc.invalidateQueries({ queryKey: groupKeyOf(groupKey) });
    onChanged();
  };
  const changed = (item: AaItem) => {
    dirty.current.delete(item.content_id);
    // Put the answer in place at once (the panel remounts on the new updated_at), then refetch the list.
    qc.setQueryData<AaGroup>(groupKeyOf(groupKey), (g) => g && {
      ...g, items: g.items.some((x) => x.content_id === item.content_id)
        ? g.items.map((x) => (x.content_id === item.content_id ? item : x))
        : [...g.items, item],
    });
    onChanged();
  };
  const close = () => {
    if (dirty.current.size && !window.confirm("You have unsaved changes. Close the editor and lose them?")) return;
    onClose();
  };

  const items = group.data?.items ?? [];
  const first = items[0];

  return (
    <Drawer open onClose={close} width={1120}
      title={
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span>{groupKey}</span>
          {first && <Badge tone="purple" dot={false}>{KIND_LABEL[first.kind] ?? first.kind}</Badge>}
        </span>
      }>
      <div data-testid="aa-content-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {!canWrite && (
          <div style={noteStyle}>Read only: your role can read AA content but not change it.</div>
        )}
        {group.isLoading ? (
          <SkeletonBar w="100%" h={240} />
        ) : group.error ? (
          <ErrorState message={(group.error as Error).message || "Could not load this group."} onRetry={reload} />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))", gap: 14 }}>
            {LOCALES.map((loc) => {
              const item = items.find((x) => x.locale === loc);
              if (item) {
                return (
                  <LocalePanel key={`${item.content_id}:${item.updated_at}`} item={item} canWrite={canWrite}
                    canAdmin={canAdmin} onChanged={changed} onReload={reload}
                    onDirty={(d) => { if (d) dirty.current.add(item.content_id); else dirty.current.delete(item.content_id); }} />
                );
              }
              return first ? (
                <MissingLocale key={loc} locale={loc} sibling={first} canWrite={canWrite} onCreated={changed} />
              ) : null;
            })}
          </div>
        )}
      </div>
    </Drawer>
  );
}

// ── one locale ──────────────────────────────────────────────────────────────────────────────────────────────────

type Pending = null | "save" | "discard" | "publish" | "retire" | "restore";
type Confirm = null | "discard" | "publish" | "retire" | "restore";

function LocalePanel({ item, canWrite, canAdmin, onChanged, onReload, onDirty }: {
  item: AaItem; canWrite: boolean; canAdmin: boolean; onChanged: (item: AaItem) => void; onReload: () => void;
  onDirty: (dirty: boolean) => void;
}) {
  const toast = useToast();
  const baseVersion = baselineVersion(item);
  const [base] = useState<AaForm>(() => formFromVersion(baseVersion));
  const [form, setForm] = useState<AaForm>(base);
  const [preview, setPreview] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [refusal, setRefusal] = useState<{ message: string; code: string | null; field: string | null } | null>(null);

  const retired = item.state === "retired";
  const editable = canWrite && !retired;
  const draft = item.versions.find((v) => v.status === "draft") ?? null;
  const dirty = isDirty(form, base);
  const problems = formProblems(form);
  const visaGaps = item.kind === "prep_pack" ? visaSectionsWithoutLink(form.body_markdown) : [];
  const nextVersion = draft ? draft.version : (item.versions[0]?.version ?? 0) + 1;
  const id = item.content_id;
  const locale = item.locale.toUpperCase();

  const edit = (patch: Partial<AaForm>) => {
    const next = { ...form, ...patch };
    setForm(next);
    onDirty(isDirty(next, base));
    if (refusal?.field) setRefusal(null);
  };

  const run = async <T,>(what: Exclude<Pending, null>, call: () => Promise<AaResult<T>>, done: (body: T) => void) => {
    setPending(what);
    setRefusal(null);
    const r = await call();
    setPending(null);
    setConfirm(null);
    if (!r.ok) {
      setRefusal({ message: r.message, code: r.code, field: r.field });
      return;
    }
    done(r.body);
  };

  const expected = { expected_updated_at: item.updated_at };
  const save = () => run<AaItem>("save",
    () => aaCall("PUT", `/${id}/draft`, {
      title: form.title.trim(), body_markdown: form.body_markdown, applies_to: appliesFromForm(form), ...expected,
    }),
    (next) => { toast.success(`${locale}: draft v${next.draft_version ?? nextVersion} saved.`); onChanged(next); });
  const discard = () => run<AaItem>("discard",
    () => aaCall("DELETE", `/${id}/draft?expected_updated_at=${encodeURIComponent(item.updated_at)}`),
    (next) => { toast.success(`${locale}: draft discarded.`); onChanged(next); });
  const publish = () => run<AaPublishResult>("publish", () => aaCall("POST", `/${id}/publish`, expected),
    (res) => { toast.success(`${locale}: version ${res.item.published_version} published.`); onChanged(res.item); });
  const retire = () => run<AaItem>("retire", () => aaCall("POST", `/${id}/retire`, expected),
    (next) => { toast.success(`${locale}: retired.`); onChanged(next); });
  const restore = () => run<AaItem>("restore", () => aaCall("POST", `/${id}/restore`, expected),
    (next) => { toast.success(`${locale}: restored.`); onChanged(next); });

  const fieldError = (field: string) => (refusal?.field === field ? refusal.message : null);
  const badge = stateBadge(item);
  const pendingLabel = pendingDraftLabel(item);
  const canSave = editable && dirty && !Object.keys(problems).length && !pending;
  const canPublish = canAdmin && !retired && !!draft && !dirty && !pending;
  const fid = (name: string) => `aa-${id}-${name}`;

  return (
    <section data-testid={`aa-content-panel-${item.locale}`} style={{
      border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px", display: "flex",
      flexDirection: "column", gap: 12, minWidth: 0,
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: K.ink, fontFamily: sans }}>
          {LOCALE_LABEL[item.locale as AaLocale] ?? item.locale}
        </h3>
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          <Badge tone={badge.tone}>{badge.label}</Badge>
          {pendingLabel && <Badge tone="warning" dot={false}>{pendingLabel}</Badge>}
        </span>
      </div>

      {retired && (
        <div style={noteStyle}>
          Retired {formatDateTime(item.retired_at)}: hidden from the CRM list. Agencies that use a published version
          can still fetch it. {canAdmin ? "Restore the item to edit it again." : ""}
        </div>
      )}
      {editable && !draft && item.versions.length > 0 && (
        <div style={noteStyle}>
          Version {item.versions[0].version} is published. Saving starts draft v{nextVersion}; agencies keep
          v{item.versions[0].version} until an admin publishes the new one.
        </div>
      )}

      <Field label="Title" htmlFor={fid("title")} error={(dirty && problems.title) || fieldError("title")}>
        <input id={fid("title")} type="text" autoComplete="off" value={form.title} readOnly={!editable}
          onChange={(e) => edit({ title: e.target.value })} style={fieldStyle} maxLength={400} />
      </Field>

      <Field label="Text" htmlFor={fid("body")}
        error={problems.body_markdown ?? fieldError("body_markdown")}
        hint={`${form.body_markdown.length.toLocaleString("en")} / ${BODY_MAX.toLocaleString("en")} characters. Markdown; the preview shows the plain text.`}>
        <div style={{ display: "flex", gap: 6 }} role="tablist" aria-label="Text view">
          <Button size="sm" variant={preview ? "ghost" : "secondary"} onClick={() => setPreview(false)}>Write</Button>
          <Button size="sm" variant={preview ? "secondary" : "ghost"} onClick={() => setPreview(true)}>Preview</Button>
        </div>
        {preview ? (
          <div data-testid="aa-content-preview" style={{
            ...fieldStyle, whiteSpace: "pre-wrap", overflowWrap: "anywhere", minHeight: 260, maxHeight: 480,
            overflowY: "auto", lineHeight: 1.55, background: K.bg,
          }}>
            {form.body_markdown || <span style={{ color: K.muted }}>(empty)</span>}
          </div>
        ) : (
          <textarea id={fid("body")} value={form.body_markdown} readOnly={!editable} rows={14}
            onChange={(e) => edit({ body_markdown: e.target.value })}
            style={{ ...fieldStyle, fontFamily: mono, fontSize: 12.5, lineHeight: 1.5, resize: "vertical", minHeight: 260 }} />
        )}
      </Field>

      {item.kind === "prep_pack" && (
        visaGaps.length ? (
          <div role="status" style={{
            display: "flex", gap: 8, padding: "8px 10px", borderRadius: RADIUS.md, background: K.amberSoft,
            fontSize: 12.5, lineHeight: 1.45, color: K.ink,
          }}>
            <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2, color: K.amber }} />
            <span style={{ overflowWrap: "anywhere" }}>
              Visa information must link to the official source (an https link in the same section), or publishing is
              refused. Missing in: &ldquo;{visaGaps[0]}&rdquo;{visaGaps.length > 1 ? ` and ${visaGaps.length - 1} more` : ""}.
            </span>
          </div>
        ) : (
          <div style={noteStyle}>Visa rule: every section that mentions a visa needs the official https link (e-visa or immigration site).</div>
        )
      )}

      <Field label="Countries" htmlFor={fid("countries")} error={fieldError("applies_to.country_codes")}
        hint={form.country_codes.length ? null : "None picked: the item applies to every country."}>
        <div id={fid("countries")} role="group" aria-label="Countries" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {countryOptions(form.country_codes).map((c) => {
            const on = form.country_codes.includes(c.code);
            return (
              <button key={c.code} type="button" aria-pressed={on} disabled={!editable}
                onClick={() => edit({
                  country_codes: on ? form.country_codes.filter((x) => x !== c.code) : [...form.country_codes, c.code],
                })}
                style={{
                  fontSize: 12, padding: "3px 9px", borderRadius: RADIUS.pill, fontFamily: sans,
                  border: `1px solid ${on ? K.accentBorder : K.line}`, background: on ? K.accentTint : "transparent",
                  color: on ? K.accentDeep : K.muted, cursor: editable ? "pointer" : "default",
                }}>
                {c.name}
              </button>
            );
          })}
        </div>
      </Field>

      <Field label="Tours (optional)" htmlFor={fid("tours")}
        error={problems["applies_to.tours"] ?? fieldError("applies_to.tours")}
        hint="One CIS tour id per line, for content that belongs to specific tours.">
        <textarea id={fid("tours")} value={form.tours_text} readOnly={!editable} rows={2}
          onChange={(e) => edit({ tours_text: e.target.value })}
          style={{ ...fieldStyle, fontFamily: mono, fontSize: 12.5, resize: "vertical" }} />
      </Field>

      {refusal && !refusal.field && (
        <InlineError message={refusal.message}
          action={refusal.code === "stale" ? (
            <Button size="sm" variant="secondary" onClick={onReload}><RefreshCw size={12} /> Reload</Button>
          ) : undefined} />
      )}
      {refusal?.field && refusal.field !== "title" && refusal.field !== "body_markdown"
        && !refusal.field.startsWith("applies_to") && <InlineError message={refusal.message} />}

      {(canWrite || canAdmin) && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          {editable && (
            <Button variant="primary" size="sm" onClick={() => void save()} disabled={!canSave}>
              {pending === "save" ? "Saving…" : draft ? `Save draft v${draft.version}` : `Save as draft v${nextVersion}`}
            </Button>
          )}
          {editable && dirty && (
            <Button variant="ghost" size="sm" onClick={() => { setForm(base); onDirty(false); setRefusal(null); }}
              disabled={!!pending}>
              Undo changes
            </Button>
          )}
          {editable && draft && item.published_version != null && (
            <Button variant="ghost" size="sm" onClick={() => setConfirm("discard")} disabled={!!pending}>
              Discard draft
            </Button>
          )}
          {canAdmin && !retired && draft && (
            <Button variant="secondary" size="sm" onClick={() => setConfirm("publish")} disabled={!canPublish}
              title={dirty ? "Save the draft first" : undefined}>
              Publish v{draft.version}
            </Button>
          )}
          {canAdmin && !retired && (
            <Button variant="danger" size="sm" onClick={() => setConfirm("retire")} disabled={!!pending}>Retire</Button>
          )}
          {canAdmin && retired && (
            <Button variant="secondary" size="sm" onClick={() => setConfirm("restore")} disabled={!!pending}>Restore</Button>
          )}
          {canAdmin && !retired && draft && dirty && <span style={noteStyle}>Save the draft before publishing.</span>}
        </div>
      )}

      <History versions={item.versions} />

      <ConfirmModal open={confirm === "discard"} onClose={() => setConfirm(null)} onConfirm={() => void discard()}
        busy={pending === "discard"} destructive title={`Discard draft v${draft?.version ?? ""}?`}
        confirmLabel="Discard draft"
        body={`The ${locale} draft is deleted. Published version ${item.published_version ?? ""} stays as it is.`} />
      <ConfirmModal open={confirm === "publish"} onClose={() => setConfirm(null)} onConfirm={() => void publish()}
        busy={pending === "publish"} title={`Publish ${locale} v${draft?.version ?? ""}?`} confirmLabel="Publish"
        body={item.published_version != null
          ? `Agencies' CRMs start receiving v${draft?.version ?? ""}. Version ${item.published_version} stays fetchable for the agencies that still use it. A published version can never be edited; changes need a new draft.`
          : "Agencies' CRMs start receiving this item. A published version can never be edited; changes need a new draft."} />
      <ConfirmModal open={confirm === "retire"} onClose={() => setConfirm(null)} onConfirm={() => void retire()}
        busy={pending === "retire"} destructive title={`Retire the ${locale} item?`} confirmLabel="Retire"
        body="The item disappears from the CRM list. Agencies that already use a published version can still fetch it. You can restore it later." />
      <ConfirmModal open={confirm === "restore"} onClose={() => setConfirm(null)} onConfirm={() => void restore()}
        busy={pending === "restore"} title={`Restore the ${locale} item?`} confirmLabel="Restore"
        body="The item shows in the CRM list again with its newest published version." />
    </section>
  );
}

// ── version history ─────────────────────────────────────────────────────────────────────────────────────────────

function History({ versions }: { versions: AaVersion[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const published = versions.filter((v) => v.status === "published");
  if (!published.length) return <div style={noteStyle}>Never published: agencies see nothing yet.</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>Published versions</div>
      {published.map((v) => {
        const isOpen = open === v.version;
        return (
          <div key={v.version} style={{ border: `1px solid ${K.line}`, borderRadius: RADIUS.md, minWidth: 0 }}>
            <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : v.version)}
              style={{
                display: "flex", width: "100%", gap: 8, alignItems: "center", padding: "6px 8px", border: "none",
                background: "transparent", cursor: "pointer", fontFamily: sans, fontSize: 12.5, color: K.ink,
                textAlign: "left", flexWrap: "wrap",
              }}>
              {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              <strong>v{v.version}</strong>
              <span style={{ color: K.muted }}>{formatDateTime(v.published_at)}</span>
              <span style={{ color: K.muted, overflowWrap: "anywhere" }}>{v.published_by}</span>
            </button>
            {isOpen && (
              <div style={{ padding: "0 10px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{v.title}</div>
                <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12.5, lineHeight: 1.5, color: K.body,
                  maxHeight: 280, overflowY: "auto" }}>
                  {v.body_markdown}
                </div>
                <div style={{ ...noteStyle, fontFamily: mono, overflowWrap: "anywhere" }}>
                  {(v.applies_to.country_codes ?? []).map(countryLabel).join(", ") || "All countries"}
                  {" · "}{v.content_hash}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── the other locale, not written yet ───────────────────────────────────────────────────────────────────────────

function MissingLocale({ locale, sibling, canWrite, onCreated }: {
  locale: AaLocale; sibling: AaItem; canWrite: boolean; onCreated: (item: AaItem) => void;
}) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = LOCALE_LABEL[locale];

  const create = async () => {
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    // The new copy starts with the sibling's scope (countries, tours): the same pack in another language.
    const r = await aaCall<AaItem>("POST", "", {
      kind: sibling.kind, locale, group_key: sibling.group_key, title: title.trim(), body_markdown: "",
      applies_to: baselineVersion(sibling)?.applies_to ?? {},
    });
    setBusy(false);
    if (!r.ok) {
      setError(r.message);
      return;
    }
    toast.success(`${locale.toUpperCase()}: draft v1 created.`);
    onCreated(r.body);
  };

  return (
    <section data-testid={`aa-content-panel-${locale}`} style={{
      border: `1px dashed ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px", display: "flex",
      flexDirection: "column", gap: 10, minWidth: 0,
    }}>
      <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: K.ink, fontFamily: sans }}>{label}</h3>
      <div style={noteStyle}>No {label} copy yet. Agencies that work in {label} do not get this item.</div>
      {canWrite && (
        <form onSubmit={(e) => { e.preventDefault(); void create(); }}
          style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Field label={`${label} title`} htmlFor={`aa-missing-${locale}`}>
            <input id={`aa-missing-${locale}`} type="text" autoComplete="off" maxLength={300} value={title}
              onChange={(e) => setTitle(e.target.value)} style={fieldStyle} />
          </Field>
          <div>
            <Button type="submit" variant="secondary" size="sm" disabled={!title.trim() || busy}>
              {busy ? "Creating…" : `Create the ${label} draft`}
            </Button>
          </div>
          {error && <InlineError message={error} />}
        </form>
      )}
    </section>
  );
}

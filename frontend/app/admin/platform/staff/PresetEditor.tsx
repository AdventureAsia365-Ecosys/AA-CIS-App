"use client";
// app/admin/platform/staff/PresetEditor.tsx — AA-1155 (spec task T14, root spec R5.7): the Staff page's people-role
// preset editor. The presets are rows of shared.staff_role_preset (CIS 465); an owner on a company sign-in creates,
// edits, archives or restores one here (POST / PATCH /api/admin/platform/presets, api/routers/admin_platform_roles.py:
// owners only, every role code checked, audited before and after); everyone else on this page reads them. The account
// panel (PeoplePresets.tsx) offers the active ones. The guards stay in the backend code: a preset never sets the owner
// flag (no field here can), the self-demote and last-owner checks, and an additive-only preset never replaces a held
// role. Kit components only.

import { useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import {
  Badge, Button, Checkbox, Field, Input, K, Modal, RADIUS, Section, Select, Textarea, apiSend, mono, useT,
} from "../../../_kit";
import { ASSIGNABLE_STAFF_ROLES } from "@/lib/rbac";
import { customRoleOptions } from "@/lib/rolesMatrix";
import {
  BOOKING_ROLES, CRM_ROLES, PRESETS_PATH, type BookingMode, type CrmMode, type StaffPreset,
} from "@/lib/staffPresets";
import {
  blankPreset, presetCreateBody, presetEditBody, presetFormProblem, type PresetForm,
} from "@/lib/staffRoleEditor";
import { PRESET_EDITOR } from "./editors.messages";
import { CRM_ROLE } from "./messages";
import { PRESETS_KEY, usePresets } from "./PeoplePresets";
import { OutcomeBanner, useRolesCatalogue, useStaffWords, type Outcome } from "./StaffManage";

type Editing = null | { kind: "new" } | { kind: "edit"; preset: StaffPreset };

export function PresetEditor() {
  const t = useT(PRESET_EDITOR);
  const q = usePresets();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Editing>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const data = q.data;
  const canEdit = !!data?.can_edit && !!data?.available;
  const presets = [...(data?.presets ?? [])].sort((a, b) =>
    Number(a.archived) - Number(b.archived) || a.sort_order - b.sort_order || a.label.localeCompare(b.label));

  const run = async (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => {
    setBusy(true);
    let o: Outcome;
    try {
      await apiSend(path, { method, body });
      o = { kind: "ok", message: ok };
    } catch (e) {
      o = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
    setBusy(false);
    if (o.kind === "ok") { setEditing(null); await qc.invalidateQueries({ queryKey: PRESETS_KEY }); }
    setOutcome(o);
  };
  const path = (p: StaffPreset) => `${PRESETS_PATH}/${encodeURIComponent(p.code)}`;
  const nextSort = Math.min(999, Math.max(0, ...presets.map((p) => p.sort_order)) + 10);

  return (
    <Section testId="staff-preset-editor" title={t("title")}
      description={<>{t("description")}{data && !data.can_edit ? ` ${t("readOnly")}` : ""}</>}
      actions={canEdit && (
        <Button size="sm" variant="secondary" testId="preset-editor-new" disabled={busy}
          onClick={() => { setOutcome(null); setEditing({ kind: "new" }); }}>
          <Plus size={13} /> {t("newPreset")}
        </Button>
      )}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {outcome && <OutcomeBanner outcome={outcome} onRetried={setOutcome} />}
      {q.isLoading && <span style={{ fontSize: 13, color: K.muted }}>{t("loading")}</span>}
      {q.error && <OutcomeBanner outcome={{ kind: "error", message: (q.error as Error).message }} onRetried={() => {}} />}
      {data && !data.available && <span style={{ fontSize: 13, color: K.muted }}>{t("unavailable")}</span>}
      {data?.available && presets.length === 0 && <span style={{ fontSize: 13, color: K.muted }}>{t("none")}</span>}
      {presets.map((p) => (
        <PresetRow key={p.code} preset={p} canEdit={canEdit} busy={busy}
          onEdit={() => { setOutcome(null); setEditing({ kind: "edit", preset: p }); }}
          onArchive={(archived) => void run(path(p), "PATCH", { archived },
            archived ? t("archivedDone", p.label) : t("restoredDone", p.label))} />
      ))}
      {editing?.kind === "new" && (
        <PresetFormModal initial={blankPreset(nextSort)} isNew busy={busy} onClose={() => setEditing(null)}
          onSubmit={(f) => void run(PRESETS_PATH, "POST", presetCreateBody(f), t("created", f.label.trim()))} />
      )}
      {editing?.kind === "edit" && (
        <PresetFormModal initial={editing.preset} isNew={false} busy={busy} onClose={() => setEditing(null)}
          onSubmit={(f) => {
            const body = presetEditBody(editing.preset, f);
            if (Object.keys(body).length === 0) { setEditing(null); return; }
            void run(path(editing.preset), "PATCH", body, t("saved", f.label.trim()));
          }} />
      )}
    </Section>
  );
}

/** "Sets: CIS Content Lead, Booking Sales, CRM Support" in words. */
function usePresetSummary() {
  const t = useT(PRESET_EDITOR);
  const crm = useT(CRM_ROLE);
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  return (p: StaffPreset) => {
    const parts: string[] = [];
    if (p.cis_role) parts.push(t("setsCis", roleLabel(p.cis_role)));
    if (p.booking_mode === "set" && p.booking_role) parts.push(t("setsBooking", bookingRoleLabel(p.booking_role)));
    if (p.booking_mode === "clear") parts.push(t("setsBookingClear"));
    const names = CRM_ROLES.filter((r) => p.crm_roles.includes(r)).map((r) => crm(r)).join(", ");
    if (p.crm_mode === "replace") parts.push(names ? t("setsCrmReplace", names) : t("noCrm"));
    if (p.crm_mode === "add") parts.push(t("setsCrmAdd", names));
    return `${t("sets")} ${parts.join(", ")}`;
  };
}

function PresetRow({ preset, canEdit, busy, onEdit, onArchive }: {
  preset: StaffPreset;
  canEdit: boolean;
  busy: boolean;
  onEdit: () => void;
  onArchive: (archived: boolean) => void;
}) {
  const t = useT(PRESET_EDITOR);
  const summary = usePresetSummary();
  return (
    <div data-testid={`preset-editor-row-${preset.code}`} style={{
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "10px 12px", background: K.line2,
      borderRadius: RADIUS.sm, opacity: preset.archived ? 0.75 : 1,
    }}>
      <span style={{ flex: "1 1 260px", minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{preset.label}</span>
          {preset.additive_only && <Badge tone="info">{t("additive")}</Badge>}
          {preset.archived && <Badge tone="neutral">{t("archived")}</Badge>}
        </span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45, overflowWrap: "anywhere" }}>
          <span style={{ fontFamily: mono }}>{preset.code}</span> · {summary(preset)}
        </span>
        {preset.description && (
          <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>{preset.description}</span>
        )}
      </span>
      {canEdit && (
        <span style={{ display: "flex", gap: 6 }}>
          {!preset.archived && (
            <Button size="sm" variant="secondary" testId={`preset-editor-edit-${preset.code}`} onClick={onEdit}
              disabled={busy}>
              {t("edit")}
            </Button>
          )}
          <Button size="sm" variant="ghost" testId={`preset-editor-${preset.archived ? "restore" : "archive"}-${preset.code}`}
            onClick={() => onArchive(!preset.archived)} disabled={busy}>
            {preset.archived ? t("restore") : t("archive")}
          </Button>
        </span>
      )}
    </div>
  );
}

const PROBLEM_KEY = {
  code: "problemCode", label: "problemLabel", bookingRole: "problemBookingRole", crmRoles: "problemCrmRoles",
  additive: "problemAdditive", empty: "problemEmpty",
} as const;

/** Create or edit one preset: every field of shared.staff_role_preset an owner may set (never an owner flag). */
function PresetFormModal({ initial, isNew, busy, onClose, onSubmit }: {
  initial: PresetForm;
  isNew: boolean;
  busy: boolean;
  onClose: () => void;
  onSubmit: (f: PresetForm) => void;
}) {
  const t = useT(PRESET_EDITOR);
  const crm = useT(CRM_ROLE);
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const catalogue = useRolesCatalogue().data;
  const [f, setF] = useState<PresetForm>(initial);
  const set = <K extends keyof PresetForm>(k: K, v: PresetForm[K]) => setF((cur) => ({ ...cur, [k]: v }));
  const problem = presetFormProblem(f, isNew);
  const cisOptions = [...ASSIGNABLE_STAFF_ROLES.map((r) => ({ value: r, label: roleLabel(r) })),
    ...customRoleOptions(catalogue, "cis").map((c) => ({ value: c.code, label: c.label }))];
  const bookingOptions = [...BOOKING_ROLES.map((r) => ({ value: r, label: bookingRoleLabel(r) })),
    ...customRoleOptions(catalogue, "booking").map((c) => ({ value: c.code, label: c.label }))];
  const toggle = (k: "crm_roles" | "cis_keep_roles" | "booking_keep_roles", v: string, on: boolean) =>
    set(k, on ? [...new Set([...f[k], v])] : f[k].filter((x) => x !== v));

  return (
    <Modal open onClose={onClose} width={620} pinFooter testId="preset-editor-form"
      title={isNew ? t("createTitle") : t("editTitle", initial.label)}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" testId="preset-editor-submit" disabled={problem !== null || busy}
          title={problem ? t(PROBLEM_KEY[problem]) : undefined} onClick={() => onSubmit(f)}>
          {busy ? t("saving") : isNew ? t("create") : t("save")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {isNew && (
          <Field label={t("code")} hint={t("codeHint")} required>
            <Input data-testid="preset-editor-code" value={f.code} maxLength={48} autoComplete="off"
              onChange={(e) => set("code", e.target.value.trim().toLowerCase())} />
          </Field>
        )}
        <Field label={t("label")} required>
          <Input data-testid="preset-editor-label" value={f.label} maxLength={80} autoComplete="off"
            onChange={(e) => set("label", e.target.value)} />
        </Field>
        <Field label={t("descriptionField")} hint={t("descriptionHint")}>
          <Textarea data-testid="preset-editor-description" value={f.description} maxLength={300} rows={2}
            onChange={(e) => set("description", e.target.value)} />
        </Field>
        <Field label={t("cisRole")}>
          <Select data-testid="preset-editor-cis" value={f.cis_role ?? ""}
            options={[{ value: "", label: t("keep") }, ...cisOptions]}
            onChange={(e) => set("cis_role", e.target.value || null)} />
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14 }}>
          <Field label={t("bookingMode")}>
            <Select data-testid="preset-editor-booking-mode" value={f.booking_mode}
              options={[{ value: "keep", label: t("bookingKeep") }, { value: "set", label: t("bookingSet") },
                { value: "clear", label: t("bookingClear") }]}
              onChange={(e) => set("booking_mode", e.target.value as BookingMode)} />
          </Field>
          {f.booking_mode === "set" && (
            <Field label={t("bookingRole")}>
              <Select data-testid="preset-editor-booking-role" value={f.booking_role ?? ""}
                placeholder="—" options={bookingOptions}
                onChange={(e) => set("booking_role", e.target.value || null)} />
            </Field>
          )}
        </div>
        <Field as="div" label={t("crmMode")}>
          <Select data-testid="preset-editor-crm-mode" value={f.crm_mode}
            options={[{ value: "keep", label: t("crmKeep") }, { value: "replace", label: t("crmReplace") },
              { value: "add", label: t("crmAdd") }]}
            onChange={(e) => set("crm_mode", e.target.value as CrmMode)} />
          {f.crm_mode !== "keep" && (
            <div style={{ display: "flex", gap: "4px 16px", flexWrap: "wrap" }}>
              {CRM_ROLES.map((r) => (
                <Checkbox key={r} data-testid={`preset-editor-crm-${r}`} label={crm(r)} checked={f.crm_roles.includes(r)}
                  onChange={(e) => toggle("crm_roles", r, e.target.checked)} style={{ fontSize: 13 }} />
              ))}
            </div>
          )}
        </Field>
        <Field as="div" label={t("additiveOnly")} hint={t("additiveHint")}>
          <Checkbox data-testid="preset-editor-additive" label={t("additiveOnly")} checked={f.additive_only}
            onChange={(e) => set("additive_only", e.target.checked)} style={{ fontSize: 13 }} />
        </Field>
        {f.additive_only && (
          <>
            <Field as="div" label={t("cisKeep")}>
              <div style={{ display: "flex", gap: "4px 16px", flexWrap: "wrap" }}>
                {cisOptions.map((o) => (
                  <Checkbox key={o.value} label={o.label} checked={f.cis_keep_roles.includes(o.value)}
                    onChange={(e) => toggle("cis_keep_roles", o.value, e.target.checked)} style={{ fontSize: 13 }} />
                ))}
              </div>
            </Field>
            <Field as="div" label={t("bookingKeep2")}>
              <div style={{ display: "flex", gap: "4px 16px", flexWrap: "wrap" }}>
                {bookingOptions.map((o) => (
                  <Checkbox key={o.value} label={o.label} checked={f.booking_keep_roles.includes(o.value)}
                    onChange={(e) => toggle("booking_keep_roles", o.value, e.target.checked)} style={{ fontSize: 13 }} />
                ))}
              </div>
            </Field>
          </>
        )}
        <Field label={t("sortOrder")} hint={t("sortHint")}>
          <Input data-testid="preset-editor-sort" type="number" min={0} max={999} value={String(f.sort_order)}
            onChange={(e) => set("sort_order", Math.max(0, Math.min(999, Math.trunc(Number(e.target.value) || 0))))} />
        </Field>
        {problem && <span style={{ fontSize: 12, color: K.danger }}>{t(PROBLEM_KEY[problem])}</span>}
      </div>
    </Modal>
  );
}

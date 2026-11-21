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
import { Badge, Button, Checkbox, Field, Input, K, Modal, Section, Select, Textarea, mono, useT } from "../../../_kit";
import { ASSIGNABLE_STAFF_ROLES } from "@/lib/rbac";
import { customRoleOptions } from "@/lib/rolesMatrix";
import {
  BOOKING_ROLES, CRM_ROLES, PRESETS_PATH, type BookingMode, type CrmMode, type PresetList, type StaffPreset,
} from "@/lib/staffPresets";
import {
  blankPreset, presetCreateBody, presetEditBody, presetFormProblem, type PresetForm,
} from "@/lib/staffRoleEditor";
import { PRESET_EDITOR } from "./editors.messages";
import { CHOICE_ROW, EditorRow, EditorStatus, useEditorWrite } from "./EditorParts";
import { CRM_ROLE } from "./messages";
import { PRESETS_KEY, usePresets } from "./PeoplePresets";
import { useRolesCatalogue, useStaffWords } from "./StaffManage";

type Editing = null | { kind: "new" } | { kind: "edit"; preset: StaffPreset };
type Option = { value: string; label: string };

const presetPath = (p: StaffPreset) => `${PRESETS_PATH}/${encodeURIComponent(p.code)}`;

/** Who may edit (an owner, once the table is there), whether the viewer only reads, whether the table is missing. */
function editorFlags(data: PresetList | undefined): { canEdit: boolean; readOnly: boolean; unavailable: boolean } {
  if (!data) return { canEdit: false, readOnly: false, unavailable: false };
  return { canEdit: data.can_edit && data.available, readOnly: !data.can_edit, unavailable: !data.available };
}

/** Archived last, then by sort order and name. */
function editorOrder(presets: readonly StaffPreset[]): StaffPreset[] {
  return [...presets].sort((a, b) =>
    Number(a.archived) - Number(b.archived) || a.sort_order - b.sort_order || a.label.localeCompare(b.label));
}

export function PresetEditor() {
  const t = useT(PRESET_EDITOR);
  const q = usePresets();
  const qc = useQueryClient();
  const write = useEditorWrite(() => qc.invalidateQueries({ queryKey: PRESETS_KEY }));
  const [editing, setEditing] = useState<Editing>(null);
  const data = q.data;
  const { canEdit, readOnly, unavailable } = editorFlags(data);
  const presets = editorOrder(data?.presets ?? []);
  const open = (next: Editing) => { write.setOutcome(null); setEditing(next); };
  const run = async (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => {
    if (await write.run(path, method, body, ok)) setEditing(null);
  };
  return (
    <Section testId="staff-preset-editor" title={t("title")}
      description={readOnly ? `${t("description")} ${t("readOnly")}` : t("description")}
      actions={canEdit && (
        <Button size="sm" variant="secondary" testId="preset-editor-new" disabled={write.busy}
          onClick={() => open({ kind: "new" })}>
          <Plus size={13} /> {t("newPreset")}
        </Button>
      )}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <EditorStatus outcome={write.outcome} onOutcome={write.setOutcome} loading={q.isLoading} error={q.error}
        unavailable={unavailable} empty={!!data && !unavailable && presets.length === 0}
        words={{ loading: t("loading"), unavailable: t("unavailable"), none: t("none") }} />
      {presets.map((p) => (
        <PresetRow key={p.code} preset={p} canEdit={canEdit} busy={write.busy}
          onEdit={() => open({ kind: "edit", preset: p })}
          onArchive={(archived) => void run(presetPath(p), "PATCH", { archived },
            t(archived ? "archivedDone" : "restoredDone", p.label))} />
      ))}
      <PresetDialogs editing={editing} presets={presets} busy={write.busy} onClose={() => setEditing(null)} run={run} />
    </Section>
  );
}

/** The create or edit form of the preset being edited (a new one is placed after the others). */
function PresetDialogs({ editing, presets, busy, onClose, run }: {
  editing: Editing;
  presets: StaffPreset[];
  busy: boolean;
  onClose: () => void;
  run: (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => Promise<void>;
}) {
  const t = useT(PRESET_EDITOR);
  if (editing === null) return null;
  if (editing.kind === "new") {
    const nextSort = Math.min(999, Math.max(0, ...presets.map((p) => p.sort_order)) + 10);
    return (
      <PresetFormModal initial={blankPreset(nextSort)} isNew busy={busy} onClose={onClose}
        onSubmit={(f) => void run(PRESETS_PATH, "POST", presetCreateBody(f), t("created", f.label.trim()))} />
    );
  }
  const before = editing.preset;
  const save = (f: PresetForm) => {
    const body = presetEditBody(before, f);
    if (Object.keys(body).length === 0) onClose();
    else void run(presetPath(before), "PATCH", body, t("saved", f.label.trim()));
  };
  return <PresetFormModal initial={before} isNew={false} busy={busy} onClose={onClose} onSubmit={save} />;
}

/** "Sets: CIS Content Lead, Booking Sales, CRM Support" in words. */
function usePresetSummary() {
  const t = useT(PRESET_EDITOR);
  const crm = useT(CRM_ROLE);
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const crmPart = (p: StaffPreset) => {
    const names = CRM_ROLES.filter((r) => p.crm_roles.includes(r)).map((r) => crm(r)).join(", ");
    if (p.crm_mode === "add") return t("setsCrmAdd", names);
    return names ? t("setsCrmReplace", names) : t("noCrm");
  };
  return (p: StaffPreset) => {
    const parts = [
      p.cis_role ? t("setsCis", roleLabel(p.cis_role)) : "",
      p.booking_mode === "set" ? t("setsBooking", bookingRoleLabel(p.booking_role)) : "",
      p.booking_mode === "clear" ? t("setsBookingClear") : "",
      p.crm_mode === "keep" ? "" : crmPart(p),
    ];
    return `${t("sets")} ${parts.filter(Boolean).join(", ")}`;
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
  const action = preset.archived ? "restore" : "archive";
  return (
    <EditorRow testId={`preset-editor-row-${preset.code}`} dim={preset.archived}
      title={<>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{preset.label}</span>
        {preset.additive_only && <Badge tone="info">{t("additive")}</Badge>}
        {preset.archived && <Badge tone="neutral">{t("archived")}</Badge>}
      </>}
      details={<>
        <span style={{ fontFamily: mono }}>{preset.code}</span> · {summary(preset)}
        {preset.description && <><br />{preset.description}</>}
      </>}
      actions={canEdit && <>
        {!preset.archived && (
          <Button size="sm" variant="secondary" testId={`preset-editor-edit-${preset.code}`} onClick={onEdit}
            disabled={busy}>
            {t("edit")}
          </Button>
        )}
        <Button size="sm" variant="ghost" testId={`preset-editor-${action}-${preset.code}`}
          onClick={() => onArchive(!preset.archived)} disabled={busy}>
          {t(action)}
        </Button>
      </>} />
  );
}

const PROBLEM_KEY = {
  code: "problemCode", label: "problemLabel", bookingRole: "problemBookingRole", crmRoles: "problemCrmRoles",
  additive: "problemAdditive", empty: "problemEmpty",
} as const;

/** The CIS and Booking role choices of a preset: the assignable built-in roles, then the active custom ones. */
function useRoleOptions(): { cis: Option[]; booking: Option[] } {
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const catalogue = useRolesCatalogue().data;
  const custom = (app: "cis" | "booking") => customRoleOptions(catalogue, app).map((c) => ({ value: c.code, label: c.label }));
  return {
    cis: [...ASSIGNABLE_STAFF_ROLES.map((r) => ({ value: r, label: roleLabel(r) })), ...custom("cis")],
    booking: [...BOOKING_ROLES.map((r) => ({ value: r, label: bookingRoleLabel(r) })), ...custom("booking")],
  };
}

type SetField = <F extends keyof PresetForm>(k: F, v: PresetForm[F]) => void;

/** Create or edit one preset: every field of shared.staff_role_preset an owner may set (never an owner flag). */
function PresetFormModal({ initial, isNew, busy, onClose, onSubmit }: {
  initial: PresetForm;
  isNew: boolean;
  busy: boolean;
  onClose: () => void;
  onSubmit: (f: PresetForm) => void;
}) {
  const t = useT(PRESET_EDITOR);
  const options = useRoleOptions();
  const [f, setF] = useState<PresetForm>(initial);
  const set: SetField = (k, v) => setF((cur) => ({ ...cur, [k]: v }));
  const problem = presetFormProblem(f, isNew);
  const why = problem ? t(PROBLEM_KEY[problem]) : undefined;
  return (
    <Modal open onClose={onClose} width={620} pinFooter testId="preset-editor-form"
      title={isNew ? t("createTitle") : t("editTitle", initial.label)}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" testId="preset-editor-submit" disabled={problem !== null || busy} title={why}
          onClick={() => onSubmit(f)}>
          {busy ? t("saving") : t(isNew ? "create" : "save")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <PresetTextFields f={f} set={set} isNew={isNew} />
        <PresetRoleFields f={f} set={set} options={options} />
        <PresetAdditiveFields f={f} set={set} options={options} />
        <Field label={t("sortOrder")} hint={t("sortHint")}>
          <Input data-testid="preset-editor-sort" type="number" min={0} max={999} value={String(f.sort_order)}
            onChange={(e) => set("sort_order", Math.max(0, Math.min(999, Math.trunc(Number(e.target.value) || 0))))} />
        </Field>
        {why && <span style={{ fontSize: 12, color: K.danger }}>{why}</span>}
      </div>
    </Modal>
  );
}

/** Code (new presets only), name and description. */
function PresetTextFields({ f, set, isNew }: { f: PresetForm; set: SetField; isNew: boolean }) {
  const t = useT(PRESET_EDITOR);
  return (
    <>
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
    </>
  );
}

/** What the preset sets: the CIS role (or kept), the Booking mode and role, the CRM mode and roles. */
function PresetRoleFields({ f, set, options }: { f: PresetForm; set: SetField; options: { cis: Option[]; booking: Option[] } }) {
  const t = useT(PRESET_EDITOR);
  const crm = useT(CRM_ROLE);
  const toggleCrm = (r: string, on: boolean) => set("crm_roles", on ? [...new Set([...f.crm_roles, r])] : f.crm_roles.filter((x) => x !== r));
  return (
    <>
      <Field label={t("cisRole")}>
        <Select data-testid="preset-editor-cis" value={f.cis_role ?? ""} options={[{ value: "", label: t("keep") }, ...options.cis]}
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
            <Select data-testid="preset-editor-booking-role" value={f.booking_role ?? ""} placeholder="—"
              options={options.booking} onChange={(e) => set("booking_role", e.target.value || null)} />
          </Field>
        )}
      </div>
      <Field as="div" label={t("crmMode")}>
        <Select data-testid="preset-editor-crm-mode" value={f.crm_mode}
          options={[{ value: "keep", label: t("crmKeep") }, { value: "replace", label: t("crmReplace") },
            { value: "add", label: t("crmAdd") }]}
          onChange={(e) => set("crm_mode", e.target.value as CrmMode)} />
        {f.crm_mode !== "keep" && (
          <div style={CHOICE_ROW}>
            {CRM_ROLES.map((r) => (
              <Checkbox key={r} data-testid={`preset-editor-crm-${r}`} label={crm(r)} checked={f.crm_roles.includes(r)}
                onChange={(e) => toggleCrm(r, e.target.checked)} style={{ fontSize: 13 }} />
            ))}
          </div>
        )}
      </Field>
    </>
  );
}

/** Additive only, and the CIS / Booking roles such a preset keeps as they are. */
function PresetAdditiveFields({ f, set, options }: {
  f: PresetForm;
  set: SetField;
  options: { cis: Option[]; booking: Option[] };
}) {
  const t = useT(PRESET_EDITOR);
  return (
    <>
      <Field as="div" label={t("additiveOnly")} hint={t("additiveHint")}>
        <Checkbox data-testid="preset-editor-additive" label={t("additiveOnly")} checked={f.additive_only}
          onChange={(e) => set("additive_only", e.target.checked)} style={{ fontSize: 13 }} />
      </Field>
      {f.additive_only && (
        <>
          <KeepChecks label={t("cisKeep")} options={options.cis} value={f.cis_keep_roles}
            onChange={(v) => set("cis_keep_roles", v)} />
          <KeepChecks label={t("bookingKeep2")} options={options.booking} value={f.booking_keep_roles}
            onChange={(v) => set("booking_keep_roles", v)} />
        </>
      )}
    </>
  );
}

function KeepChecks({ label, options, value, onChange }: {
  label: string;
  options: Option[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  return (
    <Field as="div" label={label}>
      <div style={CHOICE_ROW}>
        {options.map((o) => (
          <Checkbox key={o.value} label={o.label} checked={value.includes(o.value)} style={{ fontSize: 13 }}
            onChange={(e) => onChange(e.target.checked ? [...new Set([...value, o.value])] : value.filter((x) => x !== o.value))} />
        ))}
      </div>
    </Field>
  );
}

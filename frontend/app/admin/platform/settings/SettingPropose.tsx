"use client";
// app/admin/platform/settings/SettingPropose.tsx — the change dialog of Platform › Settings (moved out of
// SettingsRegistry.tsx in the AA-1096 S280 UX fix round).
//
// The guide, the scope (platform or one tenant), the value in force at that scope, the new value (by value type, the
// unit in the label), the reason, the evidence link, "Effective from" (never in the past: the input's min is now and
// the form and the API refuse an older time) and "approve it now" when allowed: always while two-person approval is
// off, and for a single-approval key also while it is on (the server checks the second person for maker_checker keys
// only). The form says before sending when the scope already has an open proposal (one per key and scope) and when an
// approved change is scheduled for the scope (approving this one cancels it: the newest decision wins). The form's
// problems are worded in the page's language (useSettingWords).

import { useState } from "react";
import { Button, Checkbox, Field, Input, K, Modal, Select, Textarea, mono, useT, useToast } from "../../../_kit";
import {
  PLATFORM_SCOPE, boundsText, canApplyAtOnce, draftProblem, draftText, effectiveInPast, localMinute, openProposal,
  proposalBody, proposalPath, scheduledOf, scopeReady, scopeTarget, scopeTenant, type ProposalDraft, type ScopeChoice,
  type SettingItem, type ValueOf,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";
import { DraftNow, GuideDetails } from "./SettingGuide";
import { ScopeCurrent, ScopeFields } from "./SettingScope";
import { post } from "./settingsApi";
import { useSettingWords } from "./useSettingWords";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

/** The value input of the change dialog, by value type. */
function ValueInput({ item, text, onText }: { item: SettingItem; text: string; onText: (text: string) => void }) {
  const t = useT(SETTINGS);
  const { words } = useSettingWords();
  const label = item.unit && item.value_type !== "bool" && item.value_type !== "enum"
    ? `${t("newValue")} (${words.unit(item.unit)})` : t("newValue");
  if (item.value_type === "bool") {
    return (
      <Field label={label}>
        <Select value={text} onChange={(e) => onText(e.target.value)}
          options={[{ value: "on", label: t("on") }, { value: "off", label: t("off") }]} />
      </Field>
    );
  }
  if (item.value_type === "enum") {
    return (
      <Field label={label}>
        <Select value={text} onChange={(e) => onText(e.target.value)}
          options={(item.allowed_values ?? []).map((v) => ({ value: String(v), label: String(v) }))} />
      </Field>
    );
  }
  if (item.value_type === "json" || item.value_type === "list") {
    return (
      <Field label={label} hint={t("jsonHint")}>
        <Textarea rows={7} value={text} onChange={(e) => onText(e.target.value)}
          style={{ fontFamily: mono, fontSize: 12 }} />
      </Field>
    );
  }
  return (
    <Field label={label} hint={boundsText(item, words) || undefined}>
      <Input inputMode="decimal" value={text} onChange={(e) => onText(e.target.value)}
        data-testid="settings-propose-value" />
    </Field>
  );
}

/** What the form warns about before sending: an open proposal of the scope (sending is refused), a scheduled change
 *  of the scope (approving cancels it). */
function ScopeNotes({ item, tenant, effectiveFrom }: { item: SettingItem; tenant: string | null; effectiveFrom: string }) {
  const t = useT(SETTINGS);
  const pending = openProposal(item, tenant);
  const scheduled = scheduledOf(item, tenant).length > 0;
  const later = !!effectiveFrom && !effectiveInPast(effectiveFrom);
  return (
    <>
      {pending && <div role="alert" style={{ ...small, color: K.danger }} data-testid="settings-propose-pending">
        {t("pendingScope")}</div>}
      {scheduled && <div style={{ ...small, color: K.body }} data-testid="settings-propose-scheduled">
        {later ? t("scheduledWarnLater") : t("scheduledWarnNow")}</div>}
    </>
  );
}

function ApplyChoice({ item, twoPerson, apply, onApply }: {
  item: SettingItem; twoPerson: boolean; apply: boolean; onApply: (apply: boolean) => void;
}) {
  const t = useT(SETTINGS);
  if (!canApplyAtOnce(item, twoPerson)) return <div style={small}>{t("applyTwoPerson")}</div>;
  return (
    <Checkbox checked={apply} onChange={(e) => onApply(e.target.checked)}
      label={item.approval === "maker_checker" ? t("applySelf") : t("applyNow")} />
  );
}

/** The change form's state and what follows from it: the problem, the scope's target, whether it can be sent. */
function useProposeForm(item: SettingItem, twoPerson: boolean, canWrite: boolean) {
  const [draft, setDraft] = useState<ProposalDraft>({
    text: draftText(item, item.value), reason: "", evidence: "", effectiveFrom: "",
    apply: canApplyAtOnce(item, twoPerson),
  });
  const [scope, setScope] = useState<ScopeChoice>(PLATFORM_SCOPE);
  const [busy, setBusy] = useState(false);
  const problem = draftProblem(item, draft);
  const tenant = scopeTenant(scope);
  const target = scopeTarget(item, tenant);
  const blocked = !!problem || busy || !canWrite || !scopeReady(scope) || !!openProposal(item, tenant);
  const touched = !!draft.reason || !!draft.effectiveFrom || draft.text !== draftText(item, target.value);
  const set = (patch: Partial<ProposalDraft>) => setDraft((d) => ({ ...d, ...patch }));
  // AA-1096 (S280): the form starts from the value in force at the picked scope.
  const pickScope = (c: ScopeChoice) => {
    setScope(c);
    set({ text: draftText(item, scopeTarget(item, scopeTenant(c)).value) });
  };
  return { draft, set, scope, pickScope, busy, setBusy, problem, tenant, target, blocked, touched };
}

export function ProposeModal({ item, twoPerson, canWrite, valueOf, onClose, onDone }: {
  item: SettingItem; twoPerson: boolean; canWrite: boolean; valueOf: ValueOf; onClose: () => void; onDone: () => void;
}) {
  const t = useT(SETTINGS);
  const toast = useToast();
  const { problem: problemText } = useSettingWords();
  const { draft, set, scope, pickScope, busy, setBusy, problem, tenant, target, blocked, touched } =
    useProposeForm(item, twoPerson, canWrite);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (blocked) return;
    setBusy(true);
    setError(null);
    const out = await post(proposalPath(item.key), proposalBody(item, target, draft, tenant),
      { network: t("network"), failed: (s) => t("failed", s), readOnly: t("readOnly") });
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    toast.success(draft.apply ? t("applied", item.label) : t("proposed", item.label), 6000);
    onDone();
  };
  return (
    <Modal open onClose={onClose} title={t("proposeTitle", item.label)} width={560} pinFooter testId="settings-propose"
      footer={<>
        <Button variant="secondary" onClick={onClose}>{t("cancel")}</Button>
        <Button variant="primary" disabled={blocked} onClick={() => void submit()} testId="settings-propose-submit">
          {busy ? t("saving") : draft.apply ? t("proposeApply") : t("propose")}
        </Button>
      </>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 10, background: K.bg }}>
          <GuideDetails item={item} testId="settings-propose-guide" valueOf={valueOf} />
        </div>
        <ScopeFields item={item} value={scope} onChange={pickScope} />
        <ScopeCurrent item={item} scope={scope} />
        <ScopeNotes item={item} tenant={tenant} effectiveFrom={draft.effectiveFrom} />
        <ValueInput item={item} text={draft.text} onText={(text) => set({ text })} />
        <DraftNow item={item} text={draft.text} valueOf={valueOf} />
        <Field label={t("reason")} required hint={t("reasonHint")}>
          <Textarea rows={3} value={draft.reason} onChange={(e) => set({ reason: e.target.value })}
            data-testid="settings-propose-reason" />
        </Field>
        <Field label={t("evidence")} required={item.evidence_required}
          hint={item.evidence_required ? t("evidenceHint") : t("evidenceOptional")}>
          <Input value={draft.evidence} onChange={(e) => set({ evidence: e.target.value })} />
        </Field>
        <Field label={t("effectiveFrom")} hint={t("effectiveHint")}>
          <Input type="datetime-local" min={localMinute()} value={draft.effectiveFrom}
            onChange={(e) => set({ effectiveFrom: e.target.value })} data-testid="settings-propose-effective" />
        </Field>
        <ApplyChoice item={item} twoPerson={twoPerson} apply={draft.apply} onApply={(apply) => set({ apply })} />
        {problem && touched && <div role="alert" style={{ ...small, color: K.danger }}>{problemText(problem)}</div>}
        {error && <div role="alert" style={{ ...small, color: K.danger }}>{error}</div>}
      </div>
    </Modal>
  );
}

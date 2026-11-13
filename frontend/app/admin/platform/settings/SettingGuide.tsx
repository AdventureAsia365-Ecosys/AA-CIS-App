"use client";
// app/admin/platform/settings/SettingGuide.tsx — AA-1096 l23 (tech lead S279: "each setting needs an explanation or
// definition so I know when to change it"): the operator's guide of a setting on Platform › Settings.
//
// - SettingCell: the Setting column — plain label, the one-line "what it controls", the key (small, monospace), the
//   badges and an Explain toggle that opens the guide in place (in the flow of the cell, never a floating popover in
//   the scrolling table: lesson S239).
// - GuideDetails: what it controls / if you raise it / if you lower it (an on/off switch: turn on / off; a choice:
//   the stricter / looser choice) / when to change it / example (at the default), then the technical description as a
//   collapsed "Engineering note". The change dialog shows it above the inputs.
// - ActorName: the staff member's name or e-mail, the actor id in the tooltip.
// The guide comes from the API in English and Vietnamese (CIS 415, lib/settingsRegistry guideFor); kit only.

import { ChevronDown, ChevronRight, Info } from "lucide-react";
import { useState, type ReactNode } from "react";
import { K, LinkButton, mono, useLang, useT } from "../../../_kit";
import {
  actorLabel, directionWords, guideFor, type GuidePart, type SettingDefinition,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
type PartKey = "guideWhat" | "guideWhen" | "guideExample" | "guideRaise_number" | "guideLower_number" |
  "guideRaise_switch" | "guideLower_switch" | "guideRaise_choice" | "guideLower_choice";

type GuideItem = Pick<SettingDefinition, "key" | "label" | "description" | "value_type" | "guide">;

function partKey(part: GuidePart, item: GuideItem): PartKey {
  if (part === "raise") return `guideRaise_${directionWords(item.value_type)}`;
  if (part === "lower") return `guideLower_${directionWords(item.value_type)}`;
  return part === "what" ? "guideWhat" : part === "when" ? "guideWhen" : "guideExample";
}

/** The one-line "what it controls" under a label (nothing when the setting has no guide yet). */
export function GuideSummary({ item }: { item: GuideItem }) {
  const guide = guideFor(item, useLang());
  if (!guide) return null;
  return <span style={{ ...small, color: K.body }} data-testid={`settings-what-${item.key}`}>{guide.what}</span>;
}

function EngineeringNote({ text }: { text: string }) {
  const t = useT(SETTINGS);
  const [open, setOpen] = useState(false);
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <LinkButton tone="muted" size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown size={12} aria-hidden /> : <ChevronRight size={12} aria-hidden />}
        {open ? t("hideNote") : t("showNote")}
      </LinkButton>
      {open && (
        <div style={{ ...small, borderLeft: `2px solid ${K.line}`, paddingLeft: 8 }}>
          <strong>{t("engineeringNote")}:</strong> {text}
        </div>
      )}
    </div>
  );
}

/** The whole guide of one setting, then its engineering note (collapsed). */
export function GuideDetails({ item, testId }: { item: GuideItem; testId?: string }) {
  const t = useT(SETTINGS);
  const guide = guideFor(item, useLang());
  const parts: GuidePart[] = ["what", "raise", "lower", "when", "example"];
  return (
    <div data-testid={testId} style={{ display: "grid", gap: 8, minWidth: 0 }}>
      {guide ? (
        <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 6 }}>
          {parts.map((part) => (
            <div key={part} style={{ display: "grid", gap: 1 }}>
              <dt style={{ fontSize: 12, fontWeight: 600, color: K.ink }}>{t(partKey(part, item))}</dt>
              <dd style={{ ...small, color: K.body, margin: 0 }}>{guide[part]}</dd>
            </div>
          ))}
        </dl>
      ) : <div style={small}>{t("noGuide")}</div>}
      <EngineeringNote text={item.description} />
    </div>
  );
}

/** The Setting column of the registry table: label, what, key, badges, the Explain toggle and, open, the guide. */
export function SettingCell({ item, badges, extra }: { item: GuideItem; badges: ReactNode; extra?: ReactNode }) {
  const t = useT(SETTINGS);
  const [open, setOpen] = useState(false);
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 240, maxWidth: 560 }}>
      <strong style={{ fontSize: 13 }}>{item.label}</strong>
      <GuideSummary item={item} />
      <span style={{ ...small, fontFamily: mono, fontSize: 11 }}>{item.key}</span>
      {badges}
      {extra}
      <span data-testid={`settings-explain-${item.key}`}>
        <LinkButton size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <Info size={12} aria-hidden /> {open ? t("hideExplain") : t("explain")}
        </LinkButton>
      </span>
      {open && (
        <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 10, background: K.bg }}>
          <GuideDetails item={item} testId={`settings-guide-${item.key}`} />
        </div>
      )}
    </div>
  );
}

/** Who made a change: name or e-mail, the actor id in the tooltip. */
export function ActorName({ actor, name }: { actor: string | null | undefined; name: string | null | undefined }) {
  const t = useT(SETTINGS);
  const who = actorLabel(actor, name);
  return <span title={t("staffId", who.title)}>{who.text}</span>;
}

// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/KitGallery.tsx
// AA-994 S4a — the kit gallery: every core component in its states with static demo data, no backend. Shared by all
// apps that vendor the full core, so each app shows (and the visual job captures) the same thing. CIS renders it in
// admin Settings → UI kit; the gated `KitGallery` route of the visual job (root ADR 0016 decision 9, AA-994 S1d)
// renders it too. This first slice holds the components upstreamed from AA-Booking in S4a (Section, KpiTile,
// FilterBar, RowMenu, ConfirmModal, Money, DateText); S6a adds the form controls (Field, Input, Select, Textarea,
// Checkbox, Radio, DateField in a Field); the older core components join it with S1d.
//
// Demo dates are fixed values and the relative examples use a fixed "now" (no clock read during render), so the
// gallery renders the same text on the server and the client and in every screenshot.

import { Suspense, useState, type ReactNode } from "react";
import { Archive, Copy, Eye, Lock, CalendarCheck } from "lucide-react";
import { Button } from "./primitives";
import { ConfirmModal } from "./Modal";
import { Section } from "./Section";
import { KpiTile } from "./KpiTile";
import { FilterBar } from "./FilterBar";
import { RowMenu } from "./RowMenu";
import { Money } from "./Money";
import { DateText, relativeTime } from "./DateText";
import { DateField } from "./DateField";
import { Checkbox, Field, Input, Radio, Select, Textarea } from "./FormControls";
import { K, sans } from "./tokens";
import { GALLERY_MESSAGES } from "./kitGallery.messages";
import { useLang, useT } from "./useI18n";

const GRID: React.CSSProperties = {
  display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))", minWidth: 0,
};

function Entry({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <div data-kit-gallery={id} style={{ display: "flex", flexDirection: "column", gap: 10, minWidth: 0 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.1em", color: K.muted }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap", fontSize: 13, color: K.body }}>
      <span style={{ width: 120, flexShrink: 0, color: K.muted }}>{label}</span>
      <span style={{ display: "inline-flex", gap: 16, flexWrap: "wrap", minWidth: 0 }}>{children}</span>
    </div>
  );
}

/** The form controls in their states; local state only, nothing is sent. */
function FormDemo() {
  const t = useT(GALLERY_MESSAGES);
  const [name, setName] = useState(() => t("formNameValue"));
  const [code, setCode] = useState("HAN-0");
  const [currency, setCurrency] = useState("USD");
  const [day, setDay] = useState("2026-11-14");
  const [note, setNote] = useState("");
  const [notify, setNotify] = useState(true);
  const [pay, setPay] = useState("card");
  return (
    <div style={GRID}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <Field label={t("formName")} required hint={t("formNameHint")}>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("formCode")} required error={code.length < 6 ? t("formCodeError") : undefined}>
          <Input value={code} onChange={(e) => setCode(e.target.value)} />
        </Field>
        <Field label={t("formCurrency")}>
          <Select value={currency} onChange={(e) => setCurrency(e.target.value)}
            options={[{ value: "USD", label: "USD" }, { value: "EUR", label: "EUR" }, { value: "VND", label: "VND" }]} />
        </Field>
        <Field label={t("formStart")}>
          <DateField value={day} onChange={setDay} style={{ height: 40, width: "100%" }} />
        </Field>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <Field label={t("formNote")} hint={t("formNoteHint")}>
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("formNotePlaceholder")} />
        </Field>
        <Field label={t("formLockedLabel")} locked hint={t("formLockedHint")}>
          <Input value="BK-2026-0142" disabled readOnly />
        </Field>
        <Field label={t("formPay")} as="div">
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            {(["card", "transfer"] as const).map((v) => (
              <Radio key={v} name="kit-gallery-pay" value={v} checked={pay === v} onChange={() => setPay(v)}
                label={t(v === "card" ? "formPayCard" : "formPayTransfer")} />
            ))}
          </div>
        </Field>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Checkbox checked={notify} onChange={(e) => setNotify(e.target.checked)} label={t("formNotify")} />
          <Checkbox checked={false} indeterminate onChange={() => {}} label={t("formSome")} />
          <Checkbox checked disabled onChange={() => {}} label={t("formDisabled")} />
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <Input size="sm" placeholder={t("formSmall")} aria-label={t("formSmall")} style={{ width: 180 }} />
          <Select size="sm" aria-label={t("formStatus")} placeholder={t("formAnyStatus")} defaultValue=""
            options={[{ value: "open", label: t("filterOpen") }, { value: "closed", label: t("filterClosed") }]} />
        </div>
      </div>
    </div>
  );
}

/** The fixed "now" of the relative examples: 9 Oct 2026, 07:05 UTC. */
const DEMO_NOW = Date.UTC(2026, 9, 9, 7, 5);

type ConfirmDemo = null | "plain" | "danger" | "busy";

export function KitGallery() {
  const t = useT(GALLERY_MESSAGES);
  const [confirm, setConfirm] = useState<ConfirmDemo>(null);
  const close = () => setConfirm(null);
  const lang = useLang();

  return (
    <div data-testid="kit-gallery" style={{ display: "flex", flexDirection: "column", gap: 28, minWidth: 0, fontFamily: sans }}>
      <Entry id="section" title={t("sectionTitle")}>
        <div style={GRID}>
          <Section title={t("sectionTitle")} description={t("sectionDesc")}
            actions={<Button size="sm" variant="ghost">{t("edit")}</Button>}>
            <div style={{ fontSize: 13, color: K.body }}>{t("sectionBody")}</div>
          </Section>
          <Section title={t("sectionFlush")} padded={false}>
            <div style={{ padding: "12px 20px", fontSize: 13, color: K.body, borderBottom: `1px solid ${K.line2}` }}>
              {t("sectionFlushBody")}
            </div>
            <div style={{ padding: "12px 20px", fontSize: 13, color: K.body }}>{t("sectionFlushBody")}</div>
          </Section>
        </div>
      </Entry>

      <Entry id="kpi" title={t("kpiTitle")}>
        <div style={GRID}>
          <KpiTile label={t("kpiBookings")} value="128" hint={t("kpiUp")} hintTone="success"
            icon={<CalendarCheck size={16} />} info={t("kpiInfo")} />
          <KpiTile label={t("kpiRevenue")} value={<Money amount="48210.5" currency="USD" />} hint={t("kpiDown")}
            hintTone="danger" />
          <KpiTile label={t("kpiLoading")} value={undefined} />
          <KpiTile label={t("kpiEmpty")} value={null} />
        </div>
        <div style={GRID}>
          <KpiTile size="sm" label={t("kpiMargin")} value="-1,204.00 USD" valueTone="danger" />
          <KpiTile size="sm" label={t("kpiMargin")} value="3,980.00 USD" valueTone="success" hint={t("kpiUp")} />
          <KpiTile size="sm" label={t("kpiBookings")} value="42" />
        </div>
      </Entry>

      <Entry id="filterbar" title={t("filterTitle")}>
        <Suspense fallback={null}>
          <FilterBar
            testId="kit-gallery-filterbar"
            search={{ key: "kg_q", placeholder: t("filterSearch") }}
            selects={[{
              key: "kg_status", label: t("filterStatus"), allLabel: t("filterAllStatuses"), width: 160,
              options: [{ value: "open", label: t("filterOpen") }, { value: "closed", label: t("filterClosed") }],
            }]}
            dateRange={{ fromKey: "kg_from", toKey: "kg_to", label: t("filterCreated") }}
          />
        </Suspense>
        <div style={{ fontSize: 12, color: K.muted }}>{t("filterNote")}</div>
      </Entry>

      <Entry id="form" title={t("formTitle")}>
        <FormDemo />
      </Entry>

      <Entry id="rowmenu" title={t("menuTitle")}>
        <div>
          <RowMenu
            testId="kit-gallery-rowmenu"
            items={[
              { label: t("menuView"), icon: <Eye size={14} />, onSelect: () => {} },
              { label: t("menuDuplicate"), icon: <Copy size={14} />, onSelect: () => {} },
              { label: t("menuLocked"), icon: <Lock size={14} />, onSelect: () => {}, disabled: true },
              { label: t("menuArchive"), icon: <Archive size={14} />, onSelect: () => {}, danger: true, divider: true },
            ]}
          />
        </div>
      </Entry>

      <Entry id="confirm" title={t("confirmTitle")}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button size="sm" onClick={() => setConfirm("plain")}>{t("openConfirm")}</Button>
          <Button size="sm" variant="danger" onClick={() => setConfirm("danger")}>{t("openDanger")}</Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirm("busy")}>{t("openBusy")}</Button>
        </div>
        <ConfirmModal open={confirm === "plain"} onClose={close} onConfirm={close}
          title={t("confirmPlainTitle")} body={t("confirmPlainBody")} />
        <ConfirmModal open={confirm === "danger"} onClose={close} onConfirm={close} destructive
          title={t("confirmDangerTitle")} body={t("confirmDangerBody")} confirmLabel={t("confirmDangerAction")}
          reason={{ label: t("reasonLabel"), required: true, placeholder: t("reasonPlaceholder") }} />
        <ConfirmModal open={confirm === "busy"} onClose={close} onConfirm={close} destructive busy
          title={t("confirmDangerTitle")} body={t("confirmDangerBody")} confirmLabel={t("confirmDangerAction")}
          error={t("confirmError")} />
      </Entry>

      <Entry id="formats" title={t("formatTitle")}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Row label={t("fmtMoney")}>
            <Money amount="1845" currency="USD" />
            <Money amount="2390.5" currency="eur" />
            <Money amount="12500000" currency="VND" />
            <Money amount="1.2345" currency="KWD" strong />
          </Row>
          <Row label={t("fmtDay")}>
            <DateText value="2026-10-09" />
          </Row>
          <Row label={t("fmtInstant")}>
            <DateText value="2026-10-09T07:05:00Z" />
            <DateText value="2026-10-09T07:05:00Z" withTime />
          </Row>
          <Row label={t("fmtRelative")}>
            {["2026-10-09T07:04:40Z", "2026-10-09T06:40:00Z", "2026-10-09T04:05:00Z", "2026-10-07T07:05:00Z",
              "2026-09-20T07:05:00Z"].map((v) => <span key={v}>{relativeTime(v, DEMO_NOW, lang)}</span>)}
          </Row>
          <Row label={t("fmtEmpty")}>
            <Money amount={null} currency="USD" />
            <DateText value={null} />
          </Row>
        </div>
      </Entry>
    </div>
  );
}

// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/KitGallery.tsx
// AA-994 S4a — the kit gallery: every core component in its states with static demo data, no backend. Shared by all
// apps that vendor the full core, so each app shows (and the visual job captures) the same thing. CIS renders it in
// admin Settings → UI kit; the gated `KitGallery` route of the visual job (root ADR 0016 decision 9, AA-994 S1d)
// renders it too. This first slice holds the components upstreamed from AA-Booking in S4a (Section, KpiTile,
// FilterBar, RowMenu, ConfirmModal, Money, DateText); S6a adds the form controls (Field, Input, Select, Textarea,
// Checkbox, Radio, DateField in a Field); the button variants (core Buttons.tsx: Button incl. dashed, LinkButton,
// IconButton, ButtonGroup, SegmentedControl, ChipButton, Switch, underline Tabs, ListButton, Pressable) have their
// own entry; the older
// core components join it with S1d.
//
// Demo dates are fixed values and the relative examples use a fixed "now" (no clock read during render), so the
// gallery renders the same text on the server and the client and in every screenshot.

import { Suspense, useState, type ReactNode } from "react";
import {
  Archive, CalendarCheck, ChevronRight, Copy, Download, Eye, Lock, Pencil, Pin, Plus, Send, Trash2, X, XCircle,
} from "lucide-react";
import { Tabs } from "./primitives";
import {
  Button, ButtonGroup, ChipButton, IconButton, LinkButton, ListButton, Pressable, SegmentedControl, Switch,
} from "./Buttons";
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

/** Every kind of kit button; local state only, nothing is sent. */
function ButtonsDemo() {
  const t = useT(GALLERY_MESSAGES);
  const [pinned, setPinned] = useState(false);
  const [state, setState] = useState<"open" | "answered" | "all">("open");
  const [who, setWho] = useState<"existing" | "new">("existing");
  const [tab, setTab] = useState("overview");
  const [view, setView] = useState<"grid" | "table">("grid");
  const [place, setPlace] = useState("hoan-kiem");
  const [cats, setCats] = useState<string[]>(["hotel"]);
  const [mail, setMail] = useState(true);
  const noop = () => {};
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
      <Row label={t("btnBrand")}>
        <Button variant="primary" onClick={noop}>{t("btnSave")}</Button>
        <Button onClick={noop}>{t("btnExplore")}</Button>
        <Button variant="ghost" onClick={noop}>{t("btnCancel")}</Button>
        <Button variant="danger" onClick={noop}>{t("btnDelete")}</Button>
        <Button variant="dashed" onClick={noop}><Plus size={14} /> {t("btnAddDay")}</Button>
        <Button size="sm" onClick={noop}>{t("btnSmall")}</Button>
        <Button variant="primary" disabled>{t("btnDisabled")}</Button>
      </Row>
      <Row label={t("btnLink")}>
        <LinkButton onClick={noop}>{t("btnShowAll")}</LinkButton>
        <LinkButton tone="muted" aria-expanded onClick={noop}>{t("btnHide")}</LinkButton>
        <LinkButton tone="danger" size="sm" onClick={noop}>{t("btnRemove")}</LinkButton>
        <LinkButton weight="regular" truncate title={t("btnFile")} onClick={noop} style={{ maxWidth: 200 }}>
          <Download size={13} />
          {t("btnFile")}
        </LinkButton>
        <LinkButton disabled>{t("btnDisabled")}</LinkButton>
      </Row>
      <Row label={t("btnIcon")}>
        <IconButton label={t("btnEdit")} onClick={noop}><Pencil size={14} /></IconButton>
        <IconButton label={t("btnRemoveRow")} variant="plain" size="sm" tone="danger" onClick={noop}>
          <Trash2 size={14} />
        </IconButton>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 4px 2px 10px",
          borderRadius: 999, background: K.accentSoft, fontSize: 12, color: K.accentDeep }}>
          {t("btnChip")}
          <IconButton label={t("btnRemoveChip")} variant="plain" size="xs" round onClick={noop}><X size={12} /></IconButton>
        </span>
        <IconButton label={t("btnPin")} pressed={pinned} onClick={() => setPinned((p) => !p)}><Pin size={14} /></IconButton>
        <IconButton label={t("btnSend")} disabled><Send size={14} /></IconButton>
        <span style={{ display: "inline-grid", placeItems: "center", width: 96, height: 56, borderRadius: 8,
          background: "var(--aa-hero-bg)" }}>
          <IconButton label={t("btnNextPhoto")} variant="overlay" onClick={noop}><ChevronRight size={16} /></IconButton>
        </span>
      </Row>
      <Row label={t("btnGroup")}>
        <ButtonGroup label={t("btnRowActions")} gap="sm">
          <IconButton label={t("btnPreview")} onClick={noop}><Eye size={14} /></IconButton>
          <IconButton label={t("btnEdit")} onClick={noop}><Pencil size={14} /></IconButton>
          <IconButton label={t("btnSend")} onClick={noop}><Send size={14} /></IconButton>
        </ButtonGroup>
        <ButtonGroup label={t("btnViewSwitch")} attached>
          <Button size="sm" variant={view === "grid" ? "secondary" : "ghost"} aria-pressed={view === "grid"}
            onClick={() => setView("grid")}>{t("btnGrid")}</Button>
          <Button size="sm" variant={view === "table" ? "secondary" : "ghost"} aria-pressed={view === "table"}
            onClick={() => setView("table")}>{t("btnTable")}</Button>
        </ButtonGroup>
      </Row>
      <Row label={t("btnChoice")}>
        <SegmentedControl label={t("btnState")} appearance="chips" size="sm" value={state} onChange={setState}
          options={[
            { value: "open", label: t("btnWaiting") },
            { value: "answered", label: t("btnAnswered"), count: 3 },
            { value: "all", label: t("btnAll") },
          ]} />
        <SegmentedControl label={t("btnCustomer")} value={who} onChange={setWho}
          options={[{ value: "existing", label: t("btnExisting") }, { value: "new", label: t("btnNewLead") }]} />
      </Row>
      <Row label={t("btnChips")}>
        {(["hotel", "guide", "transport"] as const).map((c) => (
          <ChipButton key={c} role="checkbox" selected={cats.includes(c)}
            onClick={() => setCats((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]))}>
            {t(c === "hotel" ? "btnCatHotel" : c === "guide" ? "btnCatGuide" : "btnCatTransport")}
          </ChipButton>
        ))}
      </Row>
      <Row label={t("btnSwitch")}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <Switch label={t("btnPayMail")} checked={mail} onChange={setMail} />
          <span>{t("btnPayMail")}</span>
        </span>
        <Switch label={t("btnDisabled")} checked={false} onChange={() => {}} disabled />
      </Row>
      <Row label={t("btnTabs")}>
        <Tabs variant="underline" ariaLabel={t("btnSections")} active={tab} onChange={setTab}
          tabs={[
            { key: "overview", label: t("btnOverview") },
            { key: "proposals", label: t("btnProposals"), count: 2 },
            { key: "documents", label: t("btnDocuments"), disabled: true, title: t("btnLater") },
          ]} />
      </Row>
      <Row label={t("btnList")}>
        <span style={{ display: "inline-flex", flexDirection: "column", gap: 8 }}>
          <span role="listbox" aria-label={t("btnPlaces")} style={{ display: "flex", flexDirection: "column", width: 220,
            padding: 4, borderRadius: 8, border: `1px solid ${K.line}`, background: K.card }}>
            {[["hoan-kiem", t("btnTile")], ["old-quarter", t("formNameValue")]].map(([v, l]) => (
              <ListButton key={v} role="option" selected={place === v} onClick={() => setPlace(v)}>{l}</ListButton>
            ))}
          </span>
          <span role="menu" aria-label={t("menuTitle")} style={{ display: "flex", flexDirection: "column", width: 220,
            padding: 4, borderRadius: 8, border: `1px solid ${K.line}`, background: K.card }}>
            <ListButton role="menuitem" onClick={noop}><Copy size={14} /> {t("menuDuplicate")}</ListButton>
            <ListButton role="menuitem" tone="danger" onClick={noop}><XCircle size={14} /> {t("btnCancelCase")}</ListButton>
          </span>
        </span>
      </Row>
      <Row label={t("btnPressable")}>
        <span style={{ display: "inline-block", width: 160 }}>
          <Pressable ariaLabel={t("btnOpenPhoto")} onClick={noop}
            style={{ height: 96, borderRadius: 8, background: "var(--aa-hero-bg)", color: "var(--aa-on-solid)",
              fontSize: 12, display: "grid", placeItems: "end start", padding: 8 }}>
            {t("btnTile")}
          </Pressable>
        </span>
      </Row>
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

      <Entry id="buttons" title={t("btnTitle")}>
        <ButtonsDemo />
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

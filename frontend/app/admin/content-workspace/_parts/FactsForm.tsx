"use client";
// app/admin/content-workspace/_parts/FactsForm.tsx — AA-1148: the facts sections of the hand-made tour editor:
// Basics (name, country, AA supplier, summary, nights, group, type, languages, best time), Day by day (one card per
// day: title, places, meals, overnight, description; move, duplicate, remove) and the included / not included lists.
// Controlled: the parent owns the form and saves it (TourWorkspaceEditor.tsx). Kit controls only (ADR 0016).

import { ArrowDown, ArrowUp, Copy, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import {
  Badge, Button, Checkbox, ChipButton, Field, Input, K, Section, Select, Textarea, useT, type Translator,
} from "../../../_kit";
import {
  MEALS, addItems, duplicateDay, emptyDay, moveDay, splitItems, usualNights, type DayForm, type FactsForm,
  type Options, type Supplier,
} from "@/lib/contentWorkspace";
import { CW } from "../messages";

type T = Translator<typeof CW.en>;
type SetForm = (patch: Partial<FactsForm>) => void;

// Common items of an AA program, offered as one-click chips (English: the tour's content language).
const QUICK_INCLUDED = ["Private transport", "English-speaking guide", "Entrance fees", "Meals as listed",
  "Accommodation as listed", "Drinking water"];
const QUICK_EXCLUDED = ["International flights", "Travel insurance", "Drinks", "Tips", "Personal expenses",
  "Visa fees"];

export function FactsForm({ form, set, options, suppliers, supplierQuery, setSupplierQuery, readOnly }: {
  form: FactsForm; set: SetForm; options: Options | undefined; suppliers: Supplier[] | null; supplierQuery: string;
  setSupplierQuery: (q: string) => void; readOnly: boolean;
}) {
  const t = useT(CW);
  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1fr)" }}>
      <Basics form={form} set={set} options={options} suppliers={suppliers} supplierQuery={supplierQuery}
        setSupplierQuery={setSupplierQuery} readOnly={readOnly} t={t} />
      <Days form={form} set={set} maxDays={options?.limits.max_days ?? 0} readOnly={readOnly} t={t} />
      <ItemList title={t("included")} items={form.inclusions} quick={QUICK_INCLUDED} readOnly={readOnly} t={t}
        testId="cw-included" onChange={(inclusions) => set({ inclusions })} />
      <ItemList title={t("excluded")} items={form.exclusions} quick={QUICK_EXCLUDED} readOnly={readOnly} t={t}
        testId="cw-excluded" onChange={(exclusions) => set({ exclusions })} />
    </div>
  );
}

function supplierLabel(s: Supplier, t: T): string {
  const tags = [s.code, s.country_code, s.categories.includes("dmc") ? t("dmc") : null,
    s.status === "pending" ? t("pending") : null].filter(Boolean);
  return `${s.company_name ?? s.id}${tags.length ? ` (${tags.join(", ")})` : ""}`;
}

function Basics({ form, set, options, suppliers, supplierQuery, setSupplierQuery, readOnly, t }: {
  form: FactsForm; set: SetForm; options: Options | undefined; suppliers: Supplier[] | null; supplierQuery: string;
  setSupplierQuery: (q: string) => void; readOnly: boolean; t: T;
}) {
  const days = form.days.length;
  const supplierOptions = (suppliers ?? []).map((s) => ({ value: s.id, label: supplierLabel(s, t) }));
  if (form.supplier_id && !supplierOptions.some((o) => o.value === form.supplier_id)) {
    supplierOptions.unshift({ value: form.supplier_id, label: form.supplier_id });
  }
  const grid2: React.CSSProperties = {
    display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))",
  };
  return (
    <Section title={t("basics")} description={t("basicsDesc")} testId="cw-basics">
      <div style={{ display: "grid", gap: 12 }}>
        <Field label={t("name")} required>
          <Input value={form.name} maxLength={200} disabled={readOnly} onChange={(e) => set({ name: e.target.value })}
            style={{ width: "100%" }} />
        </Field>
        <div style={grid2}>
          <Field label={t("country")} required>
            <Select value={form.country} disabled={readOnly} placeholder={t("chooseCountry")} style={{ width: "100%" }}
              options={(options?.countries ?? []).map((c) => ({ value: c.iso2, label: c.name }))}
              onChange={(e) => set({ country: e.target.value })} />
          </Field>
          <Field label={t("supplier")} required hint={suppliers === null ? t("suppliersDown") : t("supplierHint")}>
            <div style={{ display: "grid", gap: 6 }}>
              {!readOnly && suppliers !== null && (
                <Input value={supplierQuery} placeholder={t("supplierSearch")} aria-label={t("supplierSearch")}
                  onChange={(e) => setSupplierQuery(e.target.value)} style={{ width: "100%" }} />
              )}
              <Select value={form.supplier_id} disabled={readOnly || suppliers === null} placeholder={t("chooseSupplier")}
                options={supplierOptions} onChange={(e) => set({ supplier_id: e.target.value })}
                style={{ width: "100%" }} />
            </div>
          </Field>
        </div>
        <Field label={t("summary")}>
          <Textarea value={form.summary} rows={3} maxLength={2000} disabled={readOnly} style={{ width: "100%" }}
            onChange={(e) => set({ summary: e.target.value })} />
        </Field>
        <div style={grid2}>
          <Field label={t("nights")} required hint={days ? t("nightsHint", days) : undefined}>
            <Input inputMode="numeric" value={form.nights} disabled={readOnly} maxLength={3} style={{ width: "100%" }}
              placeholder={days ? String(usualNights(days)) : ""} onChange={(e) => set({ nights: e.target.value })} />
          </Field>
          <Field label={t("tourType")}>
            <Select value={form.tour_type} disabled={readOnly} style={{ width: "100%" }}
              options={[{ value: "", label: t("tourTypeNone") }, { value: "private", label: t("tourPrivate") },
                { value: "join_in", label: t("tourJoinIn") }]}
              onChange={(e) => set({ tour_type: e.target.value as FactsForm["tour_type"] })} />
          </Field>
          <Field label={t("groupMin")}>
            <Input inputMode="numeric" value={form.group_min} disabled={readOnly} maxLength={3}
              style={{ width: "100%" }} onChange={(e) => set({ group_min: e.target.value })} />
          </Field>
          <Field label={t("groupMax")}>
            <Input inputMode="numeric" value={form.group_max} disabled={readOnly} maxLength={3}
              style={{ width: "100%" }} onChange={(e) => set({ group_max: e.target.value })} />
          </Field>
        </div>
        <div style={grid2}>
          <Field label={t("languages")}>
            <Input value={form.guide_languages.join(", ")} disabled={readOnly} style={{ width: "100%" }}
              onChange={(e) => set({ guide_languages: splitItems(e.target.value) })} />
          </Field>
          <Field label={t("bestTime")}>
            <Input value={form.best_time_to_go} maxLength={500} disabled={readOnly} style={{ width: "100%" }}
              onChange={(e) => set({ best_time_to_go: e.target.value })} />
          </Field>
        </div>
      </div>
    </Section>
  );
}

function Days({ form, set, maxDays, readOnly, t }: { form: FactsForm; set: SetForm; maxDays: number; readOnly: boolean; t: T }) {
  const setDay = (i: number, patch: Partial<DayForm>) =>
    set({ days: form.days.map((d, j) => (j === i ? { ...d, ...patch } : d)) });
  const full = maxDays > 0 && form.days.length >= maxDays;
  return (
    <Section title={t("days")} description={t("daysDesc", maxDays)} testId="cw-days">
      <div style={{ display: "grid", gap: 12 }}>
        {form.days.map((d, i) => (
          <DayCard key={i} n={i + 1} day={d} last={i === form.days.length - 1} readOnly={readOnly} t={t}
            onChange={(patch) => setDay(i, patch)}
            onMove={(to) => set({ days: moveDay(form.days, i, to) })}
            onDuplicate={full ? undefined : () => set({ days: duplicateDay(form.days, i) })}
            onRemove={() => set({ days: form.days.filter((_, j) => j !== i) })} />
        ))}
        {!readOnly && (
          <div>
            <Button variant="dashed" size="sm" disabled={full} onClick={() => set({ days: [...form.days, emptyDay()] })}>
              <Plus size={13} /> {t("addDay")}
            </Button>
          </div>
        )}
      </div>
    </Section>
  );
}

function DayCard({ n, day, last, readOnly, t, onChange, onMove, onDuplicate, onRemove }: {
  n: number; day: DayForm; last: boolean; readOnly: boolean; t: T; onChange: (p: Partial<DayForm>) => void;
  onMove: (to: number) => void; onDuplicate?: () => void; onRemove: () => void;
}) {
  const [place, setPlace] = useState("");
  const addPlaces = () => {
    const items = splitItems(place);
    if (items.length) onChange({ places: addItems(day.places, items) });
    setPlace("");
  };
  return (
    <div data-testid={`cw-day-${n}`} style={{ border: `1px solid ${K.line}`, borderRadius: 10, padding: 12,
      background: K.card, display: "grid", gap: 10, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <strong style={{ color: K.ink }}>{t("day", n)}</strong>
        {!readOnly && (
          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
            <Button size="sm" variant="ghost" disabled={n === 1} onClick={() => onMove(n - 2)} aria-label={t("moveUp")}>
              <ArrowUp size={13} />
            </Button>
            <Button size="sm" variant="ghost" disabled={last} onClick={() => onMove(n)} aria-label={t("moveDown")}>
              <ArrowDown size={13} />
            </Button>
            <Button size="sm" variant="ghost" disabled={!onDuplicate} onClick={onDuplicate} aria-label={t("duplicate")}>
              <Copy size={13} />
            </Button>
            <Button size="sm" variant="ghost" onClick={onRemove} aria-label={t("remove")}><Trash2 size={13} /></Button>
          </span>
        )}
      </div>
      <Field label={t("dayTitle")} required>
        <Input value={day.title} maxLength={200} disabled={readOnly} style={{ width: "100%" }}
          onChange={(e) => onChange({ title: e.target.value })} />
      </Field>
      <Field label={t("places")} required as="div">
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: day.places.length ? 6 : 0 }}>
          {day.places.map((p) => (
            <ChipButton key={p} size="sm" selected disabled={readOnly} aria-label={t("removeItem", p)}
              onClick={() => onChange({ places: day.places.filter((x) => x !== p) })}>
              {p}{!readOnly && <X size={11} />}
            </ChipButton>
          ))}
        </div>
        {!readOnly && (
          <div style={{ display: "flex", gap: 6 }}>
            <Input value={place} aria-label={t("places")} style={{ flex: 1, minWidth: 0 }}
              onChange={(e) => setPlace(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addPlaces(); } }} onBlur={addPlaces} />
            <Button size="sm" onClick={addPlaces}>{t("addPlace")}</Button>
          </div>
        )}
      </Field>
      <Field label={t("meals")} as="div">
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          {MEALS.map((m) => (
            <Checkbox key={m} label={t(`meal_${m}` as const)} checked={day.meals.includes(m)} disabled={readOnly}
              onChange={(e) => onChange({ meals: e.target.checked ? MEALS.filter((x) => x === m || day.meals.includes(x))
                : day.meals.filter((x) => x !== m) })} />
          ))}
        </div>
      </Field>
      <Field label={t("overnight")}>
        <Input value={day.overnight} maxLength={200} disabled={readOnly} style={{ width: "100%" }}
          onChange={(e) => onChange({ overnight: e.target.value })} />
      </Field>
      <Field label={t("dayDescription")} required>
        <Textarea value={day.description} rows={3} maxLength={4000} disabled={readOnly} style={{ width: "100%" }}
          onChange={(e) => onChange({ description: e.target.value })} />
      </Field>
    </div>
  );
}

function ItemList({ title, items, quick, readOnly, t, testId, onChange }: {
  title: string; items: string[]; quick: string[]; readOnly: boolean; t: T; testId: string;
  onChange: (items: string[]) => void;
}) {
  const [text, setText] = useState("");
  const add = () => {
    const more = splitItems(text);
    if (more.length) onChange(addItems(items, more));
    setText("");
  };
  const lower = new Set(items.map((x) => x.toLowerCase()));
  const offers = quick.filter((q) => !lower.has(q.toLowerCase()));
  return (
    <Section title={title} testId={testId} actions={<Badge tone="neutral" dot={false}>{items.length}</Badge>}>
      <div style={{ display: "grid", gap: 8 }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {items.map((x) => (
            <ChipButton key={x} size="sm" selected disabled={readOnly} aria-label={t("removeItem", x)}
              onClick={() => onChange(items.filter((y) => y !== x))}>
              {x}{!readOnly && <X size={11} />}
            </ChipButton>
          ))}
        </div>
        {!readOnly && (
          <>
            <div style={{ display: "flex", gap: 6 }}>
              <Input value={text} placeholder={t("addItems")} aria-label={`${title}: ${t("addItems")}`}
                style={{ flex: 1, minWidth: 0 }} onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} onBlur={add} />
              <Button size="sm" onClick={add}>{t("addPlace")}</Button>
            </div>
            {offers.length > 0 && (
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                <span style={{ fontSize: 12, color: K.muted }}>{t("quickPick")}:</span>
                {offers.map((q) => (
                  <ChipButton key={q} size="sm" selected={false} onClick={() => onChange(addItems(items, [q]))}>
                    <Plus size={11} /> {q}
                  </ChipButton>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </Section>
  );
}

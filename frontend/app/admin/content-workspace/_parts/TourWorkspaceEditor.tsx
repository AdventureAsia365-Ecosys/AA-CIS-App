"use client";
// app/admin/content-workspace/_parts/TourWorkspaceEditor.tsx — AA-1148: the hand-made tour editor (new and existing).
//
// Tabs: Facts (FactsForm), Customer wording (the AA-1146 draft editor: save with a reason, run the checks, publish
// through the one gate), Preview (facts + the saved wording draft), History (the fact changes). Beside them: the
// required list (the publish is refused while a required item is missing) and the read-only price from Booking.
//
// Saving: a new tour is created with Create (it needs a name). A tour that was never published saves by itself 1.5 s
// after the last change ("Saving…", "Saved 10:42", "Could not save. Retry"); the save carries the time of the last
// save it knows, so a save over someone else's is refused (409 facts_changed: Reload). A published tour does not
// autosave: its facts go live on save, so the person writes a reason and presses Save changes.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError, Badge, Button, Field, Input, K, PageFrame, PageHeader, Section, Tabs, apiGet, apiSend, useAdminIdentity,
  useFormat, useIsPhone, useT, useToast, type Translator,
} from "../../../_kit";
import TourDraftEditor from "../../_components/TourDraftEditor";
import { can } from "@/lib/rbac";
import {
  CW_API, CW_PAGE, bodyOf, emptyForm, formKey, formOf, noticeKeys, savedTypeLabel, stateTone, tourHref, type FactsForm,
  type Options, type Supplier, type TourView,
} from "@/lib/contentWorkspace";
import { CW } from "../messages";
import { FactsForm as FactsSections } from "./FactsForm";
import { Checklist, History, PricePanel, Preview } from "./Panels";

type Status = "idle" | "saving" | "saved" | "error" | "stale";
type T = Translator<typeof CW.en>;
const AUTOSAVE_MS = 1500;   // UI pacing of the autosave (not a policy value)

function useOptions() {
  return useQuery({ queryKey: ["content-workspace-options"], retry: false,
    queryFn: () => apiGet<Options>(`${CW_API}/options`) });
}

function useSuppliers(q: string, country: string) {
  return useQuery({
    queryKey: ["content-workspace-suppliers", q, country],
    queryFn: () => apiGet<{ available: boolean; items: Supplier[] }>(
      `${CW_API}/suppliers?limit=200${q.trim() ? `&q=${encodeURIComponent(q.trim())}` : ""}`
      + `${country ? `&country=${country}` : ""}`),
    retry: false,
  });
}

export default function TourWorkspaceEditor({ tourId }: { tourId: string | null }) {
  const t = useT(CW);
  const tour = useQuery({
    queryKey: ["content-workspace-tour", tourId],
    enabled: !!tourId,
    queryFn: () => apiGet<TourView>(`${CW_API}/tours/${encodeURIComponent(tourId ?? "")}`),
    retry: false,
  });
  if (tourId && tour.isError) {
    return <PageFrame><p style={{ color: K.danger }}>{(tour.error as Error)?.message || t("loadError")}</p></PageFrame>;
  }
  if (tourId && !tour.data) return <PageFrame><p style={{ color: K.muted }}>…</p></PageFrame>;
  return <Editor key={tourId ?? "new"} tourId={tourId} view={tour.data ?? null} />;
}

type Toast = ReturnType<typeof useToast>;
type Saved = { at?: string; key: string };

const initialSaved = (view: TourView | null): Saved => ({
  at: view?.facts.saved_at, key: formKey(view ? formOf(view.facts) : emptyForm()),
});

/** Save the facts of an existing tour (autosave or with a reason). The save carries the time of the last save it
 *  knows (expected_saved_at); a 409 means someone else saved in between. */
function useFactsSaver(tourId: string | null, view: TourView | null, form: FactsForm, toast: Toast) {
  const qc = useQueryClient();
  const [status, setStatus] = useState<Status>("idle");
  const [saved, setSaved] = useState<Saved>(() => initialSaved(view));
  const save = useCallback(async (withReason?: string): Promise<boolean> => {
    if (!tourId) return false;
    const key = formKey(form);
    setStatus("saving");
    try {
      const res = await apiSend<{ saved_at: string }>(`${CW_API}/tours/${encodeURIComponent(tourId)}/facts`, {
        method: "PUT", body: { ...bodyOf(form), expected_saved_at: saved.at ?? null, reason: withReason ?? null },
      });
      setSaved((old) => ({ at: res.saved_at ?? old.at, key }));
      setStatus("saved");
      void qc.invalidateQueries({ queryKey: ["content-workspace-tour", tourId] });
      void qc.invalidateQueries({ queryKey: ["content-workspace-history", tourId] });
      return true;
    } catch (e) {
      const stale = e instanceof ApiError && e.status === 409;
      setStatus(stale ? "stale" : "error");
      if (!stale) toast.error((e as Error).message, 5000);
      return false;
    }
  }, [form, qc, saved.at, toast, tourId]);
  return { status, setStatus, saved, dirty: formKey(form) !== saved.key, save };
}

function useAutosave(enabled: boolean, save: () => Promise<boolean>) {
  useEffect(() => {
    if (!enabled) return undefined;
    const timer = setTimeout(() => void save(), AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [enabled, save]);
}

function statusLine(status: Status, dirty: boolean, isNew: boolean, at: string | undefined, t: T,
  fmt: (d: string) => string): string {
  if (status === "saving") return t("saving");
  if (status === "error") return t("saveFailed");
  if (dirty && !isNew) return t("unsaved");
  return at ? t("saved", fmt(at)) : "";
}

function Editor({ tourId, view }: { tourId: string | null; view: TourView | null }) {
  const t = useT(CW);
  const toast = useToast();
  const { role } = useAdminIdentity();
  const { formatDateTime } = useFormat();
  const [form, setForm] = useState<FactsForm>(() => (view ? formOf(view.facts) : emptyForm()));
  const [reason, setReason] = useState("");
  const saver = useFactsSaver(tourId, view, form, toast);
  const writer = can(role, "content.write");
  const readOnly = !writer || (view !== null && !view.editable);
  const autosave = !!tourId && !readOnly && !view?.reason_required;
  useAutosave(autosave && saver.dirty && saver.status !== "stale", saver.save);
  const create = useCreate(form, saver.setStatus, toast);
  const set = (patch: Partial<FactsForm>) => setForm((f) => ({ ...f, ...patch }));
  const title = form.name.trim() || (tourId ? t("untitled") : t("newTitle"));
  const line = statusLine(saver.status, saver.dirty, !tourId, saver.saved.at, t, formatDateTime);
  return (
    <PageFrame>
      <PageHeader breadcrumbs={[{ label: t("back"), href: CW_PAGE }]}
        title={<span style={{ overflowWrap: "anywhere" }}>{title}</span>}
        description={<HeaderStatus view={view} line={line} failed={saver.status === "error"}
          onRetry={() => void saver.save()} />} />
      <Notices view={view} writer={writer} status={saver.status} />
      <Layout aside={<Aside view={view} />}>
        <EditorTabs tourId={tourId} title={title} form={form} set={set} readOnly={readOnly}
          tourTypeLabel={savedTypeLabel(view)} />
        <SaveBar tourId={tourId} view={view} readOnly={readOnly} form={form} dirty={saver.dirty} status={saver.status}
          reason={reason} setReason={setReason} onCreate={() => void create()}
          onSave={(r) => void saver.save(r).then((ok) => { if (ok) setReason(""); })} />
      </Layout>
    </PageFrame>
  );
}

function useCreate(form: FactsForm, setStatus: (s: Status) => void, toast: Toast) {
  const t = useT(CW);
  const qc = useQueryClient();
  const router = useRouter();
  return async () => {
    setStatus("saving");
    try {
      const res = await apiSend<{ tour_id: string }>(`${CW_API}/tours`, { method: "POST", body: bodyOf(form) });
      toast.success(t("created"), 3000);
      void qc.invalidateQueries({ queryKey: ["content-workspace-tours"] });
      router.replace(tourHref(res.tour_id));
    } catch (e) {
      setStatus("error");
      toast.error((e as Error).message, 5000);
    }
  };
}

function HeaderStatus({ view, line, failed, onRetry }: {
  view: TourView | null; line: string; failed: boolean; onRetry: () => void;
}) {
  const t = useT(CW);
  return (
    <span style={{ display: "inline-flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
      {view && <Badge tone={stateTone(view.state)}>{t(`state_${view.state}` as const)}</Badge>}
      {line && <span data-testid="cw-save-status" style={{ fontSize: 12, color: K.muted }}>{line}</span>}
      {failed && <Button size="sm" variant="ghost" onClick={onRetry}>{t("retry")}</Button>}
    </span>
  );
}

function Layout({ aside, children }: { aside: React.ReactNode; children: React.ReactNode }) {
  const phone = useIsPhone();
  return (
    <div style={{ display: "grid", gap: 16, alignItems: "start",
      gridTemplateColumns: phone ? "minmax(0, 1fr)" : "minmax(0, 1fr) 300px" }}>
      <div style={{ minWidth: 0, display: "grid", gap: 12 }}>{children}</div>
      <aside style={{ minWidth: 0, display: "grid", gap: 12 }}>{aside}</aside>
    </div>
  );
}

function Aside({ view }: { view: TourView | null }) {
  const t = useT(CW);
  if (!view) {
    return <Section title={t("required")}><p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t("createHint")}</p>
    </Section>;
  }
  return (
    <>
      <Checklist items={view.checklist} completeness={view.completeness} />
      <PricePanel price={view.from_price} />
    </>
  );
}

function tabItems(tourId: string | null, t: T) {
  const none = !tourId;
  return [{ key: "facts", label: t("tabFacts") },
    { key: "wording", label: t("tabWording"), disabled: none, title: none ? t("wordingFirst") : undefined },
    { key: "preview", label: t("tabPreview") },
    { key: "history", label: t("tabHistory"), disabled: none }];
}

/** The supplier picker's list: null when Booking's list cannot be read (the picker says so). */
function pickerList(data: { available: boolean; items: Supplier[] } | undefined): Supplier[] | null {
  if (!data) return [];
  return data.available ? data.items : null;
}

function EditorTabs({ tourId, title, form, set, readOnly, tourTypeLabel }: {
  tourId: string | null; title: string; form: FactsForm; set: (p: Partial<FactsForm>) => void; readOnly: boolean;
  tourTypeLabel?: string | null;
}) {
  const t = useT(CW);
  const [tab, setTab] = useState("facts");
  const [supplierQuery, setSupplierQuery] = useState("");
  const options = useOptions();
  const sup = useSuppliers(supplierQuery, form.country);
  const id = tourId ?? "";
  const panes: Record<string, () => React.ReactNode> = {
    facts: () => (
      <FactsSections form={form} set={set} options={options.data} suppliers={pickerList(sup.data)}
        supplierQuery={supplierQuery} setSupplierQuery={setSupplierQuery} readOnly={readOnly}
        tourTypeLabel={tourTypeLabel} />),
    wording: () => <Wording tourId={id} twoPerson={options.data?.two_person_publish === true} />,
    preview: () => <Preview tourId={tourId} form={form} />,
    history: () => <History tourId={id} />,
  };
  return (
    <>
      <Tabs active={tab} onChange={setTab} variant="underline" ariaLabel={title} testId="cw-tabs"
        tabs={tabItems(tourId, t)} />
      {panes[tab]()}
    </>
  );
}

function Notices({ view, writer, status }: { view: TourView | null; writer: boolean; status: Status }) {
  const t = useT(CW);
  const box: React.CSSProperties = { border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 12px",
    marginBottom: 12, fontSize: 13, background: K.panel, color: K.ink };
  return (
    <>
      {status === "stale" && (
        <div role="alert" style={{ ...box, borderColor: K.dangerBorder, background: K.dangerSoft }}>
          {t("staleSave")} <Button size="sm" variant="secondary" onClick={() => window.location.reload()}>
            {t("reload")}</Button>
        </div>
      )}
      {noticeKeys(view, writer).map((key) => <div key={key} style={box}>{t(key)}</div>)}
    </>
  );
}

function SaveBar({ tourId, view, readOnly, form, dirty, status, reason, setReason, onCreate, onSave }: {
  tourId: string | null; view: TourView | null; readOnly: boolean; form: FactsForm; dirty: boolean; status: Status;
  reason: string; setReason: (r: string) => void; onCreate: () => void; onSave: (reason: string) => void;
}) {
  const t = useT(CW);
  if (readOnly) return null;
  if (!tourId) {
    return (
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span data-testid="cw-create">
          <Button variant="primary" disabled={!form.name.trim() || status === "saving"} onClick={onCreate}>
            {status === "saving" ? t("creating") : t("create")}
          </Button>
        </span>
        <span style={{ fontSize: 12, color: K.muted }}>{t("createHint")}</span>
      </div>
    );
  }
  if (!view?.reason_required) return null;
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <Field label={t("reason")} hint={t("reasonHint")}>
        <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} />
      </Field>
      <div>
        <Button variant="primary" disabled={!dirty || reason.trim().length < 3 || status === "saving"}
          onClick={() => onSave(reason.trim())}>{t("saveChanges")}</Button>
      </div>
    </div>
  );
}

function Wording({ tourId, twoPerson }: { tourId: string; twoPerson: boolean }) {
  const t = useT(CW);
  return (
    <Section title={t("tabWording")} description={t("wordingIntro")} testId="cw-wording">
      {twoPerson && <p style={{ fontSize: 13, color: K.ink3, margin: "0 0 10px" }}>{t("twoPerson")}</p>}
      <TourDraftEditor tourId={tourId} fallback={<p style={{ color: K.muted }}>{t("loadError")}</p>} />
    </Section>
  );
}

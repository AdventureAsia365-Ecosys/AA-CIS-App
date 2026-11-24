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
  useFormat, useIsPhone, useT, useToast,
} from "../../../_kit";
import TourDraftEditor from "../../_components/TourDraftEditor";
import { can } from "@/lib/rbac";
import {
  CW_API, CW_PAGE, bodyOf, emptyForm, formKey, formOf, stateTone, tourHref, type FactsForm, type Options,
  type Supplier, type TourView,
} from "@/lib/contentWorkspace";
import { CW } from "../messages";
import { FactsForm as FactsSections } from "./FactsForm";
import { Checklist, History, PricePanel, Preview } from "./Panels";

type Status = "idle" | "saving" | "saved" | "error" | "stale";
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

function Editor({ tourId, view }: { tourId: string | null; view: TourView | null }) {
  const t = useT(CW);
  const toast = useToast();
  const qc = useQueryClient();
  const router = useRouter();
  const phone = useIsPhone();
  const { role } = useAdminIdentity();
  const { formatDateTime } = useFormat();
  const options = useOptions();
  const [form, setForm] = useState<FactsForm>(() => (view ? formOf(view.facts) : emptyForm()));
  const [supplierQuery, setSupplierQuery] = useState("");
  const sup = useSuppliers(supplierQuery, form.country);
  const [tab, setTab] = useState("facts");
  const [status, setStatus] = useState<Status>("idle");
  const [reason, setReason] = useState("");
  // The last save the page knows: its time (sent back as expected_saved_at) and the form it saved.
  const [saved, setSaved] = useState<{ at?: string; key: string }>(() => ({
    at: view?.facts.saved_at, key: formKey(view ? formOf(view.facts) : emptyForm()) }));
  const writer = can(role, "content.write");
  const readOnly = !writer || (view !== null && !view.editable);
  const autosave = !!tourId && !readOnly && !view?.reason_required;
  const dirty = formKey(form) !== saved.key;

  const save = useCallback(async (withReason?: string) => {
    if (!tourId) return;
    const key = formKey(form);
    setStatus("saving");
    try {
      const res = await apiSend<{ saved_at: string }>(`${CW_API}/tours/${encodeURIComponent(tourId)}/facts`, {
        method: "PUT", body: { ...bodyOf(form), expected_saved_at: saved.at ?? null, reason: withReason ?? null },
      });
      setSaved((old) => ({ at: res.saved_at ?? old.at, key }));
      setStatus("saved");
      if (withReason) setReason("");
      void qc.invalidateQueries({ queryKey: ["content-workspace-tour", tourId] });
      void qc.invalidateQueries({ queryKey: ["content-workspace-history", tourId] });
    } catch (e) {
      setStatus(e instanceof ApiError && e.status === 409 ? "stale" : "error");
      if (!(e instanceof ApiError && e.status === 409)) toast.error((e as Error).message, 5000);
    }
  }, [form, qc, saved.at, toast, tourId]);

  useEffect(() => {
    if (!autosave || !dirty || status === "stale") return undefined;
    const timer = setTimeout(() => void save(), AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [autosave, dirty, save, status]);

  const create = async () => {
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

  const set = (patch: Partial<FactsForm>) => setForm((f) => ({ ...f, ...patch }));
  const title = form.name.trim() || (tourId ? t("untitled") : t("newTitle"));
  const statusText = status === "saving" ? t("saving") : status === "error" ? t("saveFailed")
    : dirty && tourId ? t("unsaved") : saved.at ? t("saved", formatDateTime(saved.at)) : "";

  return (
    <PageFrame>
      <PageHeader
        breadcrumbs={[{ label: t("back"), href: CW_PAGE }]}
        title={<span style={{ overflowWrap: "anywhere" }}>{title}</span>}
        description={
          <span style={{ display: "inline-flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            {view && <Badge tone={stateTone(view.state)}>{t(`state_${view.state}` as const)}</Badge>}
            {statusText && <span data-testid="cw-save-status" style={{ fontSize: 12, color: K.muted }}>{statusText}</span>}
            {status === "error" && <Button size="sm" variant="ghost" onClick={() => void save()}>{t("retry")}</Button>}
          </span>
        }
      />
      <Notices view={view} writer={writer} status={status} />
      <div style={{ display: "grid", gap: 16, alignItems: "start",
        gridTemplateColumns: phone ? "minmax(0, 1fr)" : "minmax(0, 1fr) 300px" }}>
        <div style={{ minWidth: 0, display: "grid", gap: 12 }}>
          <Tabs active={tab} onChange={setTab} variant="underline" ariaLabel={title} testId="cw-tabs"
            tabs={[{ key: "facts", label: t("tabFacts") },
              { key: "wording", label: t("tabWording"), disabled: !tourId, title: tourId ? undefined : t("wordingFirst") },
              { key: "preview", label: t("tabPreview") },
              { key: "history", label: t("tabHistory"), disabled: !tourId }]} />
          {tab === "facts" && (
            <FactsSections form={form} set={set} options={options.data}
              suppliers={sup.data ? (sup.data.available ? sup.data.items : null) : []}
              supplierQuery={supplierQuery} setSupplierQuery={setSupplierQuery} readOnly={readOnly} />
          )}
          {tab === "wording" && tourId && <Wording tourId={tourId} twoPerson={!!options.data?.two_person_publish} />}
          {tab === "preview" && <Preview tourId={tourId} form={form} />}
          {tab === "history" && tourId && <History tourId={tourId} />}
          <SaveBar tourId={tourId} view={view} readOnly={readOnly} form={form} dirty={dirty} status={status}
            reason={reason} setReason={setReason} onCreate={() => void create()} onSave={(r) => void save(r)} />
        </div>
        <aside style={{ minWidth: 0, display: "grid", gap: 12 }}>
          {view && <Checklist items={view.checklist} completeness={view.completeness} />}
          {!view && <Section title={t("required")}><p style={{ margin: 0, fontSize: 13, color: K.muted }}>
            {t("createHint")}</p></Section>}
          {view && <PricePanel price={view.from_price} />}
        </aside>
      </div>
    </PageFrame>
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
      {view?.entry_origin === "sheet" && <div style={box}>{t("readOnlySheet")}</div>}
      {!writer && <div style={box}>{t("readOnlyRole")}</div>}
      {view?.reason_required && view.editable && writer && <div style={box}>{t("liveNote")}</div>}
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

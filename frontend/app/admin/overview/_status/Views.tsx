"use client";
// app/admin/overview/_status/Views.tsx — AA-1141 (P1 feature 7): named saved views of the status page per staff member
// (GET / POST / DELETE /api/admin/overview/views; the page's own URL keys under a name such as "Wave watch") and the
// auto refresh chip ("Auto 60 s · Pause"; paused while the tab is hidden, on a custom or zoomed range, or by hand).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Button, ChipButton, Input, Select, apiErrorMessage, apiGet, apiSend, useToast,
} from "../../../_kit";
import { savedViewQuery, viewQueryOf, type PauseReason } from "@/lib/opsStatus";
import { rowWrap, small, useST } from "./ui";

interface SavedView { id: string; name: string; query: Record<string, string> }
const KEY = ["admin-ecosystem-status-views"];

function errorText(e: unknown): string {
  const err = e as { status?: number; body?: unknown; message?: string };
  return err?.body !== undefined ? apiErrorMessage(err.body, err.status ?? 0) : err?.message ?? "error";
}

/** Saved views: open one (writes its keys into the URL), save the current view under a name, delete one. */
export function SavedViews({ apply }: { apply: (qs: string) => void }) {
  const t = useST();
  const toast = useToast();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState("");
  const res = useQuery({
    queryKey: KEY,
    queryFn: () => apiGet<{ ready: boolean; views: SavedView[] }>("/api/admin/overview/views", { admin: true }),
    retry: false, staleTime: 60_000,
  });
  const views = res.data?.views ?? [];
  const open = (id: string) => {
    setPicked(id);
    const v = views.find((x) => x.id === id);
    if (v) apply(savedViewQuery(window.location.search, v.query));
  };
  const save = async () => {
    try {
      await apiSend("/api/admin/overview/views", { method: "POST", admin: true,
        body: { name: name.trim(), query: viewQueryOf(window.location.search) } });
      toast.success(t("viewSaved"));
      setName("");
      await qc.invalidateQueries({ queryKey: KEY });
    } catch (e) {
      toast.error(t("viewFailed", errorText(e)));
    }
  };
  const remove = async () => {
    if (!picked) return;
    try {
      await apiSend(`/api/admin/overview/views/${encodeURIComponent(picked)}`, { method: "DELETE", admin: true });
      toast.success(t("viewDeleted"));
      setPicked("");
      await qc.invalidateQueries({ queryKey: KEY });
    } catch (e) {
      toast.error(t("viewFailed", errorText(e)));
    }
  };
  if (res.isError) return null;   // no staff id (a machine session) or the table is not there yet: no control
  return (
    <span role="group" aria-label={t("views")} style={rowWrap} data-testid="status-views">
      <Select size="sm" aria-label={t("viewPick")} value={picked} onChange={(e) => open(e.target.value)}
        placeholder={views.length ? t("viewPick") : t("viewNone")} style={{ width: "auto", minWidth: 160 }}
        options={views.map((v) => ({ value: v.id, label: v.name }))} />
      {picked && <Button size="sm" variant="ghost" onClick={remove}>{t("viewDelete")}</Button>}
      <Input size="sm" aria-label={t("viewName")} placeholder={t("viewName")} value={name} maxLength={80}
        onChange={(e) => setName(e.target.value)} style={{ width: 150 }} />
      <Button size="sm" variant="secondary" disabled={!name.trim()} onClick={save}>{t("viewSave")}</Button>
    </span>
  );
}

/** The auto refresh chip: "Auto 60 s" with Pause, or why it is paused with Resume (not for a custom range). */
export function RefreshChip({ pause, seconds, onToggle }: {
  pause: PauseReason; seconds: number; onToggle: () => void;
}) {
  const t = useST();
  const text = pause === "custom" ? t("pausedCustom") : pause === "hidden" ? t("pausedHidden")
    : pause === "paused" ? t("pausedManual") : t("autoOn", seconds);
  return (
    <span style={rowWrap} data-testid="status-refresh-chip" aria-live="polite">
      <span style={small}>{text}</span>
      {pause !== "custom" && (
        <ChipButton size="sm" selected={pause === "paused"} onClick={onToggle}>
          {pause === "paused" ? t("resume") : t("pause")}</ChipButton>)}
    </span>
  );
}

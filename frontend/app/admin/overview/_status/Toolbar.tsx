"use client";
// app/admin/overview/_status/Toolbar.tsx — AA-1140: the page controls of the Ecosystem status page (mockup v2 sticky
// bar): time range (presets or a custom from / to on the viewer's clock, at most cis.ops.status_history_days back),
// compare with the previous period, the app filter (every section but shared platform rows), copy link, reset view.
// The view lives in the URL (lib/opsStatus.ts parseView / viewParams), so a copied link opens the same view.

import { useState } from "react";
import { Button, Input, K, SegmentedControl, Switch, useFormat } from "../../../_kit";
import {
  APP_LABEL, RANGE_KEYS, STATUS_APPS, customProblem, isoToLocal, localToIso, type RangeInfo, type RangeKey,
  type View,
} from "@/lib/opsStatus";
import { ChipFilter, rowWrap, small, useST, type ST } from "./ui";

type SetView = (v: Partial<View>) => void;

function stepText(stepS: number): string {
  if (stepS >= 3600) return `${stepS / 3600} h`;
  return `${stepS / 60} min`;
}

function problemText(p: string, historyDays: number | null, t: ST): string {
  if (p === "tooOld") return t("probtooOld", historyDays ?? 0);
  return t(`prob${p}` as Parameters<ST>[0]) as string;
}

function CustomRange({ view, historyDays, onApply, onCancel }: {
  view: View; historyDays: number | null; onApply: (from: string, to: string) => void; onCancel: () => void;
}) {
  const t = useST();
  const [from, setFrom] = useState(isoToLocal(view.from));
  const [to, setTo] = useState(isoToLocal(view.to));
  const [now] = useState(() => Date.now()); // the clock when the picker opened
  // before the first answer the history setting is unknown: the server checks the range (422 bad_range)
  const problem = customProblem(localToIso(from), localToIso(to), historyDays ?? Infinity, now);
  return (
    <div role="group" aria-label={t("rcustom")} style={{ ...rowWrap, padding: "8px 0" }} data-testid="status-custom">
      <label style={{ ...rowWrap, gap: 6 }}><span style={small}>{t("from")}</span>
        <Input size="sm" type="datetime-local" step={60} value={from} onChange={(e) => setFrom(e.target.value)}
          style={{ width: 200 }} /></label>
      <label style={{ ...rowWrap, gap: 6 }}><span style={small}>{t("to")}</span>
        <Input size="sm" type="datetime-local" step={60} value={to} onChange={(e) => setTo(e.target.value)}
          style={{ width: 200 }} /></label>
      <Button size="sm" disabled={problem !== null} onClick={() => onApply(localToIso(from), localToIso(to))}>
        {t("apply")}</Button>
      <Button size="sm" variant="ghost" onClick={onCancel}>{t("cancel")}</Button>
      {problem && problem !== "missing" && (
        <span role="status" style={{ ...small, color: K.amber }}>{problemText(problem, historyDays, t)}</span>)}
      {historyDays != null && <span style={small}>{t("historyNote", historyDays)}</span>}
    </div>
  );
}

function RangeRead({ range }: { range: RangeInfo | undefined }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  if (!range) return null;
  return (
    <span style={small} aria-live="polite" data-testid="status-range-read">
      {t("showing", formatDateTime(range.start), formatDateTime(range.end), stepText(range.step_s))}
    </span>
  );
}

function CopyLink() {
  const t = useST();
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(window.location.href).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  };
  return <Button size="sm" variant="secondary" onClick={copy}>{copied ? t("copied") : t("copyLink")}</Button>;
}

export function Toolbar({ view, setView, range, historyDays, dirty, onReset }: {
  view: View; setView: SetView; range: RangeInfo | undefined; historyDays: number | null; dirty: boolean;
  onReset: () => void;
}) {
  const t = useST();
  const [custom, setCustom] = useState(false);
  const pick = (r: RangeKey) => {
    if (r === "custom") return setCustom(true);
    setCustom(false);
    setView({ range: r, from: "", to: "" });
  };
  const apps = STATUS_APPS.map((a) => ({ value: a, label: APP_LABEL[a] }));
  return (
    <div data-testid="status-toolbar" role="region" aria-label={t("timeRange")}
      style={{ position: "sticky", top: 0, zIndex: 5, background: K.bg, padding: "8px 0", display: "grid", gap: 8,
        borderBottom: `1px solid ${K.line}` }}>
      <div style={rowWrap}>
        <SegmentedControl<RangeKey> label={t("timeRange")} size="sm" testId="status-range"
          value={custom ? "custom" : view.range} onChange={pick}
          options={RANGE_KEYS.map((r) => ({ value: r, label: t(`r${r}` as Parameters<ST>[0]) as string }))} />
        <span style={{ ...rowWrap, gap: 6 }} data-testid="status-compare">
          <Switch checked={view.compare} onChange={(c) => setView({ compare: c })} label={t("compare")} />
          <span style={small}>{t("compare")}</span>
        </span>
        <RangeRead range={range} />
      </div>
      {custom && (
        <CustomRange view={view} historyDays={historyDays} onCancel={() => setCustom(false)}
          onApply={(from, to) => { setCustom(false); setView({ range: "custom", from, to }); }} />
      )}
      <div style={rowWrap}>
        <ChipFilter label={t("apps")} options={apps} value={view.apps} onChange={(a) => setView({ apps: a })}
          testId="status-apps" />
        {view.apps.length > 0 && <span style={small}>{t("filteredNote")}</span>}
        <span style={{ ...rowWrap, marginLeft: "auto" }}>
          {dirty && <Button size="sm" variant="ghost" onClick={onReset}>{t("resetView")}</Button>}
          <CopyLink />
        </span>
      </div>
    </div>
  );
}

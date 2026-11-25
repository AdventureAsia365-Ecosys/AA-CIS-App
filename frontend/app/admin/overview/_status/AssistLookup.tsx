"use client";
// app/admin/overview/_status/AssistLookup.tsx — AA-1154 (A1): the assistant's look-up panel on the Ecosystem status
// page: "What changed in the last …?", over the windows of the setting cis.ops.assist_lookup_windows_min (the API
// lists them in its answer; the page has no window list of its own). The answer comes from the records first
// (GET /api/admin/ops-assist/changes: deploys, settings, migrations, waves, route versions, alarms and service drops;
// no AI call); "Phrase with AI" asks the AI gateway (stage ops_assist, its own month cap) to word the same rows, and
// the page shows which rows it cited; a read-only session gets no Phrase button (the API refuses its POSTs). Kit
// components only (ADR 0016).

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button, ErrorState, K, SegmentedControl, WriteOnly, apiGet, apiSend, useFormat, useT } from "../../../_kit";
import { changesPath, costText, windowWords, type ChangesAnswer } from "@/lib/opsAssist";
import { ASSIST } from "../assist.messages";
import { useMayAsk } from "./useMayAsk";
import { rowWrap, small } from "./ui";

/** "hour", "6 hours", "30 minutes": a window after "What changed in the last". */
function useWindowLabel(): (minutes: number) => string {
  const t = useT(ASSIST);
  return (minutes) => {
    const w = windowWords(minutes);
    if (w.unit === "hour") return t("wHour");
    return w.unit === "hours" ? t("wHours", w.n) : t("wMinutes", w.n);
  };
}

function Phrased({ data }: { data: ChangesAnswer }) {
  const t = useT(ASSIST);
  const { formatDateTime } = useFormat();
  if (!data.ai_answer) {
    if (!data.llm?.error || data.llm.error === "nothing_to_phrase") return null;
    return <span style={small} data-testid="assist-look-ai-error">{t("llmFailed", data.llm.error)}</span>;
  }
  const rows = new Map([...data.changes, ...data.events].map((r) => [r.id, r]));
  return (
    <div data-testid="assist-look-ai" style={{ display: "grid", gap: 4, padding: 8, borderRadius: 6,
      background: K.accentSoft, minWidth: 0 }}>
      <span style={{ ...small, fontWeight: 700 }}>{t("lookAi")}</span>
      <p style={{ margin: 0, fontSize: 13, color: K.ink }}>{data.ai_answer.text}</p>
      <span style={small}>
        {data.ai_answer.cited.map((id) => rows.get(id)?.title ?? id).join(" · ")}
        {data.llm?.model ? ` · ${data.llm.model}, ${costText(data.llm.cost_usd)}` : ""}
        {data.llm?.analysed_at ? `, ${formatDateTime(data.llm.analysed_at)}` : ""}
      </span>
    </div>
  );
}

function Answer({ data }: { data: ChangesAnswer }) {
  const t = useT(ASSIST);
  const missing = Object.keys(data.unavailable ?? {});
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="assist-look-answer">
      <span style={{ fontWeight: 600, color: K.ink }}>{data.answer.headline}</span>
      {data.answer.lines.map((l, i) => <span key={i} style={{ ...small, overflowWrap: "anywhere" }}>{l}</span>)}
      <span style={small}>
        {t("lookFromRecords")} {missing.length > 0 && t("unavailable", missing.join(", "))}{" "}
        <Link href="/admin/overview" style={{ color: K.accentDeep, fontWeight: 600 }}>{t("evidence")}</Link>
      </span>
    </div>
  );
}

/** The records of the chosen window (null = the API's default) and the AI wording of the same rows on request. The
 * choices come from the answer (the setting), never from a page list; a read-only session never asks the AI. */
function useLookup() {
  const mayAsk = useMayAsk();
  const [chosen, setChosen] = useState<number | null>(null);
  const [phrase, setPhrase] = useState<number | null>(null);
  const records = useQuery({
    queryKey: ["ops-assist-changes", chosen],
    queryFn: () => apiGet<ChangesAnswer>(changesPath(chosen), { admin: true }),
    placeholderData: (prev) => prev,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const win = chosen ?? records.data?.minutes ?? null;
  const ai = useQuery({
    queryKey: ["ops-assist-changes-ai", phrase],
    queryFn: () => apiSend<ChangesAnswer>(changesPath(phrase, true), { method: "POST", admin: true }),
    enabled: mayAsk && phrase !== null && phrase === win,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  return { records, ai, win, windows: records.data?.windows ?? [], setChosen, phrased: phrase === win,
    ask: () => setPhrase(win) };
}

/** The title, the window choices (from the API) and Phrase with AI (hidden for a read-only session). */
function LookHeader({ win, windows, onPick, busy, onPhrase }: {
  win: number | null; windows: number[]; onPick: (m: number) => void; busy: boolean; onPhrase: () => void;
}) {
  const t = useT(ASSIST);
  const label = useWindowLabel();
  return (
    <div style={rowWrap}>
      <span style={{ fontWeight: 700, color: K.ink }}>{t("lookTitle")}</span>
      <span style={small}>{t("lookQuestion")}</span>
      {win !== null && windows.length > 0 && (
        <SegmentedControl<string> label={t("lookQuestion")} size="sm" value={String(win)}
          onChange={(v) => onPick(Number(v))}
          options={windows.map((m) => ({ value: String(m), label: label(m) }))} testId="assist-look-window" />
      )}
      <WriteOnly>
        <Button size="sm" variant="ghost" disabled={busy || win === null} onClick={onPhrase}
          testId="assist-look-phrase">{t("lookPhrase")}</Button>
      </WriteOnly>
    </div>
  );
}

/** "What changed in the last …?" from the records, with an optional AI wording of the same rows. */
export function AssistLookup() {
  const t = useT(ASSIST);
  const { records, ai, win, windows, setChosen, phrased, ask } = useLookup();
  return (
    <div data-testid="assist-lookup" style={{ display: "grid", gap: 8, padding: "10px 12px", minWidth: 0,
      border: `1px solid ${K.line}`, borderRadius: 8, background: K.card }}>
      <LookHeader win={win} windows={windows} onPick={setChosen} busy={ai.isFetching || !records.data}
        onPhrase={ask} />
      {records.isLoading && <span style={small}>{t("lookLoading")}</span>}
      {records.isError && <ErrorState message={t("lookFailed")} onRetry={() => records.refetch()} />}
      {records.data && <Answer data={records.data} />}
      {ai.isFetching && <span style={small}>{t("aiRunning")}</span>}
      {ai.data && phrased && <Phrased data={ai.data} />}
    </div>
  );
}

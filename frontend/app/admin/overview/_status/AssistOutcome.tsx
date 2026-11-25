"use client";
// app/admin/overview/_status/AssistOutcome.tsx — AA-1154 (A2): predicted against measured in the assistant panel of
// the Ecosystem status page. Each action card lists what is checked after it is done (its measures and the predicted
// direction); "I ran this" marks the card as done once the person ran its API call or runbook elsewhere (POST
// /api/admin/ops-assist/runs: the server rebuilds the card, reads the baselines and the status sampler checks the
// measures at the minutes of cis.ops.assist_check_after_min); the card then shows each checkpoint (right, wrong,
// unclear). Only a run recorded within the card's lifetime counts: the same card comes back when the item recurs, and
// then offers "I ran this" again with the earlier run as one line of history. The track record line shows the hit rate
// of the last cis.ops.assist_digest_days days (GET .../track), the same paragraph the OPS-12 digest carries. Nothing
// here runs an action, and a read-only session (a viewer, view-as, the Dev E2E identity) gets no button (the API
// refuses its POSTs). Kit components only (ADR 0016).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, K, WriteOnly, apiGet, apiSend, useFormat, useT,
} from "../../../_kit";
import {
  FETCH_TIMEOUT_MS, RUNS_PATH, TRACK_PATH, assistRange, currentRun, expired, pendingChecks, runOfCard, runsPath,
  verdictTone,
  withTimeout, type ActionCard, type AssistRun, type Expect, type RunsAnswer, type TrackAnswer, type Verdict,
} from "@/lib/opsAssist";
import { ASSIST } from "../assist.messages";
import { useMayAsk } from "./useMayAsk";
import { rowWrap, small } from "./ui";

const EXPECT_KEY: Record<Expect, "expZero" | "expNonzero" | "expDown" | "expUp"> = {
  zero: "expZero", nonzero: "expNonzero", down: "expDown", up: "expUp",
};
const VERDICT_KEY: Record<Verdict, "vhit" | "vmiss" | "vunclear"> = { hit: "vhit", miss: "vmiss", unclear: "vunclear" };

/** The runs of one item (cards marked as done), refreshed while the drawer is open so the checks appear. */
export function useRuns(itemId: string, open: boolean) {
  const t = useT(ASSIST);
  return useQuery({
    queryKey: ["ops-assist-runs", itemId],
    queryFn: () => withTimeout(apiGet<RunsAnswer>(runsPath(itemId), { admin: true }), FETCH_TIMEOUT_MS, t("timeout")),
    enabled: open,
    staleTime: 60_000,
    refetchInterval: 300_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

function Checks({ run }: { run: AssistRun }) {
  const t = useT(ASSIST);
  const { formatDateTime } = useFormat();
  const pending = pendingChecks(run);
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid={`assist-run-${run.action}`}>
      <span style={small}>{t("doneAt", formatDateTime(run.recorded_at))}</span>
      {run.checks.map((c) => (
        <div key={c.after_min} style={rowWrap}>
          <Badge tone="neutral" dot={false}>{t("checkAt", c.after_min)}</Badge>
          <Badge tone={verdictTone(c.verdict)}>{t(VERDICT_KEY[c.verdict])}</Badge>
          {c.late && <span style={small}>{t("vlate")}</span>}
          <span style={small}>
            {c.measures.map((m, i) => `${run.measures[i]?.label ?? ""}: ${m.value ?? "?"}`).join(" · ")}
          </span>
        </div>
      ))}
      {pending.length > 0 && (
        <span style={small}>{t("checkPending", pending.map((n) => t("checkAt", n)).join(", "))}</span>)}
    </div>
  );
}

/** "I ran this": records the card as done (no action is run from here). */
function DoneButton({ card, itemId, range }: { card: ActionCard; itemId: string; range: string }) {
  const t = useT(ASSIST);
  const qc = useQueryClient();
  const mark = useMutation({
    mutationFn: () => withTimeout(apiSend(RUNS_PATH, {
      method: "POST", admin: true, body: { item_id: itemId, card_id: card.id, range: assistRange(range) },
    }), FETCH_TIMEOUT_MS, t("timeout")),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ops-assist-runs", itemId] });
      qc.invalidateQueries({ queryKey: ["ops-assist-track"] });
    },
  });
  return (
    <div style={{ display: "grid", gap: 2 }}>
      <div style={rowWrap}>
        <Button size="sm" variant="secondary" disabled={mark.isPending || expired(card, new Date())}
          onClick={() => mark.mutate()} testId={`assist-done-${card.action}`}>{t("markDone")}</Button>
        <span style={small}>{t("markDoneHint")}</span>
      </div>
      {mark.isError && <span style={{ ...small, color: K.danger }}>{(mark.error as Error).message}</span>}
    </div>
  );
}

/** The run of an earlier incident of the same card, in one line (its checks were shown back then). */
function LastRun({ run }: { run: AssistRun }) {
  const t = useT(ASSIST);
  const { formatDateTime } = useFormat();
  return (
    <div style={rowWrap} data-testid={`assist-last-run-${run.action}`}>
      <span style={small}>{t("lastRun", formatDateTime(run.recorded_at))}</span>
      {run.verdict && <Badge tone={verdictTone(run.verdict)}>{t(VERDICT_KEY[run.verdict])}</Badge>}
    </div>
  );
}

/** Under a card: what is checked after it is done, then either its checks (a run within this card's lifetime) or
 * "I ran this", with the last run of an earlier incident of the same card as history. */
export function CardOutcome({ card, itemId, range, runs, ttlMin }: {
  card: ActionCard; itemId: string; range: string; runs: AssistRun[]; ttlMin: number;
}) {
  const t = useT(ASSIST);
  const mayAsk = useMayAsk();
  const measures = card.measures ?? [];
  const run = currentRun(runs, card, ttlMin);
  const last = run ? undefined : runOfCard(runs, card.id);
  return (
    <div style={{ display: "grid", gap: 6, paddingTop: 6, borderTop: `1px dashed ${K.line}` }}
      data-testid={`assist-outcome-${card.action}`}>
      {measures.length > 0 && (
        <span style={small}><b>{t("checked")}:</b>{" "}
          {measures.map((m) => `${m.label} (${t(EXPECT_KEY[m.expect])})`).join("; ")}</span>)}
      {run ? <Checks run={run} /> : mayAsk && (
        <WriteOnly><DoneButton card={card} itemId={itemId} range={range} /></WriteOnly>)}
      {last && <LastRun run={last} />}
    </div>
  );
}

/** The hit rate of the last days (the digest's paragraph); hidden while the run record is not set up. */
export function TrackRecord() {
  const t = useT(ASSIST);
  const res = useQuery({
    queryKey: ["ops-assist-track"],
    queryFn: () => withTimeout(apiGet<TrackAnswer>(TRACK_PATH, { admin: true }), FETCH_TIMEOUT_MS, t("timeout")),
    staleTime: 300_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  if (!res.data) return null;
  const d = res.data;
  return (
    <div data-testid="assist-track" style={{ ...rowWrap, padding: "6px 12px", border: `1px solid ${K.line}`,
      borderRadius: 8 }}>
      <span style={{ fontSize: 11, fontWeight: 700, color: K.muted, textTransform: "uppercase", letterSpacing: 0.4 }}>
        {t("trackTitle")}</span>
      {d.checked > 0 && <Badge tone="info" dot={false}>{`${d.hits} / ${d.checked}`}</Badge>}
      <span style={small}>{d.text || t("trackNone")}</span>
    </div>
  );
}

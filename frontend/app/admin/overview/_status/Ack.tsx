"use client";
// app/admin/overview/_status/Ack.tsx — AA-1141 (P1 feature 2): Ack / Snooze for one of the lengths of the setting
// cis.ops.status_snooze_choices_hours or until tomorrow / Mute until an end time on one Needs-attention item, with a
// reason of at least cis.ops.status_ack_reason_min_chars characters, and the "Acked by … at …, until …" line.
// POST /api/admin/overview/status/acks (ops.write, never the business owner: the API answers viewer.can_ack, the
// buttons show only then). Everything ends by itself at its end time; "Back to open"
// clears it now. Every write is audited by the API. Kit only (ADR 0016).

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Button, Field, Input, K, LinkButton, Modal, SegmentedControl, Textarea, apiErrorMessage, apiSend, useFormat,
  useToast,
} from "../../../_kit";
import {
  isoToLocal, localToIso, muteProblem, reasonOk, snoozeHoursOf, snoozeOptions, snoozeRequest, type AckRules,
  type AttentionItem, type SnoozeOption,
} from "@/lib/opsStatus";
import { rowWrap, small, useST, type ST } from "./ui";

type Action = "ack" | "snooze" | "mute";
export const STATUS_QUERY_KEY = "admin-ecosystem-status";

function stateWord(state: string, t: ST): string {
  if (state === "snoozed") return t("stSnoozed");
  if (state === "muted") return t("stMuted");
  return t("stAcked");
}

/** "Snoozed by Zz at 09:10, until 13:10: <reason>" under an item that is set aside; under an item that got worse
 * since it was set aside, "Worse since it was snoozed at SEV3 by Zz at 09:10: open again". */
export function AckLine({ item }: { item: AttentionItem }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  const e = item.escalated;
  if (e) {
    return (
      <span style={{ ...small, overflowWrap: "anywhere" }} data-testid={`status-att-${item.id}-escalated`}>
        {t("ackEscalated", stateWord(e.ack.state, t), e.from.toUpperCase(), e.ack.by_name ?? t("ackSomeone"),
          formatDateTime(e.ack.at))}
      </span>
    );
  }
  const a = item.ack;
  if (!a) return null;
  return (
    <span style={{ ...small, overflowWrap: "anywhere" }} data-testid={`status-att-${item.id}-ack`}>
      {t("ackLine", stateWord(a.state, t), a.by_name ?? t("ackSomeone"), formatDateTime(a.at),
        formatDateTime(a.until))}
      {a.reason ? `: ${a.reason}` : ""}
    </span>
  );
}

async function send(path: string, body: unknown): Promise<unknown> {
  return apiSend(path, { method: "POST", body, admin: true });
}

function errorText(e: unknown): string {
  const err = e as { status?: number; body?: unknown; message?: string };
  return err?.body !== undefined ? apiErrorMessage(err.body, err.status ?? 0) : err?.message ?? "error";
}

function AckDialog({ item, rules, range, onClose }: {
  item: AttentionItem; rules: AckRules; range: string; onClose: () => void;
}) {
  const t = useST();
  const toast = useToast();
  const qc = useQueryClient();
  const [action, setAction] = useState<Action>("ack");
  const options = snoozeOptions(rules.snooze_hours);
  const [snooze, setSnooze] = useState<SnoozeOption>(options[0]);
  const [now] = useState(() => Date.now());
  const [muteAt, setMuteAt] = useState(() => isoToLocal(new Date(now + 24 * 3_600_000).toISOString()));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const min = rules.reason_min_chars;
  // checked against the clock when the dialog opened; the server checks again against its own clock
  const mute = action === "mute" ? muteProblem(muteAt, now, rules.mute_max_days) : null;
  const ready = reasonOk(reason, min) && !mute && !busy;
  const save = async () => {
    const end = action === "snooze" ? snoozeRequest(snooze, Date.now(), rules.snooze_tomorrow_hour)
      : action === "mute" ? { until: localToIso(muteAt) } : {};
    setBusy(true);
    try {
      await send("/api/admin/overview/status/acks", { item_id: item.id, action, ...end, reason: reason.trim(),
        range: range === "custom" ? "24h" : range });
      toast.success(t("ackDone"));
      await qc.invalidateQueries({ queryKey: [STATUS_QUERY_KEY] });
      onClose();
    } catch (e) {
      toast.error(t("ackFailed", errorText(e)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} title={item.title} testId={`status-ack-dialog-${item.id}`}
      footer={<span style={rowWrap}>
        <Button variant="ghost" onClick={onClose}>{t("cancel")}</Button>
        <Button disabled={!ready} onClick={save} testId="status-ack-save">{busy ? t("ackSaving") : t("ackSave")}</Button>
      </span>}>
      <div style={{ display: "grid", gap: 12 }}>
        <SegmentedControl<Action> label={t("status")} size="sm" testId="status-ack-action" value={action}
          onChange={setAction} options={[{ value: "ack", label: t("ack") }, { value: "snooze", label: t("snooze") },
            { value: "mute", label: t("mute") }]} />
        {action === "snooze" && (
          <SegmentedControl<SnoozeOption> label={t("snooze")} size="sm" testId="status-ack-snooze" value={snooze}
            onChange={setSnooze} options={options.map((o) => {
              const h = snoozeHoursOf(o);
              return { value: o, label: h === null ? t("snoozeTomorrow") : t("snoozeHours", h) };
            })} />
        )}
        {action === "mute" && (
          <Field label={t("muteUntil")} error={mute && mute !== "missing"
            ? (mute === "tooFar" ? t("muteProbtooFar", rules.mute_max_days) : t("muteProbpast")) : undefined}>
            <Input type="datetime-local" step={60} value={muteAt} onChange={(e) => setMuteAt(e.target.value)} />
          </Field>
        )}
        <Field label={t("reason")} required hint={`${t("reasonHint", min)} ${t("reasonCount", reason.trim().length,
          min)}`}>
          <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000}
            data-testid="status-ack-reason" />
        </Field>
      </div>
    </Modal>
  );
}

/** The ack controls of one item: "Ack / snooze / mute" opens the dialog; an item set aside offers "Back to open". */
export function AckControls({ item, rules, range, canAck }: {
  item: AttentionItem; rules: AckRules | undefined; range: string; canAck: boolean;
}) {
  const t = useST();
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  if (!canAck || !rules) return null;
  const setAside = !!item.ack;
  const clear = async () => {
    try {
      await send(`/api/admin/overview/status/acks/${encodeURIComponent(item.id)}/clear`, {});
      toast.success(t("ackCleared"));
      await qc.invalidateQueries({ queryKey: [STATUS_QUERY_KEY] });
    } catch (e) {
      toast.error(t("ackFailed", errorText(e)));
    }
  };
  return (
    <span style={rowWrap}>
      {setAside
        ? <LinkButton size="sm" onClick={clear} testId={`status-att-${item.id}-clear`}>{t("clearAck")}</LinkButton>
        : <Button size="sm" variant="secondary" onClick={() => setOpen(true)} testId={`status-att-${item.id}-ack-btn`}>
          {t("ack")} · {t("snooze")} · {t("mute")}</Button>}
      {open && <AckDialog item={item} rules={rules} range={range} onClose={() => setOpen(false)} />}
    </span>
  );
}

/** The ack trail of the range (who set what aside, until when, why; cleared ones too). */
export function AckTrail({ rows }: { rows: { id: number; item_id: string; title: string | null; state: string;
  by_name: string | null; at: string | null; until: string | null; reason: string | null; cleared_at: string | null;
  cleared_by_name: string | null }[] }) {
  const t = useST();
  const { formatDateTime } = useFormat();
  if (rows.length === 0) return null;
  return (
    <details data-testid="status-ack-trail">
      <summary style={{ ...small, cursor: "pointer" }}>{t("ackTrail", rows.length)}</summary>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18, ...small, display: "grid", gap: 4 }}>
        {rows.map((r) => (
          <li key={r.id} style={{ overflowWrap: "anywhere" }}>
            <b style={{ color: K.ink }}>{r.title ?? r.item_id}</b>{" · "}
            {t("ackLine", stateWord(r.state, t), r.by_name ?? t("ackSomeone"), formatDateTime(r.at),
              formatDateTime(r.until))}
            {r.reason ? `: ${r.reason}` : ""}
            {r.cleared_at ? ` · ${t("ackTrailCleared", r.cleared_by_name ?? t("ackSomeone"),
              formatDateTime(r.cleared_at))}` : ""}
          </li>
        ))}
      </ul>
    </details>
  );
}

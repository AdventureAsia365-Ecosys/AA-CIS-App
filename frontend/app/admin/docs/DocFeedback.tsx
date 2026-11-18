"use client";
// app/admin/docs/DocFeedback.tsx — AA-1133: "Was this page helpful?" at the end of a docs page, and the admin view of
// the answers (tech lead S283 c-s283-docs-feedback: stored in a CIS table, acp_shared.docs_feedback, CIS 454).
//
// The widget posts {repo, path, lang, helpful, comment?} to POST /api/admin/docs/feedback (BFF → backend admin_docs.py,
// docs.read; the read-only viewer and the read-only Dev E2E session are refused there, so the widget is not shown to
// them: the manifest's `can_feedback`, lib/docsFeedback.ts feedbackAllowed). One answer per person and page:
// answering again replaces the first one. "No" asks for an optional note. The path is the original page; lang says
// which language was read. The admin view (GET /api/admin/docs/feedback, ops.read) lists the pages with the most
// "No" first and their latest notes; pages the admin cannot open are counted, never named.

import { useMutation, useQuery } from "@tanstack/react-query";
import { MessageSquare, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import {
  ApiError, Badge, Button, EmptyState, ErrorState, Field, K, RADIUS, TableSkeleton, Textarea, apiGet, apiSend,
  useFormat, useT,
} from "../../_kit";
import { M } from "./messages";
import { feedbackRefusal } from "@/lib/docsFeedback";

const COMMENT_CHARS = 1000;   // the API's limit (acp_shared.docs_feedback comment check, CIS 454)

type Sent = { helpful: boolean };

export function DocFeedback({ repo, path, lang, show = true }: { repo: string; path: string; lang: string; show?: boolean }) {
  const t = useT(M);
  const [choice, setChoice] = useState<boolean | null>(null);
  const [note, setNote] = useState("");
  const [sent, setSent] = useState<Sent | null>(null);
  const send = useMutation({
    mutationFn: (body: { helpful: boolean; comment: string | null }) =>
      apiSend<{ created: boolean }>("/api/admin/docs/feedback", {
        method: "POST", admin: true, body: { repo, path, lang, helpful: body.helpful, comment: body.comment },
      }),
    onSuccess: (_d, v) => setSent({ helpful: v.helpful }),
  });

  if (!show) return null;   // a read-only session (viewer, Dev E2E): the API refuses its answer
  // A refusal in words (never the API's code); after a read-only one (an older manifest) the vote stays off.
  const refusal = send.error ? feedbackRefusal(send.error) : null;
  const busy = send.isPending || refusal === "feedbackErrReadOnly";
  const answer = (helpful: boolean) => {
    setChoice(helpful);
    if (helpful) send.mutate({ helpful, comment: null });
  };

  if (sent) {
    return (
      <div role="status" data-testid="docs-feedback-done" style={{ display: "flex", flexWrap: "wrap", alignItems: "center",
                                                                   gap: 10, fontSize: 13, color: K.ink2 }}>
        {t("feedbackThanks")}
        <Button size="sm" variant="ghost" onClick={() => { setSent(null); setChoice(null); setNote(""); }}>
          {t("feedbackChange")}
        </Button>
      </div>
    );
  }
  return (
    <section aria-label={t("helpfulQuestion")} data-testid="docs-feedback">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("helpfulQuestion")}</span>
        <Button size="sm" variant={choice === true ? "primary" : "secondary"} onClick={() => answer(true)}
                disabled={busy} testId="docs-feedback-yes">
          <ThumbsUp size={12} /> {t("helpfulYes")}
        </Button>
        <Button size="sm" variant={choice === false ? "primary" : "secondary"} onClick={() => answer(false)}
                disabled={busy} testId="docs-feedback-no">
          <ThumbsDown size={12} /> {t("helpfulNo")}
        </Button>
      </div>
      {choice === false && (
        <div style={{ marginTop: 10, display: "grid", gap: 8, maxWidth: 560 }}>
          <Field label={t("feedbackNoteLabel")}>
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value.slice(0, COMMENT_CHARS))}
              placeholder={t("feedbackNotePlaceholder")}
              rows={3}
              maxLength={COMMENT_CHARS}
              data-testid="docs-feedback-note"
            />
          </Field>
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} testId="docs-feedback-send"
                    onClick={() => send.mutate({ helpful: false, comment: note.trim() || null })}>
              {t("feedbackSend")}
            </Button>
          </div>
        </div>
      )}
      {refusal && (
        <div role="alert" data-testid="docs-feedback-error" style={{ marginTop: 8, fontSize: 12, color: K.danger }}>
          {t(refusal)}
        </div>
      )}
    </section>
  );
}

type FeedbackNote = { lang: string; helpful: boolean; comment: string; role: string | null; at: string };
type FeedbackPage = {
  repo: string; path: string; id: string; title: string; access: string; yes: number; no: number; last_at: string;
  notes: FeedbackNote[];
};
type FeedbackResp = { pages: FeedbackPage[]; hidden_pages: number; totals: { yes: number; no: number } };

export function FeedbackView({ onOpen }: { onOpen: (path: string) => void }) {
  const t = useT(M);
  const { formatDateTime } = useFormat();
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["admin", "docs", "feedback"],
    queryFn: () => apiGet<FeedbackResp>("/api/admin/docs/feedback", { admin: true }),
    staleTime: 60_000,
    retry: (count, err) => !(err instanceof ApiError && [403, 404, 503].includes(err.status)) && count < 2,
  });
  if (isLoading) return <TableSkeleton rows={6} cols={1} />;
  if (error instanceof ApiError && error.status === 503) {
    return <EmptyState icon={<MessageSquare size={28} />} title={t("feedbackTitle")} description={t("feedbackUnavailable")} />;
  }
  if (error || !data) {
    return <ErrorState message={(error as Error | null)?.message ?? t("noData")} onRetry={() => refetch()} />;
  }
  return (
    <div data-testid="docs-feedback-view">
      <h2 style={{ fontSize: 18, fontWeight: 700, margin: "0 0 4px", color: K.ink }}>{t("feedbackTitle")}</h2>
      <p style={{ fontSize: 13, color: K.muted, margin: "0 0 14px" }}>
        {t("feedbackDesc")} {t("feedbackTotals", data.totals.yes, data.totals.no)}.
        {data.hidden_pages ? t("feedbackHidden", data.hidden_pages) : ""}
      </p>
      {!data.pages.length ? (
        <EmptyState icon={<MessageSquare size={28} />} title={t("feedbackEmpty")} description={t("feedbackEmptyBody")} />
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {data.pages.map((p) => (
            <section key={`${p.repo}/${p.path}`} data-testid="docs-feedback-row"
                     style={{ border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "10px 14px" }}>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                <a
                  href={`/admin/docs?path=${encodeURIComponent(p.path)}`}
                  onClick={(e) => {
                    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                    e.preventDefault();
                    onOpen(p.path);
                  }}
                  style={{ fontWeight: 600, fontSize: 14, color: K.accentDeep, textDecoration: "none" }}
                >
                  {p.title}
                </a>
                <Badge tone="success">{t("helpfulYes")} {p.yes}</Badge>
                <Badge tone={p.no ? "danger" : "neutral"}>{t("helpfulNo")} {p.no}</Badge>
                <span style={{ fontSize: 12, color: K.muted }}>{t("feedbackLast", formatDateTime(p.last_at))}</span>
              </div>
              {p.notes.length > 0 && (
                <ul style={{ margin: "8px 0 0", paddingLeft: 18, display: "grid", gap: 4 }}>
                  {p.notes.map((n, i) => (
                    <li key={i} style={{ fontSize: 13, color: K.ink }}>
                      {n.comment}{" "}
                      <span style={{ fontSize: 11, color: K.muted }}>
                        ({t("feedbackNoteBy", n.role ?? "staff", formatDateTime(n.at))}, {n.lang.toUpperCase()})
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

/** The header button that switches /admin/docs between the reader and the admin feedback view (admins only). */
export function FeedbackToggle({ show, active, onToggle }: { show: boolean; active: boolean; onToggle: () => void }) {
  const t = useT(M);
  if (!show) return null;
  return (
    <Button variant={active ? "primary" : "ghost"} size="sm" ariaLabel={t("feedbackViewAria")}
            testId="docs-feedback-view-button" onClick={onToggle}>
      <MessageSquare size={13} /> {active ? t("backToDocs") : t("feedbackView")}
    </Button>
  );
}

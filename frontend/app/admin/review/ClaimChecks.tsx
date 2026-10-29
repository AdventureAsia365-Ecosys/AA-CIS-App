"use client";
// app/admin/review/ClaimChecks.tsx
// AA-845 — "Source check" for one review row: every grounded sentence of the version (subtitle,
// summary, highlights, itinerary) with the automated source check's confidence (Jev
// a1_claim_supported). A reviewer marks a sentence the source does not support; that one mark is the
// ground truth the check is calibrated on. Approving the version never undoes a mark, and rejecting
// the version labels nothing (a reject judges the whole version, not each sentence).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, Undo2 } from "lucide-react";
import { useState } from "react";
import { A, Btn } from "../_components/adminUi";
import { Badge, Spinner, useT, type Tone, type Translator } from "../../_kit";
import { FIELD, M } from "./messages";
import { fetchReviewClaims, labelReviewClaim, type ReviewClaim } from "./reviewApi";

type T = Translator<typeof M.en>;

function verdictBadge(c: ReviewClaim, t: T): { tone: Tone; text: string } {
  const v = c.verdict;
  if (!v) return { tone: "neutral", text: t("notChecked") };
  const p = v.probability == null ? "" : ` ${Math.round(v.probability * 100)}%`;
  if (v.zone === "accept") return { tone: "success", text: t("supported", p) };
  if (v.zone === "reject") return { tone: "danger", text: t("notSupported", p) };
  if (v.zone === "grey") return { tone: "warning", text: t("unsure", p) };
  return { tone: "neutral", text: v.zone };
}

function fieldName(c: ReviewClaim, t: T, field: Translator<typeof FIELD.en>): string {
  const k = `aa_${c.field}`;
  const base = k in FIELD.en ? field(k as keyof typeof FIELD.en) : c.field;
  return c.day != null ? t("day", base, c.day) : base;
}

export function ClaimChecks({ reviewId, canLabel }: { reviewId: string; canLabel: boolean }) {
  const qc = useQueryClient();
  const t = useT(M);
  const fieldT = useT(FIELD);
  const [showAll, setShowAll] = useState(false);
  const key = ["review-claims", reviewId];
  const { data, isLoading, isError, error } = useQuery({
    queryKey: key,
    queryFn: () => fetchReviewClaims(reviewId),
    staleTime: 30_000,
  });
  const label = useMutation({
    mutationFn: (v: { subjectKey: string; unsupported: boolean }) =>
      labelReviewClaim(reviewId, v.subjectKey, v.unsupported),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });

  const claims = data?.claims ?? [];
  // Sentences the check already calls supported are hidden by default; the reviewer's attention
  // belongs on the unsure / not-supported ones (and anything already marked).
  const visible = showAll
    ? claims
    : claims.filter((c) => c.marked_unsupported || !c.verdict || c.verdict.zone !== "accept");
  const ordered = [...visible].sort(
    (a, b) => (a.verdict?.probability ?? 2) - (b.verdict?.probability ?? 2),
  );

  return (
    <section
      aria-label={t("sourceCheck")}
      style={{ border: `1px solid ${A.line}`, borderRadius: 8, background: A.card, padding: "12px 14px", marginBottom: 16 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
        <ShieldAlert size={14} color={A.ink} />
        <strong style={{ fontSize: 13, color: A.ink }}>{t("sourceCheck")}</strong>
        {data && (
          <span style={{ fontSize: 11.5, color: A.muted }}>
            {t("checkedCount", data.with_verdict, claims.length, data.marked_unsupported)}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {claims.length > 0 && (
          <Btn variant="ghost" size="sm" onClick={() => setShowAll((s) => !s)}>
            {showAll ? t("hideSupported") : t("showAll", claims.length)}
          </Btn>
        )}
      </div>
      <p style={{ fontSize: 11.5, color: A.muted, margin: "0 0 10px", lineHeight: 1.5 }}>
        {t("sourceHelp")}
      </p>

      {isLoading && (
        <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 6, alignItems: "center" }}>
          <Spinner size={12} color="currentColor" /> {t("loadingSentences")}
        </div>
      )}
      {isError && (
        <div style={{ fontSize: 12, color: A.red }}>
          {error instanceof Error ? error.message : t("sourceLoadFailed")}
        </div>
      )}
      {data && ordered.length === 0 && (
        <div style={{ fontSize: 12, color: A.muted }}>
          {claims.length === 0 ? t("noSentences") : t("allSupported")}
        </div>
      )}

      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }}>
        {ordered.map((c) => {
          const b = verdictBadge(c, t);
          const busy = label.isPending && label.variables?.subjectKey === c.subject_key;
          const noVerdict = !c.verdict;
          return (
            <li
              key={c.subject_key}
              style={{
                display: "flex",
                gap: 10,
                alignItems: "flex-start",
                flexWrap: "wrap",
                padding: "8px 10px",
                borderRadius: 6,
                border: `1px solid ${c.marked_unsupported ? A.redBorder : A.line}`,
                background: c.marked_unsupported ? A.redSoft : A.bg,
              }}
            >
              <div style={{ flex: "1 1 260px", minWidth: 0 }}>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 4 }}>
                  <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: A.muted }}>
                    {fieldName(c, t, fieldT)}
                  </span>
                  <Badge tone={b.tone}>{b.text}</Badge>
                  {c.marked_unsupported && <Badge tone="danger" dot={false}>{t("markedUnsupported")}</Badge>}
                </div>
                <div style={{ fontSize: 12.5, color: A.body, lineHeight: 1.55, overflowWrap: "anywhere" }}>{c.sentence}</div>
              </div>
              {canLabel && (
                <div style={{ flex: "0 0 auto" }}>
                  {c.marked_unsupported ? (
                    <Btn
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => label.mutate({ subjectKey: c.subject_key, unsupported: false })}
                    >
                      {busy ? <Spinner size={12} color="currentColor" /> : <Undo2 size={12} />} {t("undo")}
                    </Btn>
                  ) : (
                    <Btn
                      variant="ghost"
                      size="sm"
                      disabled={busy || noVerdict}
                      title={noVerdict ? t("changedAfter") : undefined}
                      onClick={() => label.mutate({ subjectKey: c.subject_key, unsupported: true })}
                    >
                      {busy ? <Spinner size={12} color="currentColor" /> : <ShieldAlert size={12} />} {t("markUnsupported")}
                    </Btn>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {label.isError && (
        <div style={{ fontSize: 12, color: A.red, marginTop: 8 }}>
          {label.error instanceof Error ? label.error.message : t("labelFailed")}
        </div>
      )}
    </section>
  );
}

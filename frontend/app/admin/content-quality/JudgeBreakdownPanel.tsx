"use client";

// AA-795 — the T2 judge breakdown of one tenant tour version (tenant_tour_versions.judge_breakdown), read-only:
// the judge's scores, which number set the stored score, the judge's feedback, and the brand claims of the saved
// text that the source tour does not support. Admin-only data; the tenant never sees any of it.

import { Fragment } from "react";
import { Badge, FONT_MONO as mono, K as A, useT } from "../../_kit";
import type { JudgeBreakdown } from "./shared";
import { D } from "./drawer.messages";
import { CLAIM_KIND } from "./messages";
import { useCqText, useLookup } from "./useCqText";

const fmt = (n: number | null | undefined) => (n == null ? "—" : String(n));

/** "highlights[2]" -> "Highlight 3", "itineraries[0].body" -> "Itinerary 1", "summary" -> "Summary". `labelOf` names
 *  a known section in the UI language (useCqText().fieldLabel); an unknown one is title-cased. */
export function claimFieldLabel(field: string, labelOf: (f: string) => string = (f) => f): string {
  const m = /^([a-z_]+)(?:\[(\d+)\])?/.exec(field);
  if (!m) return field;
  const known = labelOf(m[1]);
  const name = known !== m[1] ? known : m[1].replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  return m[2] != null ? `${name} ${Number(m[2]) + 1}` : name;
}

export function JudgeBreakdownPanel({ b }: { b: JudgeBreakdown | null }) {
  const t = useT(D);
  const claimKind = useLookup(CLAIM_KIND);
  const { bindingLabel, fieldLabel } = useCqText();
  if (!b) {
    return (
      <div data-testid="cq-tv-judge-none" style={{ fontSize: 12, color: A.muted }}>{t("noJudge")}</div>
    );
  }
  const claims = b.brand_claims;
  const rows: [string, string][] = [
    [t("brandFit"), fmt(b.brand_fit)],
    [t("crossBrand"), fmt(b.cross_brand_distinct)],
    [t("missionPresent"), b.mission_present == null ? "—" : b.mission_present ? t("yes")
      : `${t("no")}${b.mission_cap != null ? t("capsAt", b.mission_cap) : ""}`],
    [t("judgeScore"), b.judge_score_raw != null ? `${fmt(b.judge_score)}${t("beforeTiebreak", fmt(b.judge_score_raw))}` : fmt(b.judge_score)],
    [t("structureCheck"), fmt(b.validate_score)],
    [t("storedScore"), `${fmt(b.final_score)}${b.score_source === "source_fallback" ? t("sourceScore") : ""}`],
  ];
  return (
    <div data-testid="cq-tv-judge" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", fontSize: 12.5, color: A.body }}>
        {t("scoreSetBy")} <Badge tone={b.binding === "unjudged" ? "neutral" : "accent"} dot={false}>{bindingLabel(b.binding)}</Badge>
        {b.gate && <span style={{ color: A.muted, fontSize: 11.5 }}>{t("gateWord")} <span style={{ fontFamily: mono }}>{b.gate}</span></span>}
        {b.attempt != null && <span style={{ color: A.muted, fontSize: 11.5 }}>{t("draftN", b.attempt + 1)}</span>}
      </div>
      {b.judged ? (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: "4px 16px", fontSize: 12, maxWidth: 420 }}>
          {rows.map(([k, v]) => (
            <Fragment key={k}><span style={{ color: A.muted }}>{k}</span><span style={{ fontFamily: mono, color: A.ink }}>{v}</span></Fragment>
          ))}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: A.muted }}>{t("notJudged", fmt(b.final_score))}</div>
      )}
      {b.feedback && (
        <div style={{ fontSize: 12.5, lineHeight: 1.55, color: A.ink, background: A.bg, border: `1px solid ${A.line}`, borderRadius: 8, padding: "8px 10px", wordBreak: "break-word" }}>
          {b.feedback}
        </div>
      )}
      {claims && (
        <div data-testid="cq-tv-claims">
          <div style={{ fontSize: 12, fontWeight: 600, color: A.ink, marginBottom: 6 }}>
            {t("claimsTitle", claims.unsupported.length)}
            <span style={{ fontWeight: 400, color: A.muted }}>{t("claimsChecked", claims.checked)}</span>
          </div>
          {claims.unsupported.length === 0 ? (
            <div style={{ fontSize: 12, color: A.muted }}>{t("noClaims")}</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {claims.unsupported.map((c, i) => (
                <div key={`${c.field}:${c.claim}:${i}`} data-testid="cq-tv-claim-row" style={{ border: `1px solid ${A.line}`, borderRadius: 8, padding: "8px 10px", background: A.card }}>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", marginBottom: 4 }}>
                    <Badge tone="warning" dot={false}>{claimKind(c.kind)}</Badge>
                    <b style={{ fontSize: 12.5, color: A.ink }}>{c.claim}</b>
                    <span style={{ fontSize: 11, color: A.muted }}>{claimFieldLabel(c.field, fieldLabel)}</span>
                    {c.in_brand_pack && <span style={{ fontSize: 11, color: A.muted }}>{t("alsoInPack")}</span>}
                  </div>
                  <div style={{ fontSize: 12.5, lineHeight: 1.55, color: A.body, wordBreak: "break-word" }}>{c.sentence}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

"use client";
// app/admin/ai-gateway/ChangeEvidenceFields.tsx — route governance (ADR 0009 decision 12, tech lead T-19, S263).
//
// A change of the models a writer or judge stage is served by (the primary model on RoutingTab, the fallback chain on
// StageRouteEditor) must name the eval run that backs it, or a break-glass reason when no eval can run. The API refuses
// the change otherwise (422) and keeps the evidence in the audit log; a break glass raises an admin bell. This block
// sits in the confirm dialog of both editors. Kit controls only.
// AA-1052 W6: the same block backs a catalog switch of a model that serves a gated stage (CatalogTable) and a hold
// switch change of a gated stage's route policy (RouteAlertsCard); a gated edit is refused (409) during a rerun wave.
// S280 (AA-1096): the eval run field says which run qualifies (a passed, completed writing-memory run, or a run without
// a regression), links to Writing memory › Releases and offers the recent qualifying runs (GET …/experiments, the
// same read as Models & trials › Experiments) as suggestions; typing an id stays possible.

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";
import { Field, Input, K, Textarea, apiGet, useT } from "../../_kit";
import { EXPERIMENTS_API, type ExperimentsResp } from "@/lib/aiGateway";
import { qualifyingEvalRuns } from "@/lib/capTighten";
import { isGatedStage } from "@/lib/editGate";
import { MODELS } from "./models.messages";

/** The API's minimum length of a break-glass reason (shared/llm_client/route_evidence.py BREAK_GLASS_MIN_CHARS). */
export const BREAK_GLASS_MIN_CHARS = 20;
const BREAK_GLASS_MAX_CHARS = 500;

export interface ChangeEvidence {
  evalRunId: string;
  breakGlass: string;
}

export const EMPTY_EVIDENCE: ChangeEvidence = { evalRunId: "", breakGlass: "" };

/** Writer and judge stages need evidence for a model change, and every stage of the gated prefixes (AA-1052 W6);
 * other validate and embed stages do not. The API's own `gated` flag on a stage row wins when it is sent. */
export function needsEvidence(role: string, stage?: string, gated?: boolean | null): boolean {
  if (typeof gated === "boolean") return gated;
  return role === "writer" || role === "judge" || (stage !== undefined && isGatedStage(stage));
}

/** True when the evidence is enough to send (the API checks the run itself). */
export function evidenceReady(e: ChangeEvidence): boolean {
  return e.evalRunId.trim() !== "" || e.breakGlass.trim().length >= BREAK_GLASS_MIN_CHARS;
}

/** The body fields of the evidence (only the ones filled in). */
export function evidenceBody(e: ChangeEvidence): { eval_run_id?: string; break_glass_reason?: string } {
  const out: { eval_run_id?: string; break_glass_reason?: string } = {};
  if (e.evalRunId.trim()) out.eval_run_id = e.evalRunId.trim();
  if (e.breakGlass.trim()) out.break_glass_reason = e.breakGlass.trim();
  return out;
}

export function ChangeEvidenceFields({ stage, value, onChange, error }: {
  stage: string;
  value: ChangeEvidence;
  onChange: (next: ChangeEvidence) => void;
  error?: string | null;
}) {
  const t = useT(MODELS);
  const listId = useId();
  const runs = useQuery({
    queryKey: ["ai-gateway", "experiments"],
    queryFn: () => apiGet<ExperimentsResp>(EXPERIMENTS_API, { admin: true }),
    staleTime: 60_000,
    retry: false,
  });
  const choices = qualifyingEvalRuns(runs.data?.eval_runs?.rows ?? []).slice(0, 20);
  return (
    <div data-testid={`llm-evidence-${stage}`} style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 12 }}>
      <div>
        <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink2 }}>{t("evidenceTitle")}</div>
        <div style={{ fontSize: 12, color: K.muted, lineHeight: 1.5 }}>{t("evidenceHint")}</div>
      </div>
      <Field label={t("evalRunId")}
        hint={<>
          {t("evalRunRule")}{" "}
          <a href="/admin/lab/writing-memory?tab=releases" target="_blank" rel="noopener noreferrer"
            style={{ color: K.accentDeep }}>{t("evalRunWhere")}</a>
          {choices.length > 0 ? ` ${t("evalRunPick", choices.length)}` : ""}
        </>}>
        <Input size="sm" data-testid={`llm-evidence-run-${stage}`} value={value.evalRunId} maxLength={64}
          list={listId} spellCheck={false} autoComplete="off"
          placeholder={t("evalRunPlaceholder")} onChange={(e) => onChange({ ...value, evalRunId: e.target.value })} />
        <datalist id={listId}>
          {choices.map((r) => (
            <option key={r.id} value={r.id}
              label={`${r.pipeline} · ${r.prompt_version}${r.writer ? ` · ${r.writer}` : ""} · ${r.verdict ?? "—"}`} />
          ))}
        </datalist>
      </Field>
      <Field label={t("breakGlass")} error={error || undefined}>
        <Textarea data-testid={`llm-evidence-reason-${stage}`} value={value.breakGlass} rows={2}
          maxLength={BREAK_GLASS_MAX_CHARS} placeholder={t("breakGlassPlaceholder")} style={{ width: "100%" }}
          onChange={(e) => onChange({ ...value, breakGlass: e.target.value })} />
      </Field>
    </div>
  );
}

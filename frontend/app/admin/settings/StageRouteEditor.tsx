"use client";
// app/admin/settings/StageRouteEditor.tsx
// AA-686 — per-stage editor for the fallback chain + shadow A/B, inside the LLM Models › Stages
// sub-tab. The primary model is still set by the existing ModelRow (PATCH /llm-config/{stage});
// this component owns the route: the ordered fallbacks and the optional shadow model
// (PATCH /llm-config/{stage}/route). A shadow doubles the judge cost, so the tech lead wants the ON/OFF
// switch + sample % directly controllable here, with the current state readable at a glance.
// AA-994: text in the UI language (models.messages.ts).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, X } from "lucide-react";
import { A, mono, sans } from "../_components/adminUi";
import { Badge, Button, ConfirmModal, apiSend, useT, useToast, alpha } from "../../_kit";
import { MODELS } from "./models.messages";
import {
  type ModelOption,
  type StageConfigRow,
  modelLabelIn,
  withVia,
} from "./llmShared";

const DEFAULT_SAMPLE_PCT = 10;
const SAMPLE_QUICK = [0, 10, 20, 50, 100];

interface RoutePatchBody {
  fallback_model_ids?: string[];
  shadow_model_id?: string | null;
  shadow_sample_pct?: number;
}

/** Options the primary + current fallbacks do not already occupy, available only. */
function addableOptions(row: StageConfigRow, fallbacks: string[]): ModelOption[] {
  const taken = new Set([row.model_id, ...fallbacks]);
  return row.options.filter((o) => o.available && !taken.has(o.model_id));
}

/** Models eligible as a shadow: available, != primary. (Fallbacks may also be a shadow.) */
function shadowOptions(row: StageConfigRow): ModelOption[] {
  return row.options.filter((o) => o.available && o.model_id !== row.model_id);
}

export function StageRouteEditor({ row }: { row: StageConfigRow }) {
  const t = useT(MODELS);
  const toast = useToast();
  const qc = useQueryClient();

  // Local draft of the route; reset on cancel and seeded from the row (the row re-renders with
  // fresh server data after a successful save invalidates the query).
  const [fallbacks, setFallbacks] = useState<string[]>(row.fallback_model_ids ?? []);
  const [shadowOn, setShadowOn] = useState<boolean>(Boolean(row.shadow_model_id));
  const [shadowModel, setShadowModel] = useState<string>(row.shadow_model_id ?? "");
  const [samplePct, setSamplePct] = useState<number>(row.shadow_sample_pct ?? 0);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");

  const addable = addableOptions(row, fallbacks);
  const shadowOpts = shadowOptions(row);
  const disabledOpts = row.options.filter((o) => !o.available);

  // Dirty = the draft differs from what the server has.
  const origFallbacks = row.fallback_model_ids ?? [];
  const sameFallbacks =
    fallbacks.length === origFallbacks.length && fallbacks.every((m, i) => m === origFallbacks[i]);
  const effShadow = shadowOn ? shadowModel : null;
  const effSample = shadowOn ? samplePct : 0;
  const dirty =
    !sameFallbacks ||
    effShadow !== (row.shadow_model_id ?? null) ||
    effSample !== (row.shadow_sample_pct ?? 0);

  const mut = useMutation({
    mutationFn: (body: RoutePatchBody) =>
      apiSend<StageConfigRow>(`/api/admin/llm-config/${row.stage}/route`, {
        method: "PATCH",
        body,
        admin: true,
      }),
    onSuccess: () => {
      toast.success(t("routeSaved", row.stage));
      setError("");
      qc.invalidateQueries({ queryKey: ["llm-config"] });
    },
    onError: (e) => {
      // Show the 422 detail inline, don't swallow it.
      setError(e instanceof Error ? e.message : t("saveFailed"));
    },
  });

  function moveFallback(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= fallbacks.length) return;
    const next = [...fallbacks];
    [next[i], next[j]] = [next[j], next[i]];
    setFallbacks(next);
  }
  function removeFallback(id: string) {
    setFallbacks((prev) => prev.filter((m) => m !== id));
  }
  function addFallback(id: string) {
    if (!id) return;
    setFallbacks((prev) => (prev.includes(id) ? prev : [...prev, id]));
  }

  function toggleShadow(on: boolean) {
    setShadowOn(on);
    if (on) {
      // Default to the first eligible shadow model + 10% if none picked yet.
      if (!shadowModel && shadowOpts.length > 0) setShadowModel(shadowOpts[0].model_id);
      if (samplePct === 0) setSamplePct(DEFAULT_SAMPLE_PCT);
    }
  }

  function cancel() {
    setFallbacks(row.fallback_model_ids ?? []);
    setShadowOn(Boolean(row.shadow_model_id));
    setShadowModel(row.shadow_model_id ?? "");
    setSamplePct(row.shadow_sample_pct ?? 0);
    setError("");
  }

  function confirmSave() {
    setConfirming(false);
    // OFF clears the shadow (null) and sample % goes to 0; ON sends the picked model + sample.
    const body: RoutePatchBody = {
      fallback_model_ids: fallbacks,
      shadow_model_id: shadowOn ? shadowModel || null : null,
      shadow_sample_pct: effSample,
    };
    mut.mutate(body);
  }

  const label = (id: string) => modelLabelIn(row.options, id);
  const shadowLabel = effShadow ? label(effShadow) : null;

  return (
    <div
      data-testid={`llm-route-editor-${row.stage}`}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        padding: "12px 14px",
        marginTop: 10,
        borderRadius: 10,
        border: `1px solid ${A.line2}`,
        background: alpha(A.accent, 3),
      }}
    >
      {/* At-a-glance state line */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
        <span style={{ fontSize: 11.5, fontWeight: 600, color: A.ink2 }}>{t("routeEditor")}</span>
        <span data-testid={`llm-shadow-state-${row.stage}`} style={{ fontSize: 11.5, color: A.muted }}>
          {effShadow ? (
            <>
              {t("shadowLabel")} <b style={{ color: A.ink2 }}>{shadowLabel}</b> · {effSample}%
            </>
          ) : (
            <>{t("shadowLabel")} <b style={{ color: A.ink2 }}>{t("off")}</b></>
          )}
        </span>
      </div>

      {/* Fallback chain */}
      <div>
        <div style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
          {t("fallbackChain")}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 9px", borderRadius: 999,
            fontSize: 11.5, border: `1px solid ${A.line}`, background: A.line2, color: A.ink2,
          }}>
            <b>{label(row.model_id)}</b>
            <span style={{ color: A.muted2 }}>{t("primary")}</span>
          </span>
          {fallbacks.map((id, i) => (
            <span
              key={id}
              data-testid={`llm-fallback-chip-${row.stage}-${id}`}
              style={{
                display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 6px 3px 9px",
                borderRadius: 999, fontSize: 11.5, border: `1px solid ${A.line}`, background: A.card, color: A.ink2,
              }}
            >
              <span aria-hidden style={{ color: A.muted2 }}>→</span>
              {label(id)}
              <button
                aria-label={t("moveUp", id)}
                onClick={() => moveFallback(i, -1)}
                disabled={i === 0 || mut.isPending}
                style={iconBtn(i === 0 || mut.isPending)}
              >
                <ArrowUp size={12} />
              </button>
              <button
                aria-label={t("moveDown", id)}
                onClick={() => moveFallback(i, 1)}
                disabled={i === fallbacks.length - 1 || mut.isPending}
                style={iconBtn(i === fallbacks.length - 1 || mut.isPending)}
              >
                <ArrowDown size={12} />
              </button>
              <button
                aria-label={t("remove", id)}
                onClick={() => removeFallback(id)}
                disabled={mut.isPending}
                style={{ ...iconBtn(mut.isPending), color: A.red }}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          {fallbacks.length === 0 && (
            <span style={{ fontSize: 11.5, color: A.muted2 }}>{t("noFallbacks")}</span>
          )}
        </div>
        {(addable.length > 0 || disabledOpts.length > 0) && (
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8 }}>
            <Plus size={12} style={{ color: A.muted2 }} />
            <select
              data-testid={`llm-fallback-add-${row.stage}`}
              defaultValue=""
              disabled={mut.isPending}
              onChange={(e) => {
                addFallback(e.target.value);
                e.currentTarget.value = "";
              }}
              style={selectStyle}
            >
              <option value="" disabled>{t("addFallback")}</option>
              {addable.map((o) => (
                <option key={o.model_id} value={o.model_id}>{withVia(o.label, o.via)}</option>
              ))}
              {disabledOpts.map((o) => (
                <option key={o.model_id} value={o.model_id} disabled>
                  {withVia(o.label, o.via)} — {o.reason ?? t("notAvailableLower")}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Shadow A/B */}
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 6 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: A.muted, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            {t("shadowAb")}
          </span>
          <button
            role="switch"
            aria-checked={shadowOn}
            aria-label={t("shadowFor", row.stage)}
            data-testid={`llm-shadow-switch-${row.stage}`}
            onClick={() => toggleShadow(!shadowOn)}
            disabled={mut.isPending || shadowOpts.length === 0}
            style={{
              display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 10px", borderRadius: 999,
              border: `1px solid ${shadowOn ? A.accentBorder : A.line}`,
              background: shadowOn ? A.accentTint : A.card, color: shadowOn ? A.accentDeep : A.muted,
              fontSize: 11.5, fontWeight: 600, cursor: shadowOpts.length === 0 ? "not-allowed" : "pointer",
              fontFamily: sans, opacity: shadowOpts.length === 0 ? 0.5 : 1,
            }}
          >
            <span style={{
              width: 8, height: 8, borderRadius: "50%", background: shadowOn ? A.accentDeep : A.muted2, display: "block",
            }} />
            {shadowOn ? t("on") : t("offUpper")}
          </button>
          <span style={{ fontSize: 11, color: A.muted2 }}>
            {t("shadowHint")}
          </span>
        </div>

        {shadowOn && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <select
              data-testid={`llm-shadow-model-${row.stage}`}
              value={shadowModel}
              disabled={mut.isPending}
              onChange={(e) => setShadowModel(e.target.value)}
              style={selectStyle}
            >
              <option value="" disabled>{t("pickShadow")}</option>
              {shadowOpts.map((o) => (
                <option key={o.model_id} value={o.model_id}>{withVia(o.label, o.via)}</option>
              ))}
            </select>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ fontSize: 11.5, color: A.muted }} htmlFor={`sample-${row.stage}`}>{t("samplePct")}</label>
              <input
                id={`sample-${row.stage}`}
                data-testid={`llm-shadow-pct-${row.stage}`}
                type="number"
                min={0}
                max={100}
                value={samplePct}
                disabled={mut.isPending}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setSamplePct(Number.isNaN(v) ? 0 : Math.max(0, Math.min(100, v)));
                }}
                style={{
                  width: 68, padding: "5px 8px", borderRadius: 7, border: `1px solid ${A.line}`,
                  fontSize: 12.5, fontFamily: mono, color: A.body, background: A.card,
                }}
              />
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              {SAMPLE_QUICK.map((p) => (
                <button
                  key={p}
                  onClick={() => setSamplePct(p)}
                  disabled={mut.isPending}
                  style={{
                    padding: "4px 9px", borderRadius: 7, fontSize: 11.5, fontFamily: mono, cursor: "pointer",
                    border: `1px solid ${samplePct === p ? A.accentBorder : A.line}`,
                    background: samplePct === p ? A.accentTint : A.card,
                    color: samplePct === p ? A.accentDeep : A.muted,
                  }}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Footer: save / cancel / error */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Button size="sm" variant="primary" disabled={!dirty || mut.isPending} onClick={() => setConfirming(true)}>
          {mut.isPending ? t("saving") : t("saveRoute")}
        </Button>
        <Button size="sm" variant="ghost" disabled={!dirty || mut.isPending} onClick={cancel}>
          {t("cancel")}
        </Button>
        {disabledOpts.length > 0 && (
          <span style={{ fontSize: 11, color: A.muted2 }}>
            {t("unavailable", disabledOpts.length)}
          </span>
        )}
        {error && (
          <span data-testid={`llm-route-error-${row.stage}`} style={{ fontSize: 11.5, color: A.red }}>
            {error}
          </span>
        )}
        <span style={{ marginLeft: "auto" }}>
          <Badge tone="neutral" dot={false}>{row.role}</Badge>
        </span>
      </div>

      <ConfirmModal
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={confirmSave}
        title={t("changeRouteTitle", row.stage)}
        confirmLabel={t("confirmChange")}
        body={
          <div style={{ fontSize: 13, color: A.body }}>
            <p style={{ marginTop: 0 }}>
              {t("changeRouteA")} <b style={{ fontFamily: mono }}>{row.stage}</b> {t("changeRouteB")}
            </p>
            <div style={{ fontSize: 12.5, color: A.ink2 }}>
              <div>{t("fallbacksLine", fallbacks.length ? fallbacks.map(label).join(" → ") : t("nonePrimaryOnly"))}</div>
              <div style={{ marginTop: 4 }}>
                {t("shadowLabel")} {effShadow ? <>{shadowLabel} · {effSample}%</> : t("off")}
                {effShadow && (
                  <span style={{ color: A.amber }}>{t("secondCall", effSample)}</span>
                )}
              </div>
            </div>
          </div>
        }
      />
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "6px 10px",
  borderRadius: 7,
  border: `1px solid ${A.line}`,
  fontSize: 12.5,
  fontFamily: sans,
  color: A.body,
  background: A.card,
  maxWidth: "100%",
  minWidth: 0,
};

function iconBtn(disabled: boolean): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    border: "none",
    background: "transparent",
    color: A.muted2,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.4 : 1,
    padding: "0 1px",
  };
}

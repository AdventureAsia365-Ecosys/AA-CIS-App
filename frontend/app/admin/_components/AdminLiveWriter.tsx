"use client";
// app/admin/_components/AdminLiveWriter.tsx
// AA-667 — admin counterpart of the portal LiveWriter (ADR 0004). Shows the live steps + the tour
// text as s1_generate writes it, for one S1 rewrite job. Polls GET /api/admin/progress/s1/{jobId}
// (~0.8s), types the new text out locally. Built on the UI kit tokens (K), not the portal T.
//
// Purely a view: the S1 page's own job poll (GET /api/admin/jobs/{id}) decides when the run is
// done. `found: false` is normal (not started / finished >1h ago / pre-feature) → quiet "Starting…".

import { Check, Loader2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { K, sans } from "../../_kit";

type Step = { key: string; label: string; state: "pending" | "active" | "done" | "skipped" | "failed" };
type Section = { key: string; label: string; text: string };
type Snapshot = {
  found: boolean; status?: string; steps?: Step[]; sections?: Section[];
  revision?: number; message?: string | null;
};

const POLL_MS = 800;

function usePrefersReducedMotion(): boolean {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setReduce(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);
  return reduce;
}

export default function AdminLiveWriter({ jobId, active = true, title = "Rewriting", compact = false }: {
  jobId: string; active?: boolean; title?: string; compact?: boolean;
}) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [shown, setShown] = useState<Record<string, string>>({});
  const targetRef = useRef<Section[]>([]);
  const reduce = usePrefersReducedMotion();
  const docRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const terminal = snap?.status === "done" || snap?.status === "failed";

  // poll
  useEffect(() => {
    if (!jobId) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const r = await fetch(`/api/admin/progress/s1/${encodeURIComponent(jobId)}`, { cache: "no-store" });
        if (r.ok) {
          const d: Snapshot = await r.json();
          if (!stop) setSnap(d);
          if (d.status === "done" || d.status === "failed") return;
        }
      } catch { /* transient — keep polling */ }
      if (!stop && active) timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [jobId, active]);

  // typewriter towards the latest sections; a new revision restarts the typed text.
  const revisionRef = useRef(0);
  const shownRef = useRef<Record<string, string>>({});
  useEffect(() => {
    targetRef.current = snap?.sections ?? [];
    const revision = snap?.revision ?? 0;
    if (revision !== revisionRef.current) {
      revisionRef.current = revision;
      shownRef.current = {};
    }
    let raf = 0;
    const step = () => {
      let pending = false;
      const next: Record<string, string> = { ...shownRef.current };
      for (const s of targetRef.current) {
        const cur = next[s.key] ?? "";
        if (reduce || !s.text.startsWith(cur)) { next[s.key] = s.text; continue; }
        if (cur.length < s.text.length) {
          const backlog = s.text.length - cur.length;
          next[s.key] = s.text.slice(0, cur.length + Math.max(2, Math.ceil(backlog / 12)));
          pending = true;
          break;
        }
      }
      shownRef.current = next;
      setShown(next);
      if (pending) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [snap, reduce]);

  useEffect(() => {
    const el = docRef.current;
    if (el && followRef.current && !terminal) el.scrollTop = el.scrollHeight;
  }, [shown, terminal]);

  const steps = (snap?.steps ?? []).filter((s) => s.state !== "skipped");
  const sections = snap?.sections ?? [];
  const lastKey = sections.length ? sections[sections.length - 1].key : null;
  const writing = !terminal && active;

  return (
    <div style={{ fontFamily: sans, display: "flex", flexDirection: "column", gap: compact ? 12 : 16, minWidth: 0 }}>
      <style>{`@keyframes lw-caret{0%,100%{opacity:1}50%{opacity:0}} @keyframes spin{to{transform:rotate(360deg)}}`}</style>

      <div aria-live="polite" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: K.muted }}>
          {title}
        </div>
        {steps.length === 0 ? (
          <StepRow state="active" label="Starting…" />
        ) : (
          steps.map((s) => <StepRow key={s.key} state={s.state} label={s.label} />)
        )}
        {snap?.status === "failed" && snap.message && (
          <div style={{ fontSize: 12.5, color: K.danger, marginTop: 2 }}>{snap.message}</div>
        )}
      </div>

      {sections.length > 0 && (
        <div
          ref={docRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
          style={{
            background: K.card, border: `1px solid ${K.line}`, borderRadius: 12,
            padding: compact ? "14px 16px" : "18px 22px",
            maxHeight: compact ? 320 : 520, overflowY: "auto",
            display: "flex", flexDirection: "column", gap: 14,
          }}
        >
          {sections.map((s) => {
            const text = shown[s.key] ?? "";
            if (!text && s.key !== lastKey) return null;
            return (
              <div key={s.key} style={{ minWidth: 0 }}>
                {s.label && (
                  <div style={{ fontSize: 10.5, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.12em", color: K.muted2, marginBottom: 5 }}>
                    {s.label}
                  </div>
                )}
                <div style={{
                  fontSize: s.key === "name" ? 18 : 13.5, fontWeight: s.key === "name" ? 600 : 400,
                  color: K.body, lineHeight: 1.65, whiteSpace: "pre-wrap", overflowWrap: "anywhere",
                }}>
                  {text}
                  {writing && s.key === lastKey && (
                    <span aria-hidden style={{
                      display: "inline-block", width: 2, height: "1.05em", background: K.accent, marginLeft: 2,
                      verticalAlign: "text-bottom", animation: reduce ? "none" : "lw-caret 1s steps(1) infinite",
                    }} />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StepRow({ state, label }: { state: Step["state"]; label: string }) {
  const icon =
    state === "done" ? <Check size={13} strokeWidth={2.6} color={K.success} /> :
    state === "failed" ? <X size={13} strokeWidth={2.6} color={K.danger} /> :
    state === "active" ? <Loader2 size={13} color={K.accent} style={{ animation: "spin 1s linear infinite" }} /> :
    <span style={{ width: 9, height: 9, borderRadius: "50%", border: `1.5px solid ${K.line}`, display: "block" }} />;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 9, fontSize: 13, color: state === "pending" ? K.muted2 : K.ink, fontWeight: state === "active" ? 600 : 400 }}>
      <span style={{ width: 16, height: 16, display: "grid", placeItems: "center", flexShrink: 0 }}>{icon}</span>
      {label}
    </div>
  );
}

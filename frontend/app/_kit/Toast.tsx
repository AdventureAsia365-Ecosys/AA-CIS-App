"use client";
// app/_kit/Toast.tsx
// AA-662 — toast notifications for the UI kit.
//
// Usage: wrap a page/section in <ToastProvider>, then call const toast = useToast(); toast.success(
// "Saved"). Toasts auto-dismiss after `duration` ms (default 4000). This is scoped per-provider so
// a page that doesn't mount it won't crash — useToast throws a clear error if used outside one.

import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { K, RADIUS, sans } from "./tokens";

type ToastTone = "success" | "error" | "info";
type ToastItem = { id: number; tone: ToastTone; message: string };

type ToastApi = {
  show: (message: string, tone?: ToastTone, duration?: number) => void;
  success: (message: string, duration?: number) => void;
  error: (message: string, duration?: number) => void;
  info: (message: string, duration?: number) => void;
};

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a <ToastProvider>");
  }
  return ctx;
}

const TONE_STYLE: Record<
  ToastTone,
  { bg: string; fg: string; border: string; icon: React.ReactNode }
> = {
  success: {
    bg: K.successSoft,
    fg: K.success,
    border: K.success,
    icon: <CheckCircle2 size={16} />,
  },
  error: {
    bg: K.dangerSoft,
    fg: K.danger,
    border: K.dangerBorder,
    icon: <XCircle size={16} />,
  },
  info: { bg: K.infoSoft, fg: K.info, border: K.info, icon: <Info size={16} /> },
};

let nextId = 1;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, tone: ToastTone = "info", duration = 4000) => {
      const id = nextId++;
      setItems((prev) => [...prev, { id, tone, message }]);
      if (duration > 0) {
        setTimeout(() => dismiss(id), duration);
      }
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (m, d) => show(m, "success", d),
      error: (m, d) => show(m, "error", d),
      info: (m, d) => show(m, "info", d),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        style={{
          position: "fixed",
          bottom: 20,
          right: 20,
          zIndex: 9999,
          display: "flex",
          flexDirection: "column",
          gap: 10,
          maxWidth: 380,
          fontFamily: sans,
        }}
      >
        {items.map((t) => {
          const s = TONE_STYLE[t.tone];
          return (
            <div
              key={t.id}
              role="status"
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                padding: "11px 13px",
                borderRadius: RADIUS.md,
                background: K.card,
                border: `1px solid ${s.border}`,
                borderLeft: `3px solid ${s.border}`,
                boxShadow: "0 6px 20px rgba(0,0,0,0.10)",
                fontSize: 13,
                color: K.body,
              }}
            >
              <span style={{ color: s.fg, flexShrink: 0, marginTop: 1 }}>{s.icon}</span>
              <span style={{ flex: 1, lineHeight: 1.4 }}>{t.message}</span>
              <button
                onClick={() => dismiss(t.id)}
                aria-label="Dismiss"
                style={{
                  border: "none",
                  background: "transparent",
                  color: K.muted2,
                  cursor: "pointer",
                  padding: 0,
                  lineHeight: 0,
                  flexShrink: 0,
                }}
              >
                <X size={15} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

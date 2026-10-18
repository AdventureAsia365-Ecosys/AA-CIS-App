"use client";
// app/_kit/SessionKeepAlive.tsx
// AA-816 (C4) — keeps a Cognito staff session alive while an admin page stays open. The access token
// in cis_admin_token lives 15 min, and the BFF proxies never refresh it themselves (refresh-token
// rotation would race across ~8 parallel calls; plan risk 8). Page navigations are covered by
// middleware.ts; this covers a page left open: it POSTs /api/auth/refresh shortly before the token
// expires, and again when the tab becomes visible (laptop sleep, background tab). The route skips the
// Cognito call while the token still has more than 3 min left. A legacy session answers
// { mode: "legacy" } and the timer stops. Renders nothing.
import { useEffect } from "react";

const MIN_DELAY_MS = 30_000;
const LEAD_MS = 120_000; // refresh about 2 min before exp (the route refreshes inside 3 min)

export function SessionKeepAlive() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let running = false;

    const schedule = (expiresAtS: number | null) => {
      if (timer) clearTimeout(timer);
      if (stopped || !expiresAtS) return;
      const delay = Math.max(MIN_DELAY_MS, expiresAtS * 1000 - Date.now() - LEAD_MS);
      timer = setTimeout(tick, delay);
    };

    async function tick() {
      if (stopped || running) return;
      running = true;
      try {
        const res = await fetch("/api/auth/refresh", { method: "POST", cache: "no-store" });
        if (res.status === 401) {
          // Session over (refresh window ended, user disabled, or token revoked): back to sign-in.
          stopped = true;
          window.location.assign("/login?error=session_expired");
          return;
        }
        const data = (await res.json().catch(() => ({}))) as { mode?: string; expiresAt?: number | null };
        if (data.mode !== "cognito") {
          stopped = true; // legacy session: nothing to keep alive
          return;
        }
        schedule(typeof data.expiresAt === "number" ? data.expiresAt : null);
      } catch {
        // Network blip: try again in a minute rather than signing the user out.
        if (timer) clearTimeout(timer);
        timer = setTimeout(tick, 60_000);
      } finally {
        running = false;
      }
    }

    const onVisible = () => {
      if (document.visibilityState === "visible") void tick();
    };

    void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  return null;
}

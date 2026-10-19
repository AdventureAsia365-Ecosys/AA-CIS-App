"use client";
// app/_kit/SessionKeepAlive.tsx
// AA-816 (C4) — keeps a Cognito staff session alive while a staff page stays open. The access token
// in cis_admin_token lives 15 min, and the BFF proxies never refresh it themselves (refresh-token
// rotation would race across ~8 parallel calls; plan risk 8). Page navigations are covered by
// proxy.ts; this covers a page left open: it POSTs /api/auth/refresh shortly before the token
// expires, and again when the tab becomes visible (laptop sleep, background tab). The route skips the
// Cognito call while the token still has more than 3 min left. Renders nothing.
// Mounted in the admin shell (AdminShell) and the portal shell ((tenant)/portal/layout.tsx), where
// admins also work. Answers from the route:
//   - "cognito"  → schedule the next refresh;
//   - "legacy"   → a legacy session, nothing to keep alive: stop;
//   - "none"     → no staff session cookie. For a tenant on /portal that is normal: stop. After this
//                  page has seen a Cognito session it means the 12 h / 1 h window ended (both cookies
//                  expire then): back to sign-in, instead of letting saves fail with 401 silently;
//   - 503        → Cognito or the backend did not answer; the session is kept: retry in a minute;
//   - 401        → the session is over: back to sign-in.
import { useEffect } from "react";

const MIN_DELAY_MS = 30_000;
const LEAD_MS = 120_000; // refresh about 2 min before exp (the route refreshes inside 3 min)
const RETRY_MS = 60_000;

export function SessionKeepAlive() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let running = false;
    let sawCognito = false;

    const later = (delay: number) => {
      if (timer) clearTimeout(timer);
      if (!stopped) timer = setTimeout(tick, delay);
    };

    const schedule = (expiresAtS: number | null) => {
      if (!expiresAtS) return later(RETRY_MS);
      later(Math.max(MIN_DELAY_MS, expiresAtS * 1000 - Date.now() - LEAD_MS));
    };

    const signInAgain = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      window.location.assign("/login?error=session_expired");
    };

    async function tick() {
      if (stopped || running) return;
      running = true;
      try {
        const res = await fetch("/api/auth/refresh", { method: "POST", cache: "no-store" });
        if (res.status === 401) {
          // Session over (refresh window ended, user disabled, or token revoked).
          signInAgain();
          return;
        }
        if (!res.ok) {
          // 503 (Cognito / backend did not answer) or any other hiccup: the session is kept.
          later(RETRY_MS);
          return;
        }
        const data = (await res.json().catch(() => ({}))) as { mode?: string; expiresAt?: number | null };
        if (data.mode === "cognito") {
          sawCognito = true;
          schedule(typeof data.expiresAt === "number" ? data.expiresAt : null);
          return;
        }
        if (data.mode === "none" && sawCognito) {
          signInAgain();
          return;
        }
        stopped = true; // legacy session, or no staff session at all (tenant): nothing to keep alive
      } catch {
        // Network blip: try again in a minute rather than signing the user out.
        later(RETRY_MS);
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

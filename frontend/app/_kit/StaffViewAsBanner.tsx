"use client";
// app/_kit/StaffViewAsBanner.tsx — AA-1157: the banner of the owner's read-only "view as a lower role" session,
// rendered by the admin shell above every page while the display hint cookie (cis_view) is set. It names the role,
// says read-only, counts down, and "Back" ends the view (DELETE /api/view-as/roles, as the owner) and returns to Roles
// & permissions with the same sign-in. It re-checks the view every 60 s (GET /api/view-as/roles) and reloads when the
// API ended it (expired, rights changed). A CIS-local file (never _kit/core): the kit core stays app-neutral.

import { useCallback, useEffect, useState } from "react";
import { Eye } from "lucide-react";
import { Button, K } from "./index";
import { useT } from "./core/useI18n";
import { formatCountdown, secondsLeft } from "@/lib/viewAs";
import { ROLES_PAGE, STAFF_VIEW_AS_API, STAFF_VIEW_DISPLAY_COOKIE, parseDisplayCookie, type StaffViewDisplay }
  from "@/lib/staffViewAs";
import { STAFF_VIEW_AS } from "./staffViewAs.messages";

const POLL_MS = 60_000;

function readDisplay(): StaffViewDisplay | null {
  if (typeof document === "undefined") return null;
  const raw = document.cookie.split(";").map((c) => c.trim()).find((c) => c.startsWith(STAFF_VIEW_DISPLAY_COOKIE + "="));
  return parseDisplayCookie(raw ? raw.slice(STAFF_VIEW_DISPLAY_COOKIE.length + 1) : null);
}

export function StaffViewAsBanner() {
  const t = useT(STAFF_VIEW_AS);
  const [view, setView] = useState<StaffViewDisplay | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const first = setTimeout(() => setView(readDisplay()), 0);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearTimeout(first); clearInterval(tick); };
  }, []);

  useEffect(() => {
    if (!view) return;
    const check = async () => {
      try {
        const r = await fetch(STAFF_VIEW_AS_API, { cache: "no-store" });
        if (!r.ok) return;
        const body = (await r.json()) as { active?: boolean };
        if (body.active === false) window.location.reload();
      } catch { /* keep the banner; the next check tries again */ }
    };
    const poll = setInterval(check, POLL_MS);
    return () => clearInterval(poll);
  }, [view]);

  const back = useCallback(async () => {
    setBusy(true);
    try { await fetch(STAFF_VIEW_AS_API, { method: "DELETE" }); } catch { /* cookies expire on their own */ }
    window.location.assign(ROLES_PAGE);
  }, []);

  if (!view) return null;
  const left = secondsLeft(view.until, now);
  return (
    <div role="status" data-testid="staff-view-as-banner"
      style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px 14px", padding: "8px 16px",
        background: K.amberSoft, borderBottom: `1px solid ${K.line}`, color: K.ink, fontSize: 13 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
        <Eye size={14} /> {t("bannerTitle", view.label)}
      </span>
      <span style={{ color: K.ink3, minWidth: 0 }}>{t("bannerReadOnly")}</span>
      <span style={{ color: K.ink3 }}>{t("bannerEndsIn", formatCountdown(left))}</span>
      <span style={{ marginLeft: "auto" }}>
        <Button variant="secondary" size="sm" onClick={back} disabled={busy}>{busy ? t("backing") : t("back")}</Button>
      </span>
    </div>
  );
}

"use client";
// portal/app/portal/_components/DisconnectConnection.tsx — AA-815
//
// "Disconnect" for a publish channel connection (WordPress / Facebook): a confirm step (kit
// ConfirmModal), then DELETE /api/tenant/v1/integrations/{wordpress|facebook}. The backend removes
// the connection row and wipes the saved credentials. Published posts stay on the channel.

import { useState } from "react";
import { ConfirmModal } from "@/app/_kit";
import { T, sans } from "./ui";

export function DisconnectConnection({ integration, label, onDisconnected }: {
  integration: "wordpress" | "facebook";
  label: string;
  onDisconnected: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function disconnect() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/tenant/v1/integrations/${integration}`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || `Could not disconnect ${label} — try again.`);
        return;
      }
      setOpen(false);
      onDisconnected();
    } catch {
      setError("Network error — please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        onClick={() => { setError(null); setOpen(true); }}
        style={{
          alignSelf: "flex-start", background: "none", border: "none", cursor: "pointer",
          fontSize: 12.5, fontWeight: 600, color: T.red, textDecoration: "underline",
          fontFamily: sans, padding: 0,
        }}
      >
        Disconnect {label}
      </button>
      <ConfirmModal
        open={open}
        onClose={() => { if (!busy) setOpen(false); }}
        onConfirm={disconnect}
        destructive
        busy={busy}
        title={`Disconnect ${label}?`}
        confirmLabel={busy ? "Disconnecting…" : "Disconnect"}
        body={
          <div style={{ fontSize: 13, lineHeight: 1.6, color: T.body }}>
            <p style={{ margin: "0 0 10px" }}>
              Your saved {label} credentials will be removed. You won&rsquo;t be able to publish to
              or unpublish from {label} until you connect it again.
            </p>
            <p style={{ margin: 0, color: T.muted }}>
              Posts already published stay live on {label}. Unpublish them first if you want them removed.
            </p>
            {error && <p style={{ margin: "10px 0 0", color: T.red }}>{error}</p>}
          </div>
        }
      />
    </>
  );
}

"use client";
// AA-638 — ⌘K / Ctrl+K command palette: jump to any portal page, or search tours by name (opens
// Browse Tours filtered, through the shell's existing globalSearch). Keyboard first: ↑/↓ to move,
// Enter to run, Esc to close.
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Search, LayoutDashboard, Globe2, BookOpen, Sparkles, CalendarRange, Eye, Send, Store, Code2,
  Activity, CreditCard, Settings, CornerDownLeft,
} from "lucide-react";
import { T, sans } from "./ui";

type Cmd = { id: string; label: string; hint?: string; icon: React.ReactNode; run: () => void };

const PAGES: { href: string; label: string; icon: React.ReactNode; keywords: string }[] = [
  { href: "/portal/dashboard", label: "Dashboard", icon: <LayoutDashboard size={15} />, keywords: "home overview usage" },
  { href: "/portal/t1-rewrite", label: "Browse Tours", icon: <Globe2 size={15} />, keywords: "pool rewrite published" },
  { href: "/portal/t4-pool", label: "My Catalog Tours", icon: <BookOpen size={15} />, keywords: "catalog versions approve export" },
  { href: "/portal/t0-brand", label: "Brand Identity", icon: <Sparkles size={15} />, keywords: "brand voice tone" },
  { href: "/portal/t7-planning", label: "Social Content", icon: <CalendarRange size={15} />, keywords: "slate posts subjects" },
  { href: "/portal/t10-review", label: "My Content", icon: <Eye size={15} />, keywords: "review posts written" },
  { href: "/portal/t11-publish", label: "Publish", icon: <Send size={15} />, keywords: "wordpress blog" },
  { href: "/portal/marketplace", label: "Marketplace", icon: <Store size={15} />, keywords: "marketplace" },
  { href: "/portal/api", label: "API Access", icon: <Code2 size={15} />, keywords: "api key docs developer" },
  { href: "/portal/activity", label: "Activity Log", icon: <Activity size={15} />, keywords: "history log" },
  { href: "/portal/billing", label: "Billing", icon: <CreditCard size={15} />, keywords: "plan invoice upgrade" },
  { href: "/portal/settings", label: "Settings", icon: <Settings size={15} />, keywords: "account sign out" },
];

export default function CommandPalette({ open, onClose, onSearchTours }: {
  open: boolean; onClose: () => void; onSearchTours: (q: string) => void;
}) {
  return open ? <Palette onClose={onClose} onSearchTours={onSearchTours} /> : null;
}

function Palette({ onClose, onSearchTours }: { onClose: () => void; onSearchTours: (q: string) => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, []);

  const cmds: Cmd[] = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const go = (href: string) => () => { onClose(); router.push(href); };
    const pages = PAGES
      .filter(p => !needle || p.label.toLowerCase().includes(needle) || p.keywords.includes(needle))
      .map(p => ({ id: p.href, label: p.label, hint: "Go to page", icon: p.icon, run: go(p.href) }));
    const search: Cmd[] = needle ? [{
      id: "search", label: `Search tours for “${q.trim()}”`, hint: "Browse Tours", icon: <Search size={15} />,
      run: () => { onClose(); onSearchTours(q.trim()); },
    }] : [];
    return [...search, ...pages];
  }, [q, onClose, onSearchTours, router]);

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setSel(s => Math.min(s + 1, cmds.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel(s => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); cmds[Math.min(sel, cmds.length - 1)]?.run(); }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="Command palette" onClick={onClose}
      style={{ position: "fixed", inset: 0, zIndex: 1000, background: "rgba(31,41,51,0.45)", display: "flex", justifyContent: "center", alignItems: "flex-start", padding: "12vh 16px 16px" }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: "min(560px, 100%)", background: T.card, borderRadius: 14, boxShadow: "0 24px 64px -12px rgba(31,41,51,0.45)",
        overflow: "hidden", fontFamily: sans, border: `1px solid ${T.line}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${T.line2}` }}>
          <Search size={16} color={T.muted2} />
          <input ref={inputRef} value={q} onChange={e => { setQ(e.target.value); setSel(0); }} onKeyDown={onKey}
            placeholder="Search tours or jump to a page…" aria-label="Command"
            style={{ flex: 1, border: "none", outline: "none", fontSize: 15, color: T.ink, fontFamily: sans, background: "transparent", minWidth: 0 }} />
          <kbd style={{ fontSize: 10.5, color: T.muted2, border: `1px solid ${T.line}`, borderRadius: 4, padding: "1px 6px", fontFamily: sans }}>Esc</kbd>
        </div>
        <div role="listbox" style={{ maxHeight: "min(420px, 60vh)", overflowY: "auto", padding: 6 }}>
          {cmds.length === 0 && <div style={{ padding: "18px 12px", fontSize: 13, color: T.muted2 }}>No matches.</div>}
          {cmds.map((c, i) => {
            const active = i === Math.min(sel, cmds.length - 1);
            return (
              <button key={c.id} role="option" aria-selected={active} onMouseEnter={() => setSel(i)} onClick={c.run}
                style={{
                  width: "100%", display: "grid", gridTemplateColumns: "20px minmax(0,1fr) auto", alignItems: "center", gap: 10,
                  padding: "10px 12px", border: "none", borderRadius: 8, cursor: "pointer", textAlign: "left", fontFamily: sans,
                  background: active ? T.goldTint : "transparent", color: T.ink,
                }}>
                <span style={{ color: active ? T.goldDeep : T.muted, display: "flex" }}>{c.icon}</span>
                <span style={{ fontSize: 13.5, fontWeight: active ? 600 : 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.label}</span>
                <span style={{ fontSize: 11.5, color: T.muted2, display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {c.hint}{active && <CornerDownLeft size={12} />}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

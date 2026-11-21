"use client";
// app/_kit/useAdminIdentity.ts
// AA-752 — read the admin role + display name from the (client-readable) cis_role / cis_user
// cookies. useSyncExternalStore so there is no setState-in-effect (React-Compiler safe) and no
// SSR/client hydration warning for the admin-only nav items: the server snapshot is the
// logged-out shape, the client reads the real cookies after hydration (useSyncExternalStore is the
// supported way to render client-only values without a hydration mismatch warning).

import { useSyncExternalStore } from "react";
import { registerSessionCapabilities } from "@/lib/rbac";
import { CAPS_COOKIE, ROLE_NAME_COOKIE } from "@/lib/staffSession";

export interface AdminIdentity {
  role: string;
  isAdmin: boolean;
  userName: string;
  /** AA-1155: the name of a custom role (cis_role_name), "" for a built-in role. */
  roleName: string;
}

function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  return (
    document.cookie
      .split(";")
      .find((c) => c.trim().startsWith(name + "="))
      ?.split("=")[1] ?? ""
  );
}

function readIdentity(): AdminIdentity {
  const role = readCookie("cis_role");
  const rawName = readCookie("cis_user");
  const userName = rawName
    ? decodeURIComponent(rawName)
    : role === "admin"
      ? "Admin"
      : "Content";
  // AA-1155: a custom role's capabilities come with the session (cis_caps, set by the server from verify-admin); can()
  // and the nav read them for that role. A UI hint only: the BFF and the API decide every call.
  const caps = decodeURIComponent(readCookie(CAPS_COOKIE)).split(",").filter(Boolean);
  if (role) registerSessionCapabilities(role, caps);
  const rawRoleName = readCookie(ROLE_NAME_COOKIE);
  return { role, isAdmin: role === "admin", userName, roleName: rawRoleName ? decodeURIComponent(rawRoleName) : "" };
}

// The cookie value is read fresh each snapshot; it only changes on login/logout (a navigation),
// so there is no live subscription to maintain beyond the no-op below.
const EMPTY = () => () => {};

const SERVER: AdminIdentity = { role: "", isAdmin: false, userName: "Content", roleName: "" };

// Cache the client snapshot so useSyncExternalStore gets a stable reference between renders
// (it compares by identity; a fresh object each call would loop).
let cached: AdminIdentity | null = null;
function clientSnapshot(): AdminIdentity {
  if (cached) return cached;
  cached = readIdentity();
  return cached;
}

export function useAdminIdentity(): AdminIdentity {
  return useSyncExternalStore(EMPTY, clientSnapshot, () => SERVER);
}

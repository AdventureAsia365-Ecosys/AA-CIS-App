// app/admin/platform/page.tsx
// AA-883 — /admin/platform has no page of its own yet: send it to the first Platform page that
// lives under this prefix (the staff directory). PAGE_RULES /admin/platform (admin only) still
// applies before this runs (proxy.ts).
import { redirect } from "next/navigation";

export default function PlatformIndex() {
  redirect("/admin/platform/staff");
}

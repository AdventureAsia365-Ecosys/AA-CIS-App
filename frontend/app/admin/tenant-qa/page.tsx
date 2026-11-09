// app/admin/tenant-qa/page.tsx — AA-1039 (S266). The Tenant content QA group has no page of its own: send it to its
// first page (Content quality, the held / flagged queue). PAGE_RULES /admin/tenant-qa (admin only) applies first.
import { redirect } from "next/navigation";

export default function TenantQaIndex() {
  redirect("/admin/tenant-qa/quality");
}

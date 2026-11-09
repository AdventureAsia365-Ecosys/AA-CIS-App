// app/admin/lab/page.tsx — AA-1039 R6 slice 3. The Writing Lab opens on its first stage (A1, the master rewrite); the
// stage bar of /admin/lab/[stage] is the only navigation between stages. PAGE_RULES /admin/lab (admin only) applies
// first.
import { redirect } from "next/navigation";
import { DEFAULT_STAGE, stageHref } from "@/lib/writingLab";

export default function WritingLabIndex() {
  redirect(stageHref(DEFAULT_STAGE));
}

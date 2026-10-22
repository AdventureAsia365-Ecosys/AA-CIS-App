// portal/app/portal/account/page.tsx — AA-759: /portal/account has no page of its own; land on Team & Settings.
import { redirect } from "next/navigation";
import { ROUTES } from "../_shell/portalNav";

export default function AccountIndexPage() {
  redirect(ROUTES.settings);
}

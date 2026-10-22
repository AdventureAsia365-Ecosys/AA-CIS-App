"use client";
// portal/app/portal/api/page.tsx — AA-430 route migration.
// AA-759: was /portal/api (308 from the old slug, next.config.ts).
// Was the "api" tab in the old portal/page.tsx. Not a T-stage (utility page, API key
// management), so no T-prefix — see AA-430 implementation notes.
import ApiTab from "../../_components/ApiTab";

export default function ApiPage() {
  return <ApiTab />;
}

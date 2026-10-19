"use client";
// portal/app/portal/api/page.tsx — AA-430 route migration.
// Was the "api" tab in the old portal/page.tsx. Not a T-stage (utility page, API key
// management), so no T-prefix — see AA-430 implementation notes.
import ApiTab from "../_components/ApiTab";

export default function ApiPage() {
  return <ApiTab />;
}

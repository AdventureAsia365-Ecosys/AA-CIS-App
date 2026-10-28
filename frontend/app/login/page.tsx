// frontend/app/login/page.tsx
// AA-816 (C4) — staff login page. A server component so it can read two server-only facts and pass
// them to the client form:
//   - LEGACY_ADMIN_LOGIN_ENABLED (default OFF since AA-816 S2): whether the username/password form is shown at all;
//   - whether Cognito sign-in works on THIS host: only allowlisted hosts (production, localhost in
//     development) have registered callback URLs, and the client secret must be configured. On a
//     Vercel preview the company-email button is shown disabled with a note. AA-911: the fixed Preview host
//     aa-cis-preview.lumiguides.it.com has company sign-in and never shows the password form.
// The page never redirects to Cognito by itself (ADR 0006 §7): after a logout, an automatic redirect
// would sign the user straight back in through the managed-login session cookie.
import { cookies, headers } from "next/headers";
import { cognitoSignInAvailable, isPreviewSignInHost, workforceSignInHost } from "@/lib/cognito";
import { legacyAdminLoginEnabled } from "@/lib/staffSession";
import { langFromCookie } from "../_kit/core/i18n";
import { LANG_COOKIE } from "../_kit/i18n";
import LoginForm from "./LoginForm";
import { LOGIN_ERRORS } from "./messages";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const host = (await headers()).get("host");
  const params = await searchParams;
  const errorKey = typeof params.error === "string" ? params.error : "";
  // AA-994: the UI language chosen in the admin (cookie `cis_lang`, absent = English), read here so the form renders
  // in it from the first paint.
  const lang = langFromCookie((await cookies()).get(LANG_COOKIE)?.value);
  return (
    <LoginForm
      legacyEnabled={legacyAdminLoginEnabled() && !isPreviewSignInHost(host)}
      cognitoAvailable={cognitoSignInAvailable(host, "cis-admin")}
      signInHost={workforceSignInHost()}
      initialError={Object.hasOwn(LOGIN_ERRORS, errorKey) ? LOGIN_ERRORS[errorKey as keyof typeof LOGIN_ERRORS] : null}
      initialLang={lang}
    />
  );
}

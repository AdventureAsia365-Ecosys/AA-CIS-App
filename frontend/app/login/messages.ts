// app/login/messages.ts — AA-994: the staff sign-in page, English and Vietnamese. The page reads the `cis_lang`
// cookie on the server (it renders per request anyway), so a Vietnamese choice made in the admin shows here at once.

import { defineMessages } from "../_kit/core/i18n";

export const M = defineMessages(
  {
    errDenied: "Sign-in was refused. Your account may have no staff access — ask an administrator.",
    errNoAccess: "Your company account has no access to CIS. Ask an administrator to grant a CIS role.",
    errSigninFailed: "Sign-in could not be completed. Please try again.",
    errSessionExpired: "Your session has ended. Please sign in again.",
    errBackend: "The CIS service could not be reached. Please try again in a moment.",
    errCognitoHost: "Company sign-in is only available on the main CIS address.",
    errCognitoConfig: "Company sign-in is not configured on this deployment.",
    enterCredentials: "Enter username and password",
    invalidCredentials: "Invalid username or password",
    networkError: "Network error — check connection",
    title: "CIS Internal",
    subtitle: "Staff sign-in",
    companySignIn: "Sign in with company email",
    companyOnlyHere: "Company sign-in works on aa-cis.lumiguides.it.com only.",
    codeByEmail: (host: string) =>
      `No password needed: a one-time sign-in code arrives by email. Sign-in opens on ${host}.`,
    orUsername: "or username",
    username: "Username",
    password: "Password",
    connecting: "Connecting...",
    login: "Login",
  },
  {
    errDenied: "Đăng nhập bị từ chối. Tài khoản của bạn có thể chưa có quyền nhân viên — hãy hỏi quản trị viên.",
    errNoAccess: "Tài khoản công ty của bạn chưa có quyền vào CIS. Hãy nhờ quản trị viên cấp một vai trò CIS.",
    errSigninFailed: "Không hoàn tất được việc đăng nhập. Vui lòng thử lại.",
    errSessionExpired: "Phiên làm việc đã kết thúc. Vui lòng đăng nhập lại.",
    errBackend: "Không kết nối được dịch vụ CIS. Vui lòng thử lại sau giây lát.",
    errCognitoHost: "Đăng nhập bằng tài khoản công ty chỉ dùng được trên địa chỉ CIS chính.",
    errCognitoConfig: "Bản triển khai này chưa cấu hình đăng nhập bằng tài khoản công ty.",
    enterCredentials: "Nhập tên đăng nhập và mật khẩu",
    invalidCredentials: "Sai tên đăng nhập hoặc mật khẩu",
    networkError: "Lỗi mạng — kiểm tra kết nối",
    title: "CIS Internal",
    subtitle: "Đăng nhập nhân viên",
    companySignIn: "Đăng nhập bằng email công ty",
    companyOnlyHere: "Đăng nhập bằng tài khoản công ty chỉ dùng được trên aa-cis.lumiguides.it.com.",
    codeByEmail: (host: string) =>
      `Không cần mật khẩu: mã đăng nhập dùng một lần sẽ gửi qua email. Trang đăng nhập mở trên ${host}.`,
    orUsername: "hoặc tên đăng nhập",
    username: "Tên đăng nhập",
    password: "Mật khẩu",
    connecting: "Đang kết nối...",
    login: "Đăng nhập",
  },
);

/** The `?error=` codes the sign-in routes redirect with, and their message keys. */
export const LOGIN_ERRORS = {
  denied: "errDenied",
  no_access: "errNoAccess",
  signin_failed: "errSigninFailed",
  session_expired: "errSessionExpired",
  backend: "errBackend",
  cognito_host: "errCognitoHost",
  cognito_config: "errCognitoConfig",
} as const satisfies Record<string, keyof typeof M.en>;

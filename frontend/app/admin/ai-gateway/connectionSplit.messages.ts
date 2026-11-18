// app/admin/ai-gateway/connectionSplit.messages.ts — AA-1113: the text of ConnectionSplitSection (AA-994: Vietnamese
// lives only in dictionary files).
import { defineMessages } from "../../_kit/core/i18n";

export const SPLIT_STATE = defineMessages(
  { ok: "Fits", over_allocated: "Over-allocated", unbounded: "A share is not enforced", crm_unknown: "CRM share unknown",
    no_provider_budget: "No provider budget" },
  { ok: "Vừa", over_allocated: "Vượt phân bổ", unbounded: "Một phần không chặn", crm_unknown: "Chưa rõ phần CRM",
    no_provider_budget: "Chưa có ngân sách nhà cung cấp" },
);

export const SPLIT = defineMessages(
  {
    title: "Connection budget split",
    help: "Each connection has one budget at its provider. CIS and the CRM each keep their own monthly cap on it and "
      + "neither reads the other's spend, so the two shares together must stay under the provider budget.",
    colConnection: "Connection",
    colProvider: "Provider budget",
    colCis: "CIS share",
    colCrm: "CRM share",
    colLeft: "Left",
    colState: "State",
    notSet: "not set",
    noCap: "no cap",
    unknown: "unknown",
    overBy: (v: string) => `over by ${v}`,
    uncapped: (apps: string) => `no cap: ${apps}`,
    alertOnly: "alert only",
    alertOnlyApps: (apps: string) => `alert only, calls go on past it: ${apps}`,
    editProvider: "Provider budgets",
    editCrm: "CRM shares",
    crmError: (code: string) => `The CRM share could not be read (${code}); CIS alone is checked against the budget.`,
    stray: (keys: string) => `Setting entries that name no connection: ${keys}`,
    sourceError: "The provider budget setting could not be read; nothing is checked against it.",
    policyError: "The connections' stop policy could not be read; every CIS share is shown as alert only.",
    loadError: "Could not load the connection budget split",
    empty: "No LLM connection",
  },
  {
    title: "Phân bổ ngân sách kết nối",
    help: "Mỗi kết nối có một ngân sách tại nhà cung cấp. CIS và CRM mỗi bên giữ hạn mức tháng riêng trên kết nối đó và "
      + "không bên nào đọc chi tiêu của bên kia, nên hai phần cộng lại phải nằm dưới ngân sách nhà cung cấp.",
    colConnection: "Kết nối",
    colProvider: "Ngân sách nhà cung cấp",
    colCis: "Phần CIS",
    colCrm: "Phần CRM",
    colLeft: "Còn lại",
    colState: "Trạng thái",
    notSet: "chưa đặt",
    noCap: "không giới hạn",
    unknown: "chưa rõ",
    overBy: (v: string) => `vượt ${v}`,
    uncapped: (apps: string) => `không giới hạn: ${apps}`,
    alertOnly: "chỉ cảnh báo",
    alertOnlyApps: (apps: string) => `chỉ cảnh báo, vẫn gọi tiếp khi vượt: ${apps}`,
    editProvider: "Ngân sách nhà cung cấp",
    editCrm: "Phần của CRM",
    crmError: (code: string) => `Không đọc được phần của CRM (${code}); chỉ kiểm phần CIS với ngân sách.`,
    stray: (keys: string) => `Mục cài đặt không khớp kết nối nào: ${keys}`,
    sourceError: "Không đọc được cài đặt ngân sách nhà cung cấp; không kiểm gì theo nó.",
    policyError: "Không đọc được chính sách chặn của các kết nối; mọi phần CIS được coi là chỉ cảnh báo.",
    loadError: "Không tải được phân bổ ngân sách kết nối",
    empty: "Chưa có kết nối LLM",
  },
);

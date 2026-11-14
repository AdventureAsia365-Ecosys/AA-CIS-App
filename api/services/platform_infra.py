"""AA-1096 (S279 thresholds audit, section 4, lane L6) — Platform › Infra: the limits kept in Terraform, read-only.

The audit keeps a narrow set of limits in Terraform on purpose: WAF rate rules and the geo allow-list, API Gateway
throttles, Lambda reserved concurrency, the acc2 AWS Budgets and the alarm thresholds. They are AWS edge, capacity and
monitoring resources; the app roles may never write them (no ``wafv2:UpdateWebACL``, no API Gateway or Budgets write),
so a change is a reviewed AA-CIS-Infra pull request. The admin-editable layer sits above them (Platform › Settings, the
Spend page). This page lets the administrator read the Terraform layer, with what each value does and where it is
changed, without the AWS console.

Source (no new AWS access, no Terraform state read): the AA-CIS-Infra root output ``platform_limits``
(``accounts/aa365/outputs_platform_limits.tf``) is rendered by its docs generator into the section "Edge, budget and
alarm limits" of the generated root page ``docs/generated/alarms.md`` (catalog id OPS-07). The root workflow Docs sync
publishes that page to the docs reader bundle, which the CIS API already reads (``api/services/docs_reader``, staff
tier, cached 5 min). This module reads that one page by id and parses the section's tables. So the values shown are
those of the last Terraform docs run that was merged into the root repo: ``doc.terraform_sha`` names the AA-CIS-Infra
commit, ``doc.generated_at`` the bundle time.

``GET /admin/platform/infra`` (api/routers/admin_platform_ops.py, ops.read, admin only in the BFF) answers::

    {"doc": {"missing": false, "reason": null, "id", "path", "title", "generated_at", "source": {"repo", "sha"},
             "terraform_sha"},
     "groups": [{"group", "title": {en, vi}, "what": {en, vi}, "change": {en, vi}, "editable": {en, vi} | null,
                 "editable_href": "/admin/..." | null,
                 "items": [{"key", "value", "unit", "source", "label": {en, vi}, "known"}]}],
     "missing_keys": ["<group>.<key>", ...]}

``doc.missing`` with ``reason`` "not_published" (the page or its section is not in the bundle yet), "unavailable"
(storage error) or "owner_tier" (the page is not staff tier: never read here) leaves ``groups`` empty. A group or key
of the output that this catalog does not know is still shown (``known`` false, a generic explanation), so a new
Terraform value never disappears; ``missing_keys`` lists catalog keys the page no longer has (renamed or removed in
Terraform: update :data:`CATALOG`). Values are strings exactly as the generator wrote them (numbers, "on" / "off",
lists joined by ", "); they are values only, never an ID, ARN or host name (the output's rule; the bundle builder and
the reader redact account ids again). $0: one S3 read through the docs reader cache.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from api.services import docs_reader

PAGE_ID = "OPS-07"
PAGE_REPO = "AA-Ecosys"
SECTION = "Edge, budget and alarm limits"
OUTPUT_FILE = "accounts/aa365/outputs_platform_limits.tf"

_ROW = re.compile(r"^\|\s*`([^`|]+)`\s*\|\s*`([^`|]*)`\s*\|\s*$")
_TF_SHA = re.compile(r"AA-CIS-Infra@([0-9a-f]{7,40})\b")
_H2 = re.compile(r"^##\s+(.+?)\s*$")
_H3 = re.compile(r"^###\s+(.+?)\s*$")

# Units: the page translates these codes (frontend app/admin/platform/infra/messages.ts).
UNITS = ("per_5min_ip", "per_5min", "rps", "requests", "executions", "usd_month", "usd", "percent", "seconds",
         "bytes", "connections", "count", "switch", "countries", "alerts", "text")


@dataclass(frozen=True)
class Limit:
    """One value of the output: where it is set in AA-CIS-Infra, its unit and its plain label."""

    source: str
    unit: str
    en: str
    vi: str


def _v(name: str) -> str:
    return f"accounts/aa365 variable {name}"


CATALOG: dict[str, Limit] = {
    # ── WAF (requests per 5 minutes per client IP unless said otherwise) ──
    "waf.alb_acl_rate_limit_per_5min_per_ip": Limit(
        _v("waf_rate_limit"), "per_5min_ip",
        "Rate limit of the load balancer and the CIS / Booking API stages: requests one client IP may send in 5 "
        "minutes before AWS WAF blocks it (the NAT address of the apps is exempt)",
        "Giới hạn tốc độ của load balancer và các stage API CIS / Booking: số request một IP được gửi trong 5 phút "
        "trước khi AWS WAF chặn (địa chỉ NAT của các ứng dụng được miễn)"),
    "waf.staff_web_rate_limit_per_5min_per_ip": Limit(
        _v("staff_web_rate_limit"), "per_5min_ip",
        "Rate limit of the staff web apps (CIS admin, Booking admin, CRM) per client IP",
        "Giới hạn tốc độ của các ứng dụng web cho nhân viên (CIS admin, Booking admin, CRM) theo mỗi IP"),
    "waf.tripplanner_web_rate_limit_per_5min": Limit(
        _v("tripplanner_web_rate_limit"), "per_5min_ip",
        "Rate limit of the TripPlanner website per visitor IP (every page and API call)",
        "Giới hạn tốc độ của website TripPlanner theo IP khách (mọi trang và lời gọi API)"),
    "waf.tripplanner_web_model_rate_limit_per_5min": Limit(
        _v("tripplanner_web_model_rate_limit"), "per_5min_ip",
        "Rate limit of the TripPlanner paths that call an AI model (narration, plan chat) per visitor IP",
        "Giới hạn tốc độ của các đường dẫn TripPlanner gọi mô hình AI (thuyết minh, chat lập kế hoạch) theo IP khách"),
    "waf.tripplanner_web_challenge_immunity_s": Limit(
        _v("tripplanner_web_challenge_immunity_s"), "seconds",
        "How long a TripPlanner visitor who passed the bot challenge is not challenged again",
        "Thời gian một khách TripPlanner đã vượt kiểm tra chống bot không bị kiểm tra lại"),
    "waf.geo_allowed_countries": Limit(
        "modules/waf variable geo_allowed_countries", "countries",
        "Countries the WAF lets through (ISO codes); requests from any other country are blocked",
        "Các quốc gia WAF cho qua (mã ISO); request từ quốc gia khác bị chặn"),
    "waf.common_rules_count_only": Limit(
        _v("waf_common_rules_count_only"), "switch",
        "On: the AWS managed common rules only count matches (observe mode); off: they block",
        "Bật: bộ luật chung do AWS quản lý chỉ đếm (chế độ quan sát); tắt: chúng chặn"),
    "waf.protects_rest_api_stages": Limit(
        _v("waf_protect_rest_api_stages"), "switch",
        "Whether the WAF also sits in front of the CIS and Booking REST API stages",
        "WAF có đứng trước các stage REST API của CIS và Booking hay không"),
    "waf.identity_pools_acl_enabled": Limit(
        _v("identity_waf_enabled"), "switch",
        "Whether the sign-in pools (staff and customer identity) have their own WAF",
        "Các pool đăng nhập (nhân viên và khách hàng) có WAF riêng hay không"),
    "waf.identity_signup_rate_limit_per_5min_per_ip": Limit(
        _v("identity_waf_signup_rate_limit"), "per_5min_ip",
        "Sign-up and password-reset requests one IP may send to the sign-in pools in 5 minutes",
        "Số request đăng ký và đặt lại mật khẩu một IP được gửi tới các pool đăng nhập trong 5 phút"),
    "waf.identity_global_rate_limit_per_5min_per_ip": Limit(
        _v("identity_waf_global_rate_limit"), "per_5min_ip",
        "All requests one IP may send to the sign-in pools in 5 minutes",
        "Tổng số request một IP được gửi tới các pool đăng nhập trong 5 phút"),
    # ── API Gateway throttles (steady requests per second, burst = short peak) ──
    "api_gateway_throttles.cis_stage_rps": Limit(
        _v("cis_api_stage_throttle_rate_limit"), "rps",
        "CIS API: steady requests per second for the whole stage", "CIS API: số request mỗi giây ổn định cho cả stage"),
    "api_gateway_throttles.cis_stage_burst": Limit(
        _v("cis_api_stage_throttle_burst_limit"), "requests",
        "CIS API: short burst allowed above the steady rate", "CIS API: mức vượt ngắn cho phép trên tốc độ ổn định"),
    "api_gateway_throttles.cis_login_rps": Limit(
        _v("cis_api_login_throttle_rate_limit"), "rps",
        "CIS API sign-in routes: steady requests per second",
        "Các route đăng nhập CIS API: số request mỗi giây ổn định"),
    "api_gateway_throttles.cis_login_burst": Limit(
        _v("cis_api_login_throttle_burst_limit"), "requests",
        "CIS API sign-in routes: short burst", "Các route đăng nhập CIS API: mức vượt ngắn"),
    "api_gateway_throttles.cis_content_rps": Limit(
        _v("cis_api_content_throttle_rate_limit"), "rps",
        "CIS content API (catalogue reads of the apps and partners): steady requests per second",
        "CIS content API (các ứng dụng và đối tác đọc catalogue): số request mỗi giây ổn định"),
    "api_gateway_throttles.cis_content_burst": Limit(
        _v("cis_api_content_throttle_burst_limit"), "requests",
        "CIS content API: short burst", "CIS content API: mức vượt ngắn"),
    "api_gateway_throttles.booking_stage_rps": Limit(
        _v("booking_throttle_rate_limit"), "rps",
        "Booking API: steady requests per second", "Booking API: số request mỗi giây ổn định"),
    "api_gateway_throttles.booking_stage_burst": Limit(
        _v("booking_throttle_burst_limit"), "requests", "Booking API: short burst", "Booking API: mức vượt ngắn"),
    "api_gateway_throttles.tripplanner_default_rps": Limit(
        _v("tripplanner_throttle_rate_limit"), "rps",
        "TripPlanner API: steady requests per second (every route without its own limit)",
        "TripPlanner API: số request mỗi giây ổn định (mọi route không có giới hạn riêng)"),
    "api_gateway_throttles.tripplanner_default_burst": Limit(
        _v("tripplanner_throttle_burst_limit"), "requests",
                                                "TripPlanner API: short burst", "TripPlanner API: mức vượt ngắn"),
    "api_gateway_throttles.tripplanner_narrate_rps": Limit(
        _v("tripplanner_narrate_throttle_rate_limit"), "rps",
        "TripPlanner AI narration route: steady requests per second (an AI cost backstop)",
        "Route thuyết minh AI của TripPlanner: số request mỗi giây ổn định (chốt chặn chi phí AI)"),
    "api_gateway_throttles.tripplanner_narrate_burst": Limit(
        _v("tripplanner_narrate_throttle_burst_limit"), "requests",
        "TripPlanner AI narration route: short burst", "Route thuyết minh AI của TripPlanner: mức vượt ngắn"),
    "api_gateway_throttles.tripplanner_search_rps": Limit(
        _v("tripplanner_search_throttle_rate_limit"), "rps",
        "TripPlanner search route: steady requests per second",
        "Route tìm kiếm của TripPlanner: số request mỗi giây ổn định"),
    "api_gateway_throttles.tripplanner_search_burst": Limit(
        _v("tripplanner_search_throttle_burst_limit"), "requests",
        "TripPlanner search route: short burst", "Route tìm kiếm của TripPlanner: mức vượt ngắn"),
    # ── Lambda reserved concurrency (the database connection budget) ──
    "lambda_reserved_concurrency.tripplanner_browse": Limit(
        _v("tripplanner_browse_reserved_concurrency"), "executions",
        "TripPlanner browse function: most copies running at once",
        "Hàm browse của TripPlanner: số bản chạy đồng thời tối đa"),
    "lambda_reserved_concurrency.tripplanner_assembly": Limit(
        _v("tripplanner_lambda_reserved_concurrency"), "executions",
        "TripPlanner trip assembly function (AI narration, plan chat): most copies running at once",
        "Hàm ráp chuyến đi của TripPlanner (thuyết minh AI, chat lập kế hoạch): số bản chạy đồng thời tối đa"),
    "lambda_reserved_concurrency.tripplanner_jobs": Limit(
        _v("tripplanner_jobs_reserved_concurrency"), "executions",
        "TripPlanner background jobs function: most copies running at once",
        "Hàm job nền của TripPlanner: số bản chạy đồng thời tối đa"),
    # ── AWS Budgets of acc2 (alerts only, nothing is stopped) ──
    "budgets.acc2_total_monthly_usd": Limit(
        _v("total_monthly_budget_usd"), "usd_month",
        "Monthly AWS budget of the main account (every app together)",
        "Ngân sách AWS hằng tháng của tài khoản chính (tất cả ứng dụng cộng lại)"),
    "budgets.acc2_total_alerts": Limit(
        "accounts/aa365 resource aws_budgets_budget.total_monthly (observability.tf)", "alerts",
        "When the total budget e-mails an alert: actual or forecast spend above a share of the budget",
        "Khi nào ngân sách tổng gửi e-mail cảnh báo: chi phí thực tế hoặc dự báo vượt một tỷ lệ của ngân sách"),
    "budgets.acc2_cost_anomaly_alert_usd": Limit(
        _v("cost_anomaly_alert_usd"), "usd",
        "AWS Cost Anomaly Detection e-mails when an unusual cost is above this amount",
        "AWS Cost Anomaly Detection gửi e-mail khi một khoản chi bất thường vượt số tiền này"),
    **{
        f"budgets.acc2_per_app_monthly_usd.{app}": Limit(
            _v("per_app_monthly_budgets"), "usd_month",
            f"Monthly AWS budget of {name} (resources tagged with that app)",
            f"Ngân sách AWS hằng tháng của {name} (tài nguyên gắn tag ứng dụng đó)")
        for app, name in (("booking", "Booking"), ("cis", "CIS"), ("crm", "the CRM"),
                          ("identity", "identity (sign-in)"),
                          ("shared", "shared resources (network, database)"), ("tripplanner", "TripPlanner"))
    },
    # ── Alarm thresholds ──
    "alarms.rds_cpu_percent_above": Limit(
        "accounts/aa365 resource aws_cloudwatch_metric_alarm.rds_cpu_high (observability.tf)", "percent",
        "The database CPU alarm rings above this share", "Cảnh báo CPU của cơ sở dữ liệu kêu khi vượt tỷ lệ này"),
    "alarms.rds_connections_above": Limit(
        "accounts/aa365 resource aws_cloudwatch_metric_alarm.rds_connections_high (observability_ecosys.tf)",
        "connections",
        "The database connections alarm rings above this count (part of the connection budget)",
        "Cảnh báo số kết nối cơ sở dữ liệu kêu khi vượt số này (một phần của ngân sách kết nối)"),
    "alarms.rds_free_storage_bytes_below": Limit(
        "accounts/aa365 resource aws_cloudwatch_metric_alarm.rds_free_storage_low (observability_ecosys.tf)", "bytes",
        "The database free storage alarm rings below this size",
        "Cảnh báo dung lượng trống của cơ sở dữ liệu kêu khi dưới mức này"),
    "alarms.crm_slo_5xx_percent": Limit(
        _v("crm_slo (five_xx_percent)"), "percent",
        "CRM availability objective: the alarm rings when server errors exceed this share of requests",
        "Mục tiêu sẵn sàng của CRM: cảnh báo kêu khi lỗi máy chủ vượt tỷ lệ này trên tổng request"),
    "alarms.crm_slo_min_requests_per_window": Limit(
        _v("crm_slo (min_requests)"), "count",
        "CRM objective: fewer requests than this in a window are not judged (too little traffic)",
        "Mục tiêu CRM: ít request hơn số này trong một khung thời gian thì không đánh giá (quá ít lưu lượng)"),
    "alarms.crm_slo_p95_latency_s": Limit(
        _v("crm_slo (p95_latency_s)"), "seconds",
        "CRM speed objective: the alarm rings when the slowest 5 % of requests take longer than this",
        "Mục tiêu tốc độ CRM: cảnh báo kêu khi 5 % request chậm nhất mất lâu hơn mức này"),
    "alarms.crm_slo_breaching_of_periods": Limit(
        _v("crm_slo (alarm_breaching, alarm_periods)"), "text",
        "CRM alarms ring only when this many periods out of the last ones breach",
        "Cảnh báo CRM chỉ kêu khi đủ số khung thời gian này vi phạm trong các khung gần nhất"),
}


def _t(en: str, vi: str) -> dict:
    return {"en": en, "vi": vi}


_CHANGE_EN = ("Read-only here. A change is a reviewed AA-CIS-Infra pull request that sets the Terraform source named "
              "on the row (" + OUTPUT_FILE + " lists them), then a Terraform apply; this page shows the new "
              "value after the next docs run of AA-CIS-Infra is merged into the root repo.")
_CHANGE_VI = ("Ở đây chỉ xem. Muốn đổi thì mở một pull request AA-CIS-Infra (có review) sửa nguồn Terraform ghi trên "
              "dòng (" + OUTPUT_FILE + " liệt kê chúng), rồi Terraform apply; trang này hiện giá trị mới sau "
              "khi lần sinh tài liệu kế tiếp của AA-CIS-Infra được merge vào repo gốc.")

# Group order on the page and its explanation. editable / editable_href: the admin-editable layer above it, if any.
GROUPS: dict[str, dict[str, Any]] = {
    "waf": {
        "title": _t("Web firewall (AWS WAF)", "Tường lửa web (AWS WAF)"),
        "what": _t("Blocks abusive traffic at the edge, before any app code runs: per-IP rate limits, the country "
                   "allow-list and the AWS managed rules.",
                   "Chặn lưu lượng lạm dụng ở rìa mạng, trước khi mã ứng dụng chạy: giới hạn tốc độ theo IP, danh sách "
                   "quốc gia cho phép và bộ luật do AWS quản lý."),
        "why": _t("Kept in Terraform: an app may never change its own firewall (no write access for the app roles).",
                  "Giữ trong Terraform: một ứng dụng không bao giờ được tự đổi tường lửa của mình (vai trò ứng dụng "
                  "không có quyền ghi)."),
        "editable": _t("The finer per-visitor limits of TripPlanner (searches, AI narrations, chats) are settings: "
                       "Platform › Settings, TripPlanner tab.",
                       "Các giới hạn chi tiết theo khách của TripPlanner (tìm kiếm, thuyết minh AI, chat) là cài đặt: "
                       "Platform › Settings, tab TripPlanner."),
        "editable_href": "/admin/platform/settings",
    },
    "api_gateway_throttles": {
        "title": _t("API Gateway throttles", "Giới hạn API Gateway"),
        "what": _t("The most requests per second each API accepts before it answers 429; the burst is a short peak "
                   "allowed above the steady rate.",
                   "Số request mỗi giây tối đa mỗi API nhận trước khi trả 429; burst là mức vượt ngắn cho phép trên "
                   "tốc độ ổn định."),
        "why": _t("Kept in Terraform: a capacity and abuse backstop sized to the database connection budget, under the "
                  "app-level limits.",
                  "Giữ trong Terraform: chốt chặn công suất và lạm dụng, tính theo ngân sách kết nối cơ sở dữ liệu, "
                  "nằm dưới các giới hạn cấp ứng dụng."),
        "editable": _t("The app-level rate limits (TripPlanner visitors, CRM public forms) are settings: Platform › "
                       "Settings.",
                       "Các giới hạn tốc độ cấp ứng dụng (khách TripPlanner, form công khai của CRM) là cài đặt: "
                       "Platform › Settings."),
        "editable_href": "/admin/platform/settings",
    },
    "lambda_reserved_concurrency": {
        "title": _t("Lambda concurrency", "Số bản chạy đồng thời của Lambda"),
        "what": _t("The most copies of each TripPlanner function that run at once; extra requests wait or fail fast.",
                   "Số bản tối đa của mỗi hàm TripPlanner chạy cùng lúc; request thêm phải chờ hoặc bị từ chối nhanh."),
        "why": _t("Kept in Terraform: each copy holds database connections, so the numbers are part of the connection "
                  "budget of the shared database.",
                  "Giữ trong Terraform: mỗi bản giữ kết nối cơ sở dữ liệu, nên các con số là một phần của ngân sách "
                  "kết nối của cơ sở dữ liệu dùng chung."),
        "editable": None,
        "editable_href": None,
    },
    "budgets": {
        "title": _t("AWS budgets (main account)", "Ngân sách AWS (tài khoản chính)"),
        "what": _t("Monthly AWS budgets and the cost anomaly alert of the main account. They only send e-mail alerts; "
                   "nothing is stopped.",
                   "Ngân sách AWS hằng tháng và cảnh báo chi phí bất thường của tài khoản chính. Chúng chỉ gửi e-mail "
                   "cảnh báo; không dừng gì cả."),
        "why": _t("Kept in Terraform: AWS Budgets are account resources the app cannot reach. The AI and LLM accounts "
                  "have their own budgets, not shown here.",
                  "Giữ trong Terraform: AWS Budgets là tài nguyên của tài khoản mà ứng dụng không chạm tới được. Các "
                  "tài khoản AI và LLM có ngân sách riêng, không hiện ở đây."),
        "editable": _t("The AI spend caps of each provider connection and tenant are editable on the Spend and AI "
                       "gateway pages.",
                       "Mức trần chi phí AI của từng kết nối nhà cung cấp và từng tenant được đổi trên trang Spend và "
                       "AI gateway."),
        "editable_href": "/admin/spend",
    },
    "alarms": {
        "title": _t("Alarm thresholds", "Ngưỡng cảnh báo"),
        "what": _t("When the monitoring alarms ring (an e-mail to the on-duty engineer): database CPU, connections and "
                   "free storage, and the CRM availability and speed objectives.",
                   "Khi nào các cảnh báo giám sát kêu (gửi e-mail cho kỹ sư trực): CPU, số kết nối và dung lượng trống "
                   "của cơ sở dữ liệu, cùng mục tiêu sẵn sàng và tốc độ của CRM."),
        "why": _t("Kept in Terraform: monitoring is defined with the resources it watches. Every alarm is listed on "
                  "the Docs page Monitoring and alarm catalog.",
                  "Giữ trong Terraform: giám sát được định nghĩa cùng tài nguyên nó theo dõi. Mọi cảnh báo được liệt "
                  "kê ở trang Docs Monitoring and alarm catalog."),
        "editable": None,
        "editable_href": None,
    },
}

_UNKNOWN_GROUP = {
    "title": None,
    "what": _t("A Terraform value group this page does not describe yet.",
               "Một nhóm giá trị Terraform mà trang này chưa mô tả."),
    "why": _t("See " + OUTPUT_FILE + " in AA-CIS-Infra.", "Xem " + OUTPUT_FILE + " trong AA-CIS-Infra."),
    "editable": None,
    "editable_href": None,
}
_UNKNOWN_LABEL = _t("No explanation yet: see " + OUTPUT_FILE + " in AA-CIS-Infra.",
                    "Chưa có giải thích: xem " + OUTPUT_FILE + " trong AA-CIS-Infra.")


# ── parse ────────────────────────────────────────────────────────────────────────────────────────────────────────

def parse_section(markdown: str) -> Optional[dict[str, list[tuple[str, str]]]]:
    """The tables of the section :data:`SECTION`: {group: [(key, value), ...]} in page order. None when the page has
    no such section. Pure. A row that is not a two-cell code table row is ignored (headers, separators, prose)."""
    groups: dict[str, list[tuple[str, str]]] = {}
    inside = False
    group: Optional[str] = None
    for line in markdown.splitlines():
        h2 = _H2.match(line)
        if h2:
            if inside:
                break
            inside = h2.group(1) == SECTION
            continue
        if not inside:
            continue
        h3 = _H3.match(line)
        if h3:
            group = h3.group(1)
            groups.setdefault(group, [])
            continue
        row = _ROW.match(line.strip())
        if row and group is not None:
            groups[group].append((row.group(1).strip(), row.group(2).strip()))
    return groups if inside else None


def terraform_sha(markdown: str) -> Optional[str]:
    """The AA-CIS-Infra commit of the generated page (its GENERATED marker), when the bundle kept it."""
    m = _TF_SHA.search(markdown)
    return m.group(1) if m else None


def build_groups(parsed: dict[str, list[tuple[str, str]]]) -> tuple[list[dict], list[str]]:
    """(groups, missing_keys) for the response. Pure. Catalog groups first in :data:`GROUPS` order, then any other
    group of the page in page order."""
    order = [g for g in GROUPS if g in parsed] + [g for g in parsed if g not in GROUPS]
    out = []
    for g in order:
        meta = GROUPS.get(g, _UNKNOWN_GROUP)
        items = []
        for key, value in parsed[g]:
            limit = CATALOG.get(f"{g}.{key}")
            items.append({
                "key": key, "value": value,
                "unit": limit.unit if limit else "text",
                "source": limit.source if limit else None,
                "label": _t(limit.en, limit.vi) if limit else _UNKNOWN_LABEL,
                "known": limit is not None,
            })
        out.append({
            "group": g,
            "title": meta["title"] or _t(g, g),
            "what": meta["what"], "why": meta["why"],
            "change": _t(_CHANGE_EN, _CHANGE_VI),
            "editable": meta["editable"], "editable_href": meta["editable_href"],
            "items": items,
        })
    present = {f"{g}.{k}" for g, rows in parsed.items() for k, _ in rows}
    missing = sorted(k for k in CATALOG if k not in present)
    return out, missing


# ── read ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _missing(reason: str) -> dict:
    return {"doc": {"missing": True, "reason": reason}, "groups": [], "missing_keys": []}


async def read_infra(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    """GET /admin/platform/infra (see the module docstring). Never raises for a missing or unreadable page."""
    try:
        view = await docs_reader.load_staff_page_by_id(PAGE_ID, PAGE_REPO, client_factory)
    except docs_reader.DocsRestricted:
        return _missing("owner_tier")
    except docs_reader.DocsMissing:
        return _missing("not_published")
    except docs_reader.DocsUnavailable:
        return _missing("unavailable")
    markdown = view["markdown"]
    parsed = parse_section(markdown)
    if parsed is None:
        return _missing("not_published")
    groups, missing = build_groups(parsed)
    page = view["page"]
    source = view.get("source") or {}
    return {
        "doc": {
            "missing": False, "reason": None,
            "id": page["id"], "path": page["path"], "title": page["title"],
            "generated_at": view.get("generated_at"),
            "source": {"repo": source.get("repo"), "sha": source.get("sha")},
            "terraform_sha": terraform_sha(markdown),
        },
        "groups": groups,
        "missing_keys": missing,
    }

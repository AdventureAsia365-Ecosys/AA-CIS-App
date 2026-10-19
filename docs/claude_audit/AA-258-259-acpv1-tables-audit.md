# AA-258/259 follow-up — `acp_shared.acp_runs` / `acp_shared.acp_stage_runs` audit, 27/08/2026

Bối cảnh: AA-258/259 (COMMAND-CENTER LLM ops rollup) bị hủy vì scope dựa vào 2 bảng ACPv1 cũ.
Task này verify 2 bảng đó còn dùng thật không, trước khi quyết xóa.

**KHÔNG sửa code, KHÔNG DROP TABLE trong task này** — toàn bộ điều tra là read-only (grep,
`git log`, AWS CLI, SELECT qua S3-mediated ECS exec, task `1a8e3a7de32747dcac9fff7ed244512e`).

**Kết luận sớm (đọc trước khi vào chi tiết): scope thực tế LỚN HƠN 2 bảng được hỏi.**
`acp_shared.acp_runs` là gốc của một cụm 13 bảng khác (6 schema) có FK trỏ vào nó — toàn bộ cụm
14 bảng (tính cả `acp_stage_runs`) đều **0 row**, không riêng 2 bảng ban đầu. Xem §3 + §Kết luận.

---

## Bước 1 — Caller thật của 2 bảng

**Không giống AA-473/475 (nơi bảng chết = code cũng chết) — ở đây bảng "chết dữ liệu" nhưng
CODE vẫn sống, vẫn được đăng ký trong `api/main.py`, chỉ bị gỡ khỏi sidebar (AA-390).**

### `acp_shared.acp_runs` — caller theo file

| File | Vai trò |
|---|---|
| `api/routers/v1_s1.py` | Router `/acp/s1/*` — **đăng ký trong `main.py:126`**. INSERT `acp_runs` (dòng 163-166), list/poll runs (dòng 235-249, 357) |
| `api/routers/v1_s3.py` | Router `/v1/s3/*` — **đăng ký `main.py:128`**. Đọc/UPDATE `acp_runs` (dòng 159, 293, 374) |
| `api/routers/v1_acp_gate.py` | Router — **đăng ký `main.py:129`**. Đọc `tenant_id` từ `acp_runs` (dòng 109, 216, 333) |
| `api/routers/v1_s4_blog.py` | Router `/v1/s4-blog/*` — **đăng ký `main.py:132`**. INSERT/UPDATE `acp_runs` (dòng 114, 127, 156, 166, 205, 232) |
| `api/routers/v1_acp.py` | Router — **đăng ký `main.py:121`**. List/đọc `acp_runs` (dòng 121-207, 229, 367) |
| `api/routers/admin_acp_proxy.py` | Router `/admin/acp/*` — **đăng ký `main.py:135`**. Đọc `acp_runs` (dòng 92-645) |
| `api/routers/admin_pipeline.py` | 2 endpoint "stuck s1_pending" (dòng 4128-4174) — vẫn đọc/ghi `acp_shared.acp_runs` |
| `services/acp/handler.py`, `services/acp_shared/{cost_utils,tracer,h3_rule_extractor}.py`, `services/acp_s3/handler.py`, `services/acp_planning/allocator.py`, `services/acp_canary/lambda_handler.py` | Backend logic viết cost/tenant_id/tracing vào `acp_runs` |
| `api/routers/acp_health.py` (dòng 120, chỉ comment) | **Xác nhận KHÔNG còn đọc `acp_runs` runtime** — chỉ nhắc trong docstring giải thích lý do đã đổi sang `acp_v2_runs` (AA-441). Grep runtime SQL: 0 kết quả ngoài dòng comment này |

### `acp_shared.acp_stage_runs` — caller theo file

| File | Vai trò |
|---|---|
| `services/acp_shared/cost_utils.py` | UPSERT cost/token vào `acp_stage_runs` per (run_id, stage) |
| `services/acp_shared/idempotency.py` | Dùng `acp_stage_runs.event_id` làm idempotency key cho EventBridge |
| `services/acp_s3/handler.py` | UPSERT stage cost |
| `api/lambda/s4_trigger/handler.py` | Idempotency check qua `acp_stage_runs.event_id` |
| `services/acp_canary/lambda_handler.py` | Poll `acp_stage_runs` cho canary health check |
| `tests/acp_s2/test_aa112_bugs.py`, `test_checkpointer.py`, `test_aa169.py` | Unit test cho các hàm trên (mock DB, không chạm DB thật) |

### Reachability thật — điểm mấu chốt

- **Router vẫn đăng ký đầy đủ trong `api/main.py`** (`v1_acp`, `v1_s1`+`v1_s1_from_atom`, `v1_s3`,
  `v1_acp_gate`, `v1_s4_blog`, `admin_acp_proxy`) → về mặt kỹ thuật, **vẫn reachable qua HTTP
  trực tiếp** nếu ai đó gọi đúng URL với JWT/admin-secret hợp lệ.
- **Nhưng sidebar KHÔNG còn link tới các trang này từ AA-390** — comment ngay trong
  `AdminSidebar.tsx:233-237`:
  > "AA-390: Legacy B2B pipeline (ACP v1) sidebar entry hidden — nobody needs ACPv1 Pipeline
  > access via the sidebar anymore (per Nghiep). The routes/pages (admin/pipeline/s2, s3,
  > s4-blog, s4-social) and their backend (admin_acp_proxy.py, v1_acp.py, etc.) are untouched
  > and still reachable directly by URL if ever needed again."
  `CONTENT_AUTHORING_NAV` (sidebar thật hiện tại) chỉ có: Upload(S0), **S1 Rewrite →
  `/admin/s1-rewrite`** (route KHÁC, dùng `_execute_run_tour`/`graph.py`, KHÔNG phải
  `/admin/pipeline/s1` dùng `v1_s1.py`), Review Queue, Brand Identity, Master Content.
  `/admin/pipeline/s1` (frontend page thật có gọi `v1_s1.py`, tìm thấy ở
  `frontend/app/admin/pipeline/s1/page.tsx`) **cũng không có trong sidebar** — cùng nhóm bị ẩn,
  dù comment AA-390 không nêu tên "s1" tường minh.
- **2 Lambda liên quan không còn được deploy**: `aa-cis-dev-acp-s4-trigger` (dùng
  `api/lambda/s4_trigger/handler.py`) và Lambda canary (`services/acp_canary/lambda_handler.py`)
  — **không có trong `aws lambda list-functions` hiện tại** (chỉ còn 9 Lambda live: validation,
  ingestion, acp-s3-campaign-planner, export, authorizer, seo, acp-s4-evaluate, content,
  brand-brief-parser — không có s4-trigger, không có canary). CLAUDE.md ghi "Lambda
  aa-cis-dev-acp-s4-trigger: DEPLOYED ✅" — **đã stale, xác nhận qua AWS CLI hôm nay là sai**.
- **2 Lambda CÓ deploy dùng chung schema này** (`aa-cis-dev-acp-s3-campaign-planner` — dùng
  `services/acp_s3/handler.py`, ghi `acp_stage_runs`; `aa-cis-dev-acp-s4-evaluate`) — nhưng
  **CloudWatch `Invocations` 30 ngày gần nhất = 0 datapoint cho cả 2** (đã query trực tiếp,
  `2026-07-28` → `2026-08-27`).

### Run Health (AA-141 / AA-441 #14) — xác nhận riêng theo yêu cầu

**Xác nhận: Run Health hiện đã 100% dùng `acp_v2_runs`/`acp_v2_slots`, không còn đọc
`acp_runs`/`acp_stage_runs` ở bất kỳ đâu trong runtime.** Đọc trực tiếp
`api/routers/acp_health.py:100-140`: docstring tự ghi rõ lịch sử fix (AA-441 bug #2 = AA-439-00-
SUMMARY #14) — trước đây đọc nhầm `acp_shared.acp_runs` ("0 live rows"), giờ đọc
`acp_shared.acp_v2_runs` (bảng N7/N8, schema khác hẳn: `tenant_id` TEXT không phải UUID,
`created_at` thay cho `started_at`). Per-run stage detail giờ lấy từ `acp_v2_slots`, không phải
`acp_stage_runs` (comment: "which v2 runs never write to"). Grep runtime SQL trong file này:
**0 kết quả** ngoài chính dòng comment giải thích lịch sử. Kết luận AA-446 trước đây (Run Health
đã fix ở AA-441) — **đúng, tái xác nhận hôm nay, không có thay đổi gì thêm**.

---

## Bước 2 — Dữ liệu thật (live query, RDS `aa-cis-dev-db`)

```sql
SELECT COUNT(*) FROM acp_shared.acp_runs;         -- 0
SELECT COUNT(*) FROM acp_shared.acp_stage_runs;    -- 0
```

**Cả 2 bảng: 0 row.** Không có `MIN`/`MAX(created_at)` để so sánh vì hoàn toàn trống — không
phải "dữ liệu cũ, không ai ghi gần đây", mà là **chưa từng có row nào tồn tại tại thời điểm audit
này** (hoặc đã bị dọn sạch ở một đợt cleanup trước, không xác định được thời điểm vì bảng trống
hoàn toàn, không còn dấu vết). Việc này khớp với phát hiện #14 của `AA-438-00-SUMMARY` (chạy
22/08, đã ghi "`acp_shared.acp_runs`, has 0 rows") — **5 ngày sau (27/08), vẫn 0 row, không đổi.**

Lưu ý: `acp_shared.acp_runs` dùng cột `started_at`/`completed_at`, KHÔNG có `created_at` (khác
với `acp_stage_runs`, có `created_at`) — query gốc trong yêu cầu task cần chỉnh cột mới chạy được
trên bảng này.

---

## Bước 3 — FK-graph (pg_constraint, cùng phương pháp AA-473)

**Phát hiện quan trọng: đây KHÔNG phải 2 bảng độc lập — `acp_runs` là gốc FK của cả 1 cụm 13
bảng khác, trải trên 6 schema:**

| Bảng con (FK → `acp_shared.acp_runs.run_id`) | Schema | Row count |
|---|---|---|
| `acp_hitl_requests` | acp_shared | 0 |
| `acp_lessons_agency` | acp_shared | 0 |
| `acp_lessons_shared` (promoted_from_run_id) | acp_shared | 0 |
| `acp_run_context` | acp_shared | 0 |
| `acp_stage_checkpoints` | acp_shared | 0 |
| `acp_stage_runs` | acp_shared | 0 (bảng thứ 2 trong yêu cầu gốc) |
| `pipeline_checkpoints` | acp_shared | 0 |
| `ads_plan` | acp_silver_s3 | 0 |
| `content_calendars` | acp_silver_s3 | 0 |
| `blog_drafts` | acp_silver_s4 | 0 |
| `social_content` | acp_silver_s4 | 0 |
| `published_content` | acp_gold_output | 0 |
| `visibility_reports` | acp_silver_s2 | 0 |
| `tour_content_versions` | **silver_aa_internal** | 0 |

**Toàn bộ 13 bảng con: 0 row, không ngoại lệ.** Bảng đáng chú ý nhất là
`silver_aa_internal.tour_content_versions` — nằm trong schema chính (`silver_aa_internal`, không
phải schema riêng `acp_*`), dễ bị nhầm là bảng "đang dùng" của pipeline hiện tại nếu chỉ nhìn tên
schema — nhưng FK của nó trỏ thẳng vào `acp_shared.acp_runs(run_id)`, tức nó cũng thuộc cụm
ACPv1 chết này, không phải bảng aa_internal content pipeline (A0-A3) đang hoạt động.

**Không tìm thấy bảng nào khác ngoài 13 bảng trên có FK trỏ vào `acp_runs`/`acp_stage_runs`** —
FK graph đã đóng, không có nhánh bất ngờ nào khác cần theo dõi thêm.

(Riêng lưu ý: tồn tại một bảng khác tên `acp_runs` nhưng ở schema **`shared`** (không phải
`acp_shared`) — đây là bảng khác hoàn toàn, batch-keyed, phục vụ A0-A3 admin pipeline
(`services/export/handler.py` ghi vào), đã được AA-438 audit gốc ghi nhận là "naming trap" (mục
#15). Không nằm trong phạm vi task này, không đụng tới.)

---

## Kết luận

**`acp_shared.acp_runs` + `acp_shared.acp_stage_runs` (và 12 bảng con khác cùng cụm):
CHẾT VỀ DỮ LIỆU HOÀN TOÀN (0/14 bảng có row), nhưng CHƯA CHẾT VỀ CODE** — vẫn có ~15 file backend
+ 5 trang frontend + 2 thư mục Lambda source đọc/ghi các bảng này, router vẫn đăng ký trong
`main.py`, về lý thuyết vẫn gọi được qua URL trực tiếp. Bằng chứng "chết trong thực tế":
- Sidebar đã gỡ link từ AA-390 (per quyết định của Nghiệp lúc đó) — không còn đường vào UI bình
  thường.
- 2 Lambda liên quan trực tiếp nhất (s4-trigger, canary) **không còn được deploy**.
- 2 Lambda còn deploy cùng schema (s3-campaign-planner, s4-evaluate): **0 invocation/30 ngày**.
- 0 row ở TẤT CẢ 14 bảng trong cụm, không chỉ 2 bảng được hỏi.
- Run Health (điểm chạm dashboard duy nhất còn lại) đã tách hẳn sang `acp_v2_runs` từ AA-441.

**Không phải "COMMAND-CENTER (AA-258/259) hủy vì 2 bảng chết" — mà là toàn bộ nhánh kiến trúc
ACPv1 S1-S4 (14 bảng DB + code liên quan) đã bị soft-deprecate từ AA-390 nhưng chưa bao giờ được
dọn hẳn.** Phạm vi dọn thật sự lớn hơn nhiều so với 2 bảng ban đầu nêu trong task.

### Đề xuất — 2 lựa chọn phạm vi, chưa áp dụng cái nào

**Lựa chọn A (đúng phạm vi task hỏi — chỉ 2 bảng):** không khả thi sạch sẽ bằng `DROP TABLE`
đơn giản vì `acp_stage_runs` FK vào `acp_runs`, và `acp_runs` lại là gốc của 12 bảng khác — DROP
2 bảng riêng lẻ sẽ cần `CASCADE` (kéo theo xóa luôn 12 bảng con ngoài ý định ban đầu) hoặc phải
xóa `acp_stage_runs` trước rồi mới xóa `acp_runs` (vẫn còn 11 bảng con khác chặn).

**Lựa chọn B (dọn cả cụm 14 bảng — khớp thực tế FK graph tìm được):**

```sql
-- Migration 121: retire toàn bộ ACPv1 S1-S4 schema (14 bảng, 6 schema) — 0 row ở TẤT CẢ,
-- soft-deprecated từ AA-390 (sidebar gỡ link), 2 Lambda liên quan (s4-trigger, canary) không
-- còn deploy, 2 Lambda còn deploy cùng schema (s3-campaign-planner, s4-evaluate) 0 invocation/
-- 30 ngày (xác nhận CloudWatch 27/08/2026). Run Health đã tách sang acp_v2_runs từ AA-441.
-- Xác nhận an toàn: FK-graph (pg_constraint) 27/08/2026 — 13 bảng con của acp_runs, tất cả
-- 0 row, không có bảng nào khác ngoài danh sách dưới đây tham chiếu vào acp_runs/acp_stage_runs.
-- CHƯA xóa code (routers v1_s1.py/v1_s3.py/v1_acp_gate.py/v1_s4_blog.py/v1_acp.py/
-- admin_acp_proxy.py + frontend admin/pipeline/{s1,s2,s3,s4-blog,s4-social} + 2 Lambda source
-- dir) — cần quyết định riêng, KHÔNG gộp chung với migration DB này.

DROP TABLE IF EXISTS acp_shared.acp_hitl_requests;
DROP TABLE IF EXISTS acp_shared.acp_lessons_agency;
DROP TABLE IF EXISTS acp_shared.acp_lessons_shared;
DROP TABLE IF EXISTS acp_shared.acp_run_context;
DROP TABLE IF EXISTS acp_shared.acp_stage_checkpoints;
DROP TABLE IF EXISTS acp_shared.acp_stage_runs;
DROP TABLE IF EXISTS acp_shared.pipeline_checkpoints;
DROP TABLE IF EXISTS acp_silver_s3.ads_plan;
DROP TABLE IF EXISTS acp_silver_s3.content_calendars;
DROP TABLE IF EXISTS acp_silver_s4.blog_drafts;
DROP TABLE IF EXISTS acp_silver_s4.social_content;
DROP TABLE IF EXISTS acp_gold_output.published_content;
DROP TABLE IF EXISTS acp_silver_s2.visibility_reports;
DROP TABLE IF EXISTS silver_aa_internal.tour_content_versions;
DROP TABLE IF EXISTS acp_shared.acp_runs;

INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('121', now(), 'AA-258/259 follow-up: DROP entire dead ACPv1 S1-S4 schema (14 tables, 0 rows, soft-deprecated since AA-390)')
ON CONFLICT DO NOTHING;
```

(**Chưa apply — chỉ để review.** Nếu Nghiệp chọn phạm vi này, code liên quan (routers, frontend
pages, Lambda source, tests) vẫn cần 1 quyết định riêng — xóa code luôn hay giữ code chờ archive,
theo đúng câu hỏi cuối AA-390's comment "reachable directly by URL if ever needed again": nếu DB
bị xóa, code đó sẽ 500 lỗi ngay khi gọi, không còn ý nghĩa "giữ để dùng lại sau" nữa.)

### Việc cần Nghiệp quyết trước khi làm gì tiếp

1. Phạm vi xóa: chỉ 2 bảng gốc (A, cần cascade nên thực chất vẫn kéo theo B) hay cả cụm 14 bảng
   (B, khớp thực tế FK-graph)?
2. Code có xóa theo luôn không (~15 file backend, 5 trang frontend, 2 thư mục Lambda source), hay
   giữ code + chỉ xóa DB (code sẽ lỗi 500 nếu ai gọi URL trực tiếp sau khi bảng bị xóa)?
3. Có cần trước-khi-xóa kiểm tra CloudWatch Logs thật cho `admin_acp_proxy.py`/`v1_acp.py` xem
   có ai từng gọi trực tiếp qua URL (dù không qua sidebar) trong X ngày gần nhất không — task này
   chưa làm, chỉ mới check invocation Lambda, chưa check request log của ECS API.

**Dừng ở đây, chờ Nghiệp xác nhận trước khi xóa thật — đúng như yêu cầu task.**

---
---

# AA-477 — STEP0 lần 2: điều tra sâu quan hệ với A/T-series, 27/08/2026

Nghiệp KHÔNG chấp nhận kết luận STEP0 lần 1 ("0 row/0 caller = an toàn") — đúng, vì 0 row/0
caller chỉ chứng minh HIỆN TẠI không ai dùng, không chứng minh KHÔNG BAO GIỜ có quan hệ với
A/T-series. Task này đọc toàn văn 6 router + tra PRD gốc (Notion) + CloudWatch Logs Insights full
14-ngày (không chỉ metric 30 ngày) + EventBridge + ADR log, để trả lời đúng câu hỏi đó.

**Kết quả quan trọng nhất — đảo ngược một phần kết luận STEP0 lần 1:** package
`services/acp_s4_blog/` **KHÔNG thuần chết** — 2 trong 4 file của nó (`cms/wordpress.py`,
`cms/base.py`) là code **ĐANG SỐNG THẬT**, được `v1_publish.py`/`v1_integrations.py` (T11, AA-457/
458, live production) import trực tiếp. Nếu xóa cả package theo bản năng "S4 Blog = ACPv1 cũ,
chết" sẽ phá vỡ luồng publish WordPress thật đang chạy production. Đây chính xác là loại rủi ro
Nghiệp lo ngại khi từ chối kết luận lần 1 — **KHÔNG sửa gì, chỉ ghi nhận, chờ review.**

**Không sửa code, không sửa DB trong task này — toàn bộ vẫn là điều tra read-only** (đọc file,
Notion PRD, AWS CLI, CloudWatch Logs Insights).

---

## 1 — 6 router: quan hệ tiến hóa với T-series (đọc toàn văn, không chỉ đếm caller)

### v1_s1.py — ĐỘC LẬP HOÀN TOÀN, không có kế thừa ở T-series

**"S1 Configured Rewrite Engine" (admin-facing)**: `POST /acp/s1/run` tạo 1 row
`acp_shared.acp_runs` + N row `silver_aa_internal.tour_content_versions` (1/tour), rồi **trigger
AWS Step Functions** (`sfn.start_execution`, `STEP_FUNCTIONS_ARN` env var) — mỗi tour chạy 1
execution riêng, tiến độ theo dõi qua SSE poll DB mỗi 2s.

Đối chiếu trực tiếp với S1 đang sống (`services/content_generation/graph.py`, gọi qua
`admin_pipeline.py::_execute_run_tour`): **kiến trúc hoàn toàn khác** — S1 sống là LangGraph
`StateGraph` chạy in-process (generate→validate→judge→brand_audit→flag_fix), KHÔNG qua Step
Functions, ghi thẳng `silver_aa_internal.generated_content` (không phải `tour_content_versions`).
**0 dòng code chung, 0 bảng chung.**

Khớp với tech debt đã ghi sẵn trong CLAUDE.md (AA-22): *"Step Functions deployed but bypassed —
direct API flow only"* — cơ chế trigger-qua-SF mà `v1_s1.py` phụ thuộc đã bị BYPASS từ sớm, thay
bằng "direct API flow" (chính là `admin_pipeline.py`/`graph.py` hiện tại).

PRD (Notion, §Thay thế — trích nguyên văn dưới) xác nhận: **"S0+S1 (CIS) KHÔNG bị thay thế"** —
nhưng đây là nói về **nhịp S1 (khái niệm)**, sống tiếp qua bản re-implement khác (`graph.py`),
KHÔNG phải nói `v1_s1.py` (bản SF-orchestrated) tiếp tục chạy. Bản thân `v1_s1.py` là bản tiền
nhiệm đã bị bypass, không phải bản đang chạy.

**→ ĐỘC LẬP HOÀN TOÀN — AN TOÀN XÓA** (router + `tour_content_versions`, xem mục 2). Không phải
vì "S1 bị bỏ" mà vì đây là 1 bản S1 KHÁC, đã bị thay bởi 1 bản S1 khác hẳn về kiến trúc.

### v1_s3.py — ĐỘC LẬP HOÀN TOÀN, chính thức "NÉM" theo PRD

**"S3 Campaign Planner"**: đọc/UPDATE `acp_shared.acp_runs` cho `content_calendars`/`ads_plan`/
Gate 2 HITL. Đối chiếu trực tiếp PRD gốc (Notion "📘 ACP v2 PRD — Atom-based Rebuild N0→N8",
v1.0), §10 "Cái gì NÉM, cái gì GIỮ" — **trích nguyên văn**:

> **Ném:** `storage.py` (file JSON) · `run.py` (CLI monolith) · S2 LangGraph agent + 7 tools
> (ACP v1 cũ) · **S3 campaign planner LLM (ACP v1 cũ)** · `brand_audit_node` self-score · direct
> Anthropic API · single-model judge · prompt-memory thay `acp_output_rules`

Và mục "Thay thế" của cùng PRD (khung N0→N8):

> **Thay thế:** Toàn bộ kiến trúc stage-chain S2→S4.2 cũ (3 HITL gate per-stage, EventBridge
> chain). 20 issue liên quan đã Canceled ở S104. S0+S1 (CIS) KHÔNG bị thay thế... **S2→S4 cũ đã
> build nhưng CHƯA từng chạy production, không xem là ràng buộc khi đập đi làm lại.**

Xác nhận thêm bằng hạ tầng thật (mục 4 dưới): Lambda `aa-cis-dev-acp-s3-campaign-planner`
(chính là S3 Campaign Planner này, "AA-45: S3 Campaign Planner — content_calendars + ads_plan +
Gate 2 HITL") có `DATABASE_URL` **vẫn là placeholder chưa từng điền**
(`postgresql://REPLACE_ME:REPLACE_ME@REPLACE_ME:5432/...`) — không thể nào đã từng kết nối DB
thật thành công.

**→ ĐỘC LẬP HOÀN TOÀN, chính thức bị NÉM theo quyết định ghi trong PRD — AN TOÀN XÓA.**

### v1_acp_gate.py — ĐỘC LẬP HOÀN TOÀN, không phải tiền thân Gate B hay T8

**"B2B Gate Self-Approval" (AA-89)**: `POST /v1/acp/gate/{stage}/approve|reject` cho
stage ∈ {s2, s3, s4} — tenant tự approve/reject run của mình ở TỪNG stage, ghi `audit_log` bắt
buộc, publish event `acp.hitl.approved/rejected` cho stage tiếp theo. Đây chính là "3 HITL gate
per-stage" mà PRD nhắc ở trên.

So sánh khái niệm với 2 ứng viên "kế thừa":
- **Gate B** (AA-320, xóa ở AA-472) — duyệt 1 QUARTER PLAN VERSION trước khi cho phép allocate,
  không phải approve/reject 1 run ở từng stage.
- **T8 Angle Gate** (AA-449) — CHỌN 1 trong 3 angle LLM-generated cho content, không phải
  approve/reject nhị phân 1 run.

Cả 2 đều KHÁC bản chất so với "per-run, per-stage approve/reject" của `v1_acp_gate.py`. PRD tự
xác nhận cơ chế này đã bị thay: **ADR-2026-026 "HITL 2 tầng (D6) thay 3 gate per-run cũ"**
(Accepted 13/07/2026) — 3-gate-per-run bị thay bằng mô hình HITL 2 tầng (auto-check trước, chỉ
escalate người khi fail — triết lý gần với T3 QA gate/T10 gates hiện tại hơn, nhưng không phải kế
thừa file-level, chỉ kế thừa Ý TƯỞNG).

**→ ĐỘC LẬP HOÀN TOÀN, không phải tiền thân trực tiếp của Gate B hay T8 — AN TOÀN XÓA.**

### v1_s4_blog.py — ROUTER/ORCHESTRATION độc lập, NHƯNG import 2 file CÒN SỐNG THẬT (⚠️ quan trọng nhất)

**"S4 Blog Engine" (AA-46)**: `POST /v1/acp/s4/blog/runs` chạy `services/acp_s4/graph.py`
(LangGraph in-process, KHÁC `services/content_generation/graph.py` của S1), ghi
`acp_silver_s4.blog_drafts`, cơ chế review 2 tầng người (`trang` → 1 lần rewrite → escalate
`ms_thu`), rồi enqueue `acp_shared.acp_cms_publish_queue` → publish CMS.

**Phần chắc chắn chết** (không ai gọi ngoài router này, tables riêng, `services/acp_s4/graph.py`+
`generate.py` — grep xác nhận CHỈ được import bởi `v1_s4_blog.py` + file test, KHÔNG router
T-series nào đụng tới):
- Router + endpoint tự nó, `acp_silver_s4.blog_drafts`, `services/acp_s4/{graph,generate}.py`,
  `services/acp_shared/idempotency.py`'s dùng cho gate này.
- `services/acp_s4_blog/cms/publisher.py` (`publish_draft_to_cms`) — hàm CMS-publish CŨ, dùng
  `cms_secret_key = f"acp/cms/{tenant_id}"`.
- `services/acp_s4_blog/validator.py` (`ValidatorAgent`) — chỉ được import bởi
  `services/acp_s4/graph.py`+`generate.py` (2 file vừa xác nhận chết ở trên), KHÔNG được
  `services/acp_content_writing/*` (T9 thật) đụng tới.

**Phần CÒN SỐNG THẬT — KHÔNG ĐƯỢC XÓA:**
- `services/acp_s4_blog/cms/wordpress.py` (`WordPressAdapter`) — **được `api/routers/
  v1_publish.py` import trực tiếp** (`from services.acp_s4_blog.cms.wordpress import
  WordPressAdapter`, dòng 39), đây chính là code T11 (AA-458) dùng để publish WordPress thật,
  theo LIVE STATE CLAUDE.md.
- `services/acp_s4_blog/cms/base.py` (`BlogContent`, `CMSAdapter`, `CMSPostResult`) — cũng được
  `v1_publish.py` import trực tiếp, và là base class của `WordPressAdapter`.
- **Bằng chứng tự ghi trong code**: `v1_integrations.py:9-14` (T11, AA-457) — trích nguyên văn:
  > *"Secrets Manager: reuses `acp/cms/{tenant_id}` — the exact naming convention `v1_s4_blog.py`
  > already invented (confirmed live before this task: 0 secrets ever existed under it). Reuses
  > the same 'arbitrary secret_key string, fetch directly, no caching' pattern
  > `services/acp_s4_blog/cms/publisher.py::_get_cms_creds()` established..."*

  Đây là **tiền thân thiết kế xác nhận, tự ghi lại trong code** — T11 KHÔNG kế thừa dữ liệu/
  runtime của `v1_s4_blog.py` (0 secret nào từng tồn tại, tự T11 xác nhận), nhưng kế thừa
  **quy ước đặt tên + pattern lấy secret**, và quan trọng hơn — **vẫn import chung 2 file service
  thật** (`wordpress.py`, `base.py`) nằm trong CÙNG package `services/acp_s4_blog/`.

**→ MIXED, cần phẫu thuật cấp FILE, KHÔNG được xóa cả package:**
| File trong `services/acp_s4_blog/` | Trạng thái |
|---|---|
| `cms/wordpress.py` | **CẦN GIỮ TUYỆT ĐỐI** — T11 live production import trực tiếp |
| `cms/base.py` | **CẦN GIỮ TUYỆT ĐỐI** — T11 live production import trực tiếp |
| `cms/publisher.py` | AN TOÀN XÓA — chỉ `v1_s4_blog.py` gọi, 0 secret từng tồn tại (tự T11 xác nhận) |
| `validator.py` | AN TOÀN XÓA — chỉ `services/acp_s4/graph.py`+`generate.py` gọi (cả 2 đã xác nhận chết) |

Router `v1_s4_blog.py` tự nó, `services/acp_s4/` (graph.py+generate.py), bảng
`acp_silver_s4.blog_drafts`, `acp_shared.acp_cms_publish_queue`: **AN TOÀN XÓA**.
`services/acp_s4_blog/cms/{wordpress,base}.py`: **CẦN GIỮ**, không liên quan tới việc xóa
ACPv1 — đây là code T-series thật, chỉ tình cờ nằm chung thư mục với code ACPv1 chết.

### v1_acp.py — ĐỘC LẬP HOÀN TOÀN, lớp báo cáo read-only cho ACPv1 cũ

`GET /v1/acp/runs`, `/runs/{id}`, `/runs/{id}/context`, `/s1-keywords` — lớp list/detail cho
run ACPv1 cũ, thuật ngữ "gate1=stage2, gate2=stage3, gate3=stage4" khớp đúng mô hình 3-gate của
`v1_acp_gate.py` ở trên. `/s1-keywords` đọc bảng LIVE (`seo_context`/`raw_tours`) nhưng mục đích
gốc là *"S2 calls this to avoid keyword cannibalization"* — S2 đã NÉM (xem trên), nên endpoint
này không còn caller thật nào nữa dù bảng nó đọc vẫn sống.

**→ ĐỘC LẬP HOÀN TOÀN — AN TOÀN XÓA.**

### admin_acp_proxy.py — ĐỘC LẬP HOÀN TOÀN, đã có tiền lệ điều tra + quyết định của Nghiệp (AA-385)

Không phải lần đầu bị nghi vấn — **AA-385 (Canceled, 09/08/2026)** đã điều tra CHÍNH XÁC câu hỏi
này trước đây, đọc toàn bộ 703 dòng `admin_acp_proxy.py`, xác nhận **0 bảng ACPv2 nào bị đụng**
(migration 096 tách `acp_v2_runs` riêng từ đầu — rủi ro trộn bảng bị chặn chủ động ở tầng
migration), gọi thật 3/4 route qua ECS exec (đều trả `[]` rỗng), tra CloudWatch (khi đó: 3 request
thật 08/08, 0 POST/mutate). **Quyết định của Nghiệp lúc đó** (comment thật, trích nguyên văn):
*"Quyết định: gộp vào AA-386... Lý do: rủi ro thực tế là nhầm lẫn UI... không phải rủi ro dữ liệu
... Đóng issue này — Cancelled..."* → dẫn tới AA-390 (gỡ sidebar link).

**Verify lại hôm nay (không chỉ tin note cũ)** — CloudWatch Logs Insights, cửa sổ 14 ngày đầy đủ
(xem mục 3): route `/admin/acp/runs` (do `admin_acp_proxy.py` phục vụ — **lưu ý: router này và
`acp_health.py` dùng CHUNG prefix `/admin/acp`**, dễ nhầm) nhận đúng **4 request**, toàn bộ trong
8 giây (`2026-08-14 03:48:34-42`), từ 2 IP nội bộ VPC (two private VPC addresses — KHÔNG phải IP
public qua API Gateway) — hình dạng khớp với 1 phiên debug/verify thủ công (giống AA-385 tự làm),
không phải traffic sản phẩm thật. Frontend caller DUY NHẤT của `/admin/acp/runs`:
4 trang mồ côi `s2/s3/s4-blog/s4-social` (đã xác nhận mồ côi sidebar từ AA-390).

**→ ĐỘC LẬP HOÀN TOÀN, đã qua 1 lần điều tra + quyết định thật của Nghiệp trước đây — AN TOÀN
XÓA.** (Không đụng `services/acp_s4_blog/cms/{wordpress,base}.py` dù `admin_acp_proxy.py` có
route `/s4/blog/drafts` riêng — route đó đọc `acp_silver_s4.blog_drafts`, KHÔNG import
`wordpress.py`/`base.py`.)

---

## 2 — `tour_content_versions`: con của cụm chết, không phải bảng CIS core

Schema thật (`api/migrations/026_tour_content_versions.sql`, migration SỐ 026 — rất sớm, trước
cả migration 072-120 mà các tính năng T-series/AA-4xx dùng):

```sql
CREATE TABLE silver_aa_internal.tour_content_versions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_tour_id   UUID NOT NULL REFERENCES silver_aa_internal.raw_tours(tour_id) ON DELETE CASCADE,
    acp_run_id    UUID NOT NULL REFERENCES acp_shared.acp_runs(run_id) ON DELETE CASCADE,
    ...
);
```

`acp_run_id` là **`NOT NULL` FK cứng vào `acp_shared.acp_runs`** — về mặt cấu trúc, KHÔNG THỂ có
row nào trong bảng này mà không có 1 row `acp_runs` tương ứng trước. Vì `acp_runs` = 0 row (STEP0
lần 1), `tour_content_versions` chắc chắn = 0 row, không có ngoại lệ nào khả thi.

**Caller thật (grep toàn repo, backend + frontend, không chỉ ORM/migration)**: DUY NHẤT
`v1_s1.py` (5 chỗ: INSERT tạo version, SSE poll status, list versions, compare versions, activate
version) + `frontend/app/admin/pipeline/s1/page.tsx` (đã xác nhận mồ côi sidebar, KHÁC
`/admin/s1-rewrite` — trang S1 thật). **0 caller khác trong toàn repo.**

**Đối chiếu ghi chú cũ**: `docs/implementation-notes/AA-343.md` (phiên làm việc CŨ, không phải
phiên này) — Round 2 Part A (hard-delete 46 row corrupted), khi scan FK của `raw_tours`, ghi rõ
*"6 real FK constraints point at `raw_tours`; only `tour_content_versions` is `ON DELETE CASCADE`
and **it was empty for these rows too**"* — xác nhận bảng này đã trống từ trước AA-343 (một phiên
làm việc cũ hơn phiên này), không phải mới trống gần đây. Cũng khớp với `ACPv2_Portfolio_Audit_
Giai_doan_3.md` mục 141 (ADR-2026-037, ghi 29/07/2026): liệt kê `tour_content_versions` là 1 trong
4 ca đã biết của lỗi "thiết kế chốt → code build → test pass → Done — chưa ai kiểm có ghi được
xuống DB thật không" — tức đã được CHÍNH THỨC ghi nhận là "empty ACP slot" từ trước, không phải
phát hiện mới của task này.

**Xác nhận rõ theo câu hỏi gốc**: mặc dù tên gọi "tour_content" gợi ý đây là bảng CIS core (S1)
— **KHÔNG PHẢI**. Đây là con của `acp_shared.acp_runs` (cụm ACPv1 chết), bị đặt tên dễ gây nhầm
với S1's `silver_aa_internal.generated_content` (bảng CIS core thật, hoàn toàn khác, không liên
quan).

**→ AN TOÀN XÓA.** Bằng chứng: FK cứng khóa vào tổ tiên đã xác nhận chết + 0 caller ngoài router
đã kết luận độc lập hoàn toàn (mục 1) + trống từ trước 1 phiên làm việc cũ khác (không phải chỉ
"gần đây không ai ghi") + đã được 1 ADR chính thức (2026-037) gọi tên là ca "empty slot" biết
trước.

---

## 3 — CloudWatch Logs Insights, cửa sổ retention thật (14 ngày, không phải 30)

Log group `/ecs/aa-cis-dev`: **retention thật = 14 ngày** (`describe-log-groups` xác nhận trực
tiếp hôm nay — khớp với phát hiện cũ của AA-385, retention KHÔNG phải 30 ngày như config từng
tưởng). **Giới hạn thật**: cửa sổ query dưới đây chỉ phủ được 13/08 → 27/08/2026 — KHÔNG thể xác
nhận hay phủ nhận traffic trước 13/08 (ví dụ: liệu các router này có traffic thật hồi mới build,
tháng 5-7/2026, hay không — ngoài khả năng kiểm chứng bằng log ở thời điểm hiện tại).

Query Logs Insights (`fields @timestamp, @message | filter @message like /(?i)(\/acp\/s1|\/v1\/s3
|\/v1\/acp\/gate|\/v1\/acp\/s4\/blog|\/v1\/acp\/runs|\/v1\/acp\/s1-keywords|\/admin\/acp\/)/`),
toàn bộ cửa sổ 14 ngày, 421,911 dòng log quét, **8 dòng khớp**:

| Route | Số hit | Router phục vụ | Ghi chú |
|---|---|---|---|
| `/admin/acp/run-health` | 5 | **`acp_health.py`** (route KHÁC, đã fix AA-441, đọc `acp_v2_runs`) | Traffic thật, dashboard live, KHÔNG thuộc phạm vi 6 router đang xét — dễ nhầm vì cùng prefix `/admin/acp` |
| `/admin/acp/runs` | 4 | `admin_acp_proxy.py` (đúng router đang xét) | 4 hit trong 8 giây, 14/08, 2 IP nội bộ VPC — xem mục 1 (admin_acp_proxy.py) |
| `/acp/s1/*`, `/v1/s3/*`, `/v1/acp/gate/*`, `/v1/acp/s4/blog/*`, `/v1/acp/s1-keywords` | **0** | — | Không 1 request nào trong 14 ngày |

**⚠️ Bẫy đặt tên phát hiện được**: `admin_acp_proxy.py` và `acp_health.py` dùng CHUNG URL prefix
`/admin/acp` (`APIRouter(prefix="/admin/acp", ...)` ở cả 2 file) — nhìn thoáng qua log
`/admin/acp/*` sẽ tưởng nhầm router ACPv1 cũ còn traffic thật, trong khi phần lớn traffic đó
(5/8) thực ra đi vào router ĐÃ FIX, không liên quan gì tới bảng chết.

**Kết luận mục 3**: trong giới hạn 14 ngày quan sát được, 5/6 router (`v1_s1`, `v1_s3`,
`v1_acp_gate`, `v1_s4_blog`, `v1_acp`) = 0 request thật. `admin_acp_proxy.py` = 4 request, nhưng
đều nội bộ VPC, hình dạng debug-session chứ không phải traffic sản phẩm. **Không có bằng chứng
traffic thật (public, qua API Gateway) tới bất kỳ router nào trong 6 router, trong 14 ngày gần
nhất.** Không suy rộng ra được cho giai đoạn trước 13/08 — giới hạn log thật, không giả vờ đã
xác nhận đủ.

---

## 4 — 2 Lambda còn deploy: chức năng thật + EventBridge

| Lambda | Chức năng thật (Description tự khai) | Env DB | Log group |
|---|---|---|---|
| `aa-cis-dev-acp-s3-campaign-planner` | *"AA-45: S3 Campaign Planner — content_calendars + ads_plan + Gate 2 HITL"* — **chính là `v1_s3.py`'s Lambda, đã xác nhận NÉM ở mục 1** | `DATABASE_URL=postgresql://REPLACE_ME:REPLACE_ME@REPLACE_ME:5432/aa_cis_dev?sslmode=require` — **placeholder CHƯA TỪNG được điền giá trị thật** | `/aws/lambda/...`, **0 byte lưu trữ** kể từ khi log group được tạo (~04/08/2026) |
| `aa-cis-dev-acp-s4-evaluate` | *"AA-49 H-1: Isolated S4 blog draft evaluator — Bedrock only, no DB access"* — evaluator cho Gate 3 của `v1_s4_blog.py`, không đụng DB trực tiếp | (không có DB env) | **0 byte lưu trữ**, cùng thời điểm tạo |

**EventBridge — kiểm tra đúng yêu cầu (rule có thể tồn tại nhưng chưa từng khớp điều kiện, khác
với "không có rule nào")**:
- `list-rules` trên bus `default`: **0 rule**.
- Phát hiện 1 event bus TÙY CHỈNH: `aa-cis-dev-acp-events` (tạo 14/08/2026) — `list-rules` trên
  bus này: **cũng 0 rule**. (Bus này liên quan AA-402, "EventBridge module orphaned — dời sang
  acc2", Done — AA-402 sửa VỊ TRÍ account của bus, không phải tạo rule cho nó; bus tồn tại đúng
  chỗ nhưng vẫn chưa từng được nối dây tới bất kỳ consumer nào, khớp với phát hiện độc lập của
  `ACPv2_Audit_Giai_doan_5_UI_Reality_Check.md`: *"N7 Produce... 0 schedule tự động thật —
  EventBridge tự nhận 'chưa có consumer'"*).
- **`get-policy` cho CẢ 2 Lambda: `ResourceNotFoundException`** — nghĩa là **0 resource-based
  policy tồn tại**, tức KHÔNG CÓ BẤT KỲ AWS service nào (EventBridge, API Gateway, S3, SQS...)
  được cấp quyền invoke 2 Lambda này. Cách duy nhất gọi được là ai đó có IAM quyền tự gọi
  `lambda:InvokeFunction` thủ công.

**→ 2 Lambda: AN TOÀN XÓA.** Bằng chứng đến từ 4 nguồn độc lập, không chỉ 1 metric: 0 EventBridge
rule (cả bus mặc định lẫn bus riêng), 0 resource policy (không service nào được quyền gọi), 0
byte log kể từ khi tạo, và (riêng S3 planner) chuỗi kết nối DB là placeholder chưa từng điền —
không thể nào Lambda này đã từng chạy thành công.

---

## 5 — Mốc "khai tử" ACPv1 chính thức

**Có mốc rõ ràng, có ADR, có ngày — không phải quá trình mờ dần như nghi ngờ ban đầu.**
Đối chiếu PRD gốc (Notion "📘 ACP v2 PRD — Atom-based Rebuild N0→N8", đọc trực tiếp §11 "ADR liên
quan" + §10 "Cái gì NÉM"):

**13/07/2026** — Nghiệp + Ms. Thư khóa kiến trúc N0-N8 (8 quyết định D1-D8 + 6 luật L1-L6). Cùng
ngày, 3 ADR được Accepted, chính thức supersede kiến trúc ACPv1 stage-chain:

| ADR | Nội dung | Supersede gì |
|---|---|---|
| **ADR-2026-013** (Stage Orchestration S1→S4.2) | **Superseded** — "mô hình 8 nhịp thay stage-chain, đơn vị TOUR→SLOT" | Chính cơ chế orchestration mà `v1_s1.py`/`v1_s3.py`/`v1_s4_blog.py` dùng |
| **ADR-2026-024** (Atom-based corpus = tầng sự thật) | Accepted | ADR-017 (ACP S2-S4.1 rebuild cũ) |
| **ADR-2026-026** (HITL 2 tầng, D6) | Accepted | "3 gate per-run cũ" — chính là `v1_acp_gate.py` |

Trích nguyên văn PRD (mục "Thay thế", đã trích đủ ở mục 1 trên): **"S2→S4 cũ đã build nhưng CHƯA
từng chạy production, không xem là ràng buộc khi đập đi làm lại"** — 20 Linear issue liên quan
Canceled ở Sprint 104.

**Nhưng có 1 khoảng trống thật, đáng lưu ý riêng**: quyết định KIẾN TRÚC (13/07/2026, có ADR) và
hành động DỌN DẸP là 2 việc KHÁC NHAU, và chỉ việc đầu có mốc rõ. AA-390 (Done, ưu tiên Low —
"UI 7/7 — Sidebar label fix", ngày sau đó) chỉ gỡ LINK sidebar, tự ghi rõ trong code
(`AdminSidebar.tsx:233-237`): *"untouched and still reachable directly by URL if ever needed
again"* — tức bản thân AA-390 CHỦ ĐỘNG CHỌN không xóa code/DB, chỉ ẩn UI. **Không có ADR hay
Linear issue nào sau đó ra quyết định "xóa hẳn code+DB của ACPv1"** — đây chính là khoảng trống
mà AA-258/259 (bị hủy) và task AA-477 này đang cố lấp — **quyết định kiến trúc đã có từ 13/07,
nhưng quyết định dọn dẹp vật lý thì chưa bao giờ được đưa ra chính thức, tới tận hôm nay.**

---

## Bảng tổng kết — verdict riêng biệt từng phần

| Phần | Verdict | Bằng chứng chính |
|---|---|---|
| `v1_s1.py` + router | **AN TOÀN XÓA** | Kiến trúc SF-orchestration bị bypass (AA-22), 0 code chung với S1 sống, 0 request 14 ngày |
| `v1_s3.py` + router | **AN TOÀN XÓA** | PRD §10 "NÉM" tường minh, Lambda liên quan DB placeholder chưa điền, 0 request 14 ngày |
| `v1_acp_gate.py` + router | **AN TOÀN XÓA** | Không phải tiền thân Gate B/T8 (khác bản chất), đã bị ADR-2026-026 thay thế chính thức, 0 request 14 ngày |
| `v1_s4_blog.py` + router + `services/acp_s4/*` | **AN TOÀN XÓA** | PRD "chưa từng chạy production", 0 request 14 ngày, tables riêng không ai khác đụng |
| `services/acp_s4_blog/cms/publisher.py` + `validator.py` | **AN TOÀN XÓA** | Chỉ router đã-xác-nhận-chết gọi, 0 secret từng tồn tại (tự T11 xác nhận) |
| `services/acp_s4_blog/cms/wordpress.py` + `base.py` | **⚠️ CẦN GIỮ TUYỆT ĐỐI** | T11 (AA-457/458) live production import trực tiếp — xóa sẽ phá publish WordPress thật |
| `v1_acp.py` + router | **AN TOÀN XÓA** | Lớp report read-only cho ACPv1, caller gốc (S2) đã NÉM, 0 request 14 ngày |
| `admin_acp_proxy.py` + router | **AN TOÀN XÓA** | Đã qua 1 lần điều tra+quyết định thật của Nghiệp (AA-385), verify lại hôm nay: chỉ 4 hit nội bộ VPC dạng debug |
| `silver_aa_internal.tour_content_versions` | **AN TOÀN XÓA** | FK cứng khóa vào `acp_runs` đã chết, trống từ ≥AA-343 (phiên cũ), đã có ADR-2026-037 gọi tên "empty slot" từ trước |
| Cụm 14 bảng DB (STEP0 lần 1) | **AN TOÀN XÓA (dữ liệu)**, nhưng nên làm ĐỒNG THỜI với xóa code — không xóa DB trước một mình khi code sống (dù chưa từng chạy production thành công) vẫn còn đọc/ghi chúng | STEP0 lần 1 + xác nhận lại hôm nay |
| Lambda `acp-s3-campaign-planner` + `acp-s4-evaluate` | **AN TOÀN XÓA** | 0 EventBridge rule (2 bus), 0 resource policy, 0 log byte từ khi tạo, DB placeholder chưa điền |

**Không mục nào rơi vào "KHÔNG CHẮC"** — mọi nhánh điều tra đều tìm được bằng chứng đủ mạnh để kết
luận dứt điểm, kể cả trường hợp đảo ngược (wordpress.py/base.py). Duy nhất 1 giới hạn thật không
lấp được: CloudWatch chỉ phủ 14 ngày gần nhất, không nói được gì về traffic trước 13/08/2026.

## Should know (cho người review sau)

- **Đừng bao giờ `rm -rf services/acp_s4_blog/`** — 2/4 file trong đó là code T11 sống thật.
  Phẫu thuật xóa phải ở cấp FILE (`publisher.py`, `validator.py`), không phải cấp thư mục.
- `admin_acp_proxy.py` và `acp_health.py` dùng chung prefix `/admin/acp` — bất kỳ ai đọc log
  `/admin/acp/*` trong tương lai phải tách theo path đầy đủ, không gộp theo prefix.
- Quyết định kiến trúc "bỏ ACPv1 stage-chain" đã có từ 13/07/2026 (3 ADR), nhưng chưa từng có
  quyết định dọn dẹp vật lý — nếu Nghiệp đồng ý xóa sau khi đọc báo cáo này, đây sẽ là quyết định
  dọn dẹp CHÍNH THỨC ĐẦU TIÊN, hơn 1 tháng sau quyết định kiến trúc.

**Dừng ở đây, chưa xóa gì (code lẫn DB) — chờ Nghiệp review.**

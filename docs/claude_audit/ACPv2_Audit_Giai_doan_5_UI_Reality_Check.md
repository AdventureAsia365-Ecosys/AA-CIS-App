# ACPv2 Portfolio Audit — Giai đoạn 5: UI/Route Reality Check

*15/08/2026. Audit toàn bộ S0/S1/S1-from-atom/N0→N8/Marketplace ở 3 tầng: (a) code tồn tại, (b) route/endpoint sống, (c) UI thật người dùng bấm được. Đọc trực tiếp `frontend/app/admin/_components/AdminSidebar.tsx`, `frontend/middleware.ts`, toàn bộ `api/routers/*.py` (`app.include_router()` trong `api/main.py`), Terraform `modules/eventbridge/main.tf`. KHÔNG chạy browser thật — Claude Code không có mắt để bấm; mọi kết luận "UI dùng được" chỉ xác nhận CÓ TỒN TẠI + CÓ ĐƯỜNG DẪN tới, KHÔNG xác nhận nó render đúng/không lỗi khi Nghiep bấm thật.*

---

## Đối chiếu sidebar ảnh Nghiep gửi

Đọc `AdminSidebar.tsx` xác nhận đúng 100% ảnh chụp: nhóm "ACP v2 — Setup & Approval" (Dashboard, Tenants, Marketplace, Quarter Plan (Gate B), Run Health — admin-only) · nhóm "ACP v2 — Atoms" (Atomize (N2), Atom Curation) · nhóm "AA Internal Content" (Upload (S0), S1 Rewrite, Review Queue, Brand Identity, Master Content) · Settings (admin-only). Comment ngay đầu file (dòng 21-31) tự giải thích: nhóm ACPv1 legacy (S2 Research/S3 Calendar/S4 Blog/S4 Social) đã bị gỡ khỏi sidebar theo AA-390 ("nobody needs ACPv1 access anymore, per Nghiep") — route/backend vẫn còn, chỉ không có link nữa. Không thấy N0/N1(riêng)/N4/N5b/N6/N7/N8(riêng) trên sidebar — điều tra chi tiết dưới đây cho từng trường hợp.

---

## Bảng 16 mục

### 1. S0 Ingest
- **(a) Code:** có — `api/routers/admin_pipeline.py:1056,1077,1237,1294` (`/upload-url`, `/ingest-s3`, `/upload-staging/{batch_id}`, `/upload-staging/{staging_id}/decide`).
- **(b) Route sống:** có — router mounted (`api/main.py:190`), prefix `/admin/upload` trong `PROTECTED_ROUTES` (`middleware.ts:71`, role admin/reviewer/content).
- **(c) UI:** có, trên sidebar ("Upload (S0)" → `/admin/upload`).
- **Trace code path Excel→raw_tours:** `frontend/app/admin/upload/page.tsx` → `POST /api/admin/upload-url` (lấy presigned S3 URL) → upload trực tiếp lên S3 → `POST /api/admin/ingest-s3` (parse Excel, ghi `silver_aa_internal.raw_tours`) → `GET /api/admin/upload-staging/{batch_id}` (xem staging trước khi quyết định) → `POST .../decide` (chấp nhận/từ chối từng dòng). Đường code đầy đủ, khớp PRD "S0 chính là nguồn input duy nhất nuôi N0-N2".

### 2. S1 Rewrite (CIS cũ)
- **(a)(b)(c):** đều có, trên sidebar ("S1 Rewrite" → `/admin/s1-rewrite`).
- **UI trigger 1 tour cụ thể:** CÓ — `frontend/app/admin/s1-rewrite/page.tsx:313` gọi `POST /api/admin/run-tour-async` (khớp `generate_node` đã verify code thật ở Giai đoạn 2 Mục 1, `graph.py:283-340`), poll `GET /api/admin/jobs/{jobId}` (dòng 439), xem lịch sử `GET /api/admin/tours/{tourId}/history` (dòng 460). KHÔNG chỉ xem kết quả — đây là màn hình TRIGGER thật, không phải chỉ Review Queue.
- **⚠️ Phát hiện phụ (route mồ côi):** tồn tại 1 route THỨ HAI cùng chủ đề "S1" — `/admin/pipeline/s1` (file `frontend/app/admin/pipeline/s1/page.tsx`, 619 dòng, commit gốc 23/05/2026 "AA-103 E2E UI — CIS stage pages") — gọi `POST /acp/s1/run` + `PATCH /acp/s1/versions/{id}/activate` (`api/routers/v1_s1.py`, mounted prefix `/acp/s1`, **router ACPv1 GỐC, khác hẳn `admin_pipeline.py`**). Route này KHÔNG có trên sidebar (thuộc nhóm ACPv1 bị gỡ theo AA-390 comment) — chỉ vào được bằng gõ URL trực tiếp. Xem Mục 15/16.

### 3. S1-from-atom (N2b)
- **(a) Code:** có — `api/routers/v1_s1_from_atom.py:64,79` (`GET /tours/{tour_id}/atoms`, `POST /tours/{tour_id}/generate`).
- **(b) Route sống:** có — router mounted (`api/main.py:181`, không prefix riêng, path thật `/v1/s1-from-atom/...` — cần xác nhận thêm prefix chính xác nếu cần gọi tay). **Không nằm trong `PROTECTED_ROUTES` của `middleware.ts`** vì đây là `/v1/*` (BFF backend trực tiếp), không phải `/admin/*` hay `/api/admin/*` proxy — nhưng đây KHÔNG nằm trong 4 nhóm route middleware bảo vệ (`/admin`, `/upload`, `/review`, `/catalog`, `/brand`, `/portal`) nên đi qua auth khác (`X-Admin-Secret` service-to-service, theo comment CLAUDE.md) — không phải "route mở hoàn toàn không bảo vệ", chỉ là bảo vệ ở tầng khác (BFF `/api/admin/[...path]/route.ts`), không phải Next.js middleware.
- **(c) UI:** **KHÔNG CÓ — xác nhận 0 kết quả.** `grep -rln "s1_from_atom\|tours/.*atoms\|/generate\"" frontend/app` trả về **rỗng hoàn toàn**. Không có trang nào, không núp dưới tên nào trên sidebar — "S1 Rewrite" trên sidebar chỉ gọi `run-tour-async` (S1 CIS cũ, mục 2), không đụng N2b.
- **Kết luận: GAP thật.** N2b sinh nội dung production thật (đã verify DB writes ở Giai đoạn 2/3 qua AA-392) nhưng KHÔNG CÓ MỘT MÀN HÌNH NÀO để trigger/xem — mọi lần chạy N2b từ trước tới nay đều qua script/ECS-exec tay (khớp AA-391 E2E test), không qua UI.

### 4. N0 Enrich
- **(a) Code:** **KHÔNG TÌM THẤY hàm nào tên "enrich" trong `services/`** (`grep -rln "def enrich\|N0.*Enrich\|enrich_tour" services/` → rỗng).
- **(b) Route:** không có endpoint nào (`grep -rn "enrich" api/routers/*.py` → rỗng).
- **(c) UI:** không.
- **(iii) Thiết kế đúng hay gap?** **CHƯA XÁC NHẬN ĐƯỢC DỨT KHOÁT.** 2 khả năng: (1) N0 đã được GỘP hoàn toàn vào N2 decompose — cùng 1 issue Linear (AA-299 "N0+N2"), cùng router xử lý (`v1_atoms.py`, chỉ có `/decompose`, không có bước enrich tách biệt trước đó); PRD tự thiết kế N0 chạy tự động qua Step Functions→Bedrock Batch nhưng không tìm thấy Step Function nào tên liên quan "enrich" trong Terraform (`grep -rln "enrich\|N0" AA-CIS-Infra/modules/ AA-CIS-Infra/accounts/` → chỉ trúng false-positive trong file SQL migration, không phải hạ tầng N0 thật). (2) N0 chưa từng được build như 1 bước riêng dù PRD mô tả rõ (Llama 4 Scout, 10M context, tách biệt N2). **Cần Nghiep xác nhận trực tiếp — đây KHÔNG phải kết luận có thể tự suy ra từ code.**

### 5. N1 Onboard
- **Xác nhận qua code, không đoán theo tên:** "Tenants" trên sidebar (`/admin/tenants`) **CHÍNH XÁC LÀ N1 UI** — `frontend/app/admin/tenants/page.tsx:412-651` có nguyên khối "Onboarding tab (AA-389: N1 seed-atoms / angle assign / Gate A)", tự comment "Flow order matches AA-309's 6-step spec exactly".
- **(a)(b)(c):** đều có — gọi đủ 4 endpoint N1 thật: `POST /api/admin/tenants/{id}/seed-atoms` (dòng 497), `PATCH .../angle` (511), `GET .../gate-a/status` (470), `POST .../gate-a/approve` (526) — khớp `api/routers/admin.py:892,971,1159,1242`.
- **Kết luận:** N1 UI đầy đủ, đúng như Linear ghi (AA-389 "UI 3/7 Done") — xác nhận qua code thật, không chỉ tin Linear.

### 6. N2 Corpus (Atom Decompose)
- **(a)(b)(c):** đều có, trên sidebar ("Atomize (N2)" → `/admin/atomize`).
- **Trace luồng bấm nút → kết quả:** `frontend/app/admin/atomize/page.tsx:197` → `GET /api/admin/tours-for-atomization` (danh sách tour đủ điều kiện) → dòng 248 → `POST /api/admin/atoms/decompose` (`admin_pipeline.py:1857`, status 202 — async) → kết quả atom mới xuất hiện ở trang "Atom Curation" (mục 7).
- **⚠️ Phát hiện phụ quan trọng (đọc trong lúc trace route N2):** docstring `api/routers/v1_atoms.py:1-24` tự ghi rõ: *"NOTE: production boto3 (requirements.txt, 1.34.69) does not yet have `create_model_invocation_job` — the ≥100 Batch path's Bedrock call will fail with an SDK-level error (unknown operation) until the production SDK is upgraded. Verified working in isolation via a separate venv (boto3 1.43.49), not yet through this service."* — nghĩa là **nhánh Batch (≥100 tour cùng lúc) có khả năng ĐANG HỎNG trên production** do lệch version SDK, dù nhánh inline (<100 tour) không phụ thuộc hàm này nên vẫn chạy được. Đây là phát hiện NGOÀI phạm vi 4 câu hỏi UI/route (thuộc lớp bug code), ghi lại ở đây vì phát hiện được trong lúc trace — **cần Nghiep xác nhận đây có phải bug còn sống không, ngoài phạm vi Giai đoạn 5 để điều tra sâu**.

### 7. N3 Curate
- **(a)(b)(c):** đều có, trên sidebar ("Atom Curation" → `/admin/curation`, + `/admin/curation/preview` — có nút dẫn tới từ trang chính, dòng 504: `router.push("/admin/curation/preview")` — không mồ côi).
- **Endpoint:** `admin_atoms.py:142,234,309,364,419` (`/atoms`, `/atoms/summary`, `/atoms/bulk` PATCH, `/atoms/{id}` PATCH, `/atoms/preview-slotgrid`).

### 8. N4 Strategy (Runway)
- **PRD tự ghi:** "1×/năm + invalidation, Lambda, $0 LLM" — bảng Nhịp trong PRD (§4) không liệt kê UI riêng cho N4, chỉ ghi Compute=Lambda.
- **(a) Code:** có, `services/acp_planning/runway.py::runway_map()`.
- **(b)(c) — xác nhận qua code, không phải suy đoán:** `api/routers/admin.py:1755` (`POST /admin/quarter-plan`, handler `create_quarter_plan`) gọi **trực tiếp, đồng bộ trong request**: `runway = await runway_map(...)` **rồi** `plan = await plan_quarter(...)` — N4 chạy NGAY BÊN TRONG luồng tạo Quarter Plan (N5), không phải Lambda/cron tách biệt như PRD mô tả ban đầu, nhưng CÓ trigger thật qua UI: nút "Create" trên trang Quarter Plan (mục 9) → `/admin/quarter-plan/create` (có link thật, không mồ côi — `quarter-plan/page.tsx:213`).
- **Kết luận:** ĐÚNG thiết kế theo nghĩa rộng (không cần UI riêng, PRD không đòi hỏi) — nhưng thực thi khác PRD mô tả (không phải Lambda độc lập 1×/năm, mà chạy đồng bộ mỗi lần N5 được tạo). Không phải gap, chỉ là chi tiết triển khai khác PRD.

### 9. N5 Portfolio + Gate B
- **(a)(b)(c):** đều có, trên sidebar ("Quarter Plan (Gate B)" → `/admin/quarter-plan`).
- **Xác nhận KHÔNG phải trang tĩnh/demo:** `frontend/app/admin/quarter-plan/page.tsx:115,124,135,170,330` gọi 5 endpoint thật: `GET /quarter-plan/pending` (danh sách chờ duyệt — Giai đoạn 2 đã verify DB có 2 plan/7 version thật), `GET /tenants`, `GET /quarter-plan/{tenant}/{year}/{quarter}` (chi tiết), `POST /quarter-plan/{version_id}/approve` (nút duyệt thật, ghi DB), `GET .../history`. Toàn bộ khớp `admin.py:1755-1997`.
- **Kết luận:** UI thật, render dữ liệu thật, nút duyệt ghi DB thật — không phải demo. Khớp hoàn toàn với dữ liệu đã verify ở Giai đoạn 2 Mục 4.

### 10. N5b Override
- **Xác nhận: đúng CHƯA CÓ GÌ, kể cả UI.** Grep "override" trúng 2 chỗ hoàn toàn KHÔNG LIÊN QUAN: (1) `services/acp_planning/quarter.py:149` — comment nhắc "AA-323 Gap 1 — manual N5 override" nhưng đó là badge hiển thị nguồn gốc 1 dòng portfolio (`source === "override"`), không phải tính năng N5b Quarter Override của PRD; (2) `quarter-plan/page.tsx:290` — cùng badge đó. **Không có route, không có UI, không có backend logic nào cho N5b thật** — khớp chính xác Linear (AA-307, Backlog).

### 11. N6 Schedule (Slot Allocator)
- **PRD tự ghi:** "1×/tháng + invalidation, Lambda, $0" — không đòi UI riêng.
- **(a) Code:** có, `services/acp_planning/allocator.py` (`allocate_month()`/`allocate_month_from_db()`).
- **(b)(c):** không có trang riêng, nhưng **CÓ trigger qua UI khác** — `GET /admin/atoms/preview-slotgrid` (`admin_atoms.py:419`) gọi `allocate_month_from_db()` khi có dữ liệu thật, **fallback về `allocate_month()` chế độ preview/demo trong bộ nhớ khi CHƯA có dữ liệu thật** (đọc trực tiếp docstring dòng 438-440: *"Only falls back to the in-memory 'admin-preview-demo' auto-approve... when one doesn't exist"*). Endpoint này được gọi từ CẢ `quarter-plan/page.tsx` LẪN `curation/preview/page.tsx`.
- **Kết luận:** ĐÚNG thiết kế (không cần UI riêng) — nhưng có 1 điểm cần lưu ý: khi chưa có slot grid thật persist trong DB cho 1 tenant/tháng cụ thể, trang preview sẽ tự động hiện dữ liệu DEMO thay vì báo "chưa có dữ liệu" — nếu Nghiep bấm vào 1 tenant chưa từng chạy N6 thật, có thể nhầm demo data với data thật. Đáng lưu ý khi test qua browser.

### 12. N7 Produce
- **PRD tự ghi:** "hàng tuần, ECS Fargate Spot + checkpoint/slot + SQS FIFO" — không đòi UI trực tiếp cho từng slot, nhưng NGẦM ĐỊNH cần 1 cơ chế theo dõi tiến trình (PRD nói rõ N8 cần biết N7 xong chưa để assemble packet).
- **(a) Code:** có, rất đầy đủ — toàn bộ `services/acp_produce/*.py` (gates.py, pipeline.py, slot_runner.py, sweeper_lambda.py…).
- **(b) Route sống — KHÔNG CÓ:** `grep -rln "acp_produce" api/routers/` → **rỗng hoàn toàn**. Không router nào trong `api/routers/` import từ `services.acp_produce`. Xác nhận thêm: `run_slot_production()` (`slot_runner.py:79`, hàm production thật) chỉ được gọi từ `tests/verify_scripts/aa377_aa378_run_persist_verify.py`, `aa394_verify_f6_gate_pass.py`, `aa375_slot_production_verify.py`, `aa391_e2e_orchestrator.py` và `tests/unit/test_aa375_slot_runner.py` — **TOÀN BỘ là script test/verify, KHÔNG có API endpoint, KHÔNG có route production nào gọi hàm này**.
- **⚠️ Xác nhận thêm — không có scheduled trigger tự động:** đọc `AA-CIS-Infra/modules/eventbridge/main.tf:87-175` — 3 schedule EventBridge (`acp_daily_ingest`, `acp_weekly_export`, `acp_veto_window_close`) chỉ PUT event lên bus, và chính comment trong file Terraform tự thừa nhận (dòng 150-153): *"no `aws_cloudwatch_event_rule` consumes this event yet... this resource alone has no runtime effect on its own until that follow-up."* → **Không có schedule nào thật sự trigger N7 production hàng tuần.** Mọi lần N7 từng chạy (9 piece thật của AA-396, E2E test AA-391) đều do người CHỦ ĐỘNG chạy script qua ECS exec, không phải tự động.
- **(c) UI: KHÔNG CÓ.** "Run Health" trên sidebar (`/admin/run-health`) — kiểm tra kỹ backend `api/routers/acp_health.py:104` — query `FROM acp_shared.acp_runs` (dòng 154). **ĐÂY LÀ BẢNG ACPv1 CŨ**, không phải `acp_shared.acp_v2_runs` (bảng N7 thật, tạo bởi migration 096 cho AA-377/378 — xác nhận qua chính comment trong `096_acp_v2_runs_slots.sql:28`: *"Re-pointed here at acp_v2_runs(run_id) instead"* — nghĩa là hệ thống ĐÃ chủ động tách bảng ACPv2 khỏi bảng cũ, nhưng "Run Health" chưa từng được cập nhật theo). File `acp_health.py` chỉ có đúng 1 commit (`6546ac0`, AA-141, tính năng cũ) — **chưa từng bị đụng lại kể từ đó**.
- **Kết luận: GAP THẬT, mức độ nghiêm trọng — cả (b) và (c) đều thiếu.** "Run Health" (trang duy nhất có tên gợi ý theo dõi N7) đang hiển thị dữ liệu SAI HỆ THỐNG (ACPv1, không phải N7 ACPv2). Không có cách nào qua UI để biết N7 tuần này đã chạy chưa, chạy tới đâu, held bao nhiêu piece.

### 13. N8 Deliver + Learn
- **PRD tự ghi:** "hàng tuần, Lambda+SES." Gate C (Trang review) — theo AA-365 note cũ "No new UI".
- **(a) Code:** có — `services/acp_deliver/*.py` (packets.py, trust_ramp.py…).
- **(b) Route sống — KHÔNG CÓ:** cùng kết quả với N7 — `grep -rln "acp_deliver" api/routers/` → rỗng. Không endpoint nào cho Weekly Packet, veto, hay Gate C.
- **(c) UI — KHÔNG CÓ, XÁC NHẬN LẠI ĐÚNG:** `grep -rln "veto\|Weekly Packet\|Gate C\|packet" frontend/app --include="*.tsx"` → **rỗng hoàn toàn**. Note "No new UI" của AA-365 vẫn ĐÚNG tại thời điểm audit này (15/08) — không có gì thay đổi.
- **Kết luận: GAP THẬT** — giống hệt N7, cả route lẫn UI đều 0%. `acp_veto_window_close` schedule (Terraform) cũng xác nhận chưa có consumer (xem mục 12).

### 14. Marketplace
- **(a)(b)(c):** đều có, trên sidebar ("Marketplace" → `/admin/marketplace`).
- **Endpoint:** `admin_marketplace.py:98,198,257` (`GET /catalog`, `POST /portfolios`, `PATCH /portfolios/{id}/finalize`).

### 15. Toàn bộ route frontend/app/admin (đọc trực tiếp source, không suy từ ảnh sidebar)

20 `page.tsx` tìm thấy dưới `frontend/app/admin/`:

| Route | Trên sidebar? | Bảo vệ middleware? |
|---|---|---|
| `/admin/dashboard` | ✓ | ✓ (`admin,reviewer,content`) |
| `/admin/tenants` | ✓ | ✓ (`admin` only) |
| `/admin/marketplace` | ✓ | ✓ (`admin` only) |
| `/admin/quarter-plan` | ✓ | ✓ (`admin` only) |
| `/admin/quarter-plan/create` | không trực tiếp — có nút dẫn từ `/admin/quarter-plan` | ✓ (cùng prefix) |
| `/admin/run-health` | ✓ | ✓ |
| `/admin/atomize` | ✓ | ✓ |
| `/admin/curation` | ✓ | ✓ |
| `/admin/curation/preview` | không trực tiếp — có nút dẫn từ `/admin/curation` | ✓ (cùng prefix) |
| `/admin/upload` | ✓ | ✓ |
| `/admin/s1-rewrite` | ✓ | ✓ |
| `/admin/review` | ✓ | ✓ |
| `/admin/brand` | ✓ | ✓ |
| `/admin/master-content` | ✓ | ✓ |
| `/admin/settings` | ✓ | ✓ |
| **`/admin/pipeline/s1`** | **✗ MỒ CÔI** | ✓ (prefix `/admin/pipeline`) |
| **`/admin/pipeline/s2`** | **✗ MỒ CÔI** | ✓ |
| **`/admin/pipeline/s3`** | **✗ MỒ CÔI** | ✓ |
| **`/admin/pipeline/s4-blog`** | **✗ MỒ CÔI** | ✓ |
| **`/admin/pipeline/s4-social`** | **✗ MỒ CÔI** | ✓ |

**Xác nhận không sót:** đối chiếu `middleware.ts:55-88` (`PROTECTED_ROUTES`) — mọi prefix trong bảng trên đều có entry, không route nào rơi vào tình trạng "matcher bắt nhưng không có trong allow-list" (loại lỗi #4/#5 mà chính file middleware tự ghi lại đã từng xảy ra với AA-384/run-health/s1-rewrite/settings/brand — nay đã vá đủ).

### 16. Route mồ côi — có link dẫn tới không?

5 route `/admin/pipeline/{s1,s2,s3,s4-blog,s4-social}` — **KHÔNG có link/menu item nào dẫn tới**, xác nhận qua chính comment trong `AdminSidebar.tsx:240-244`: *"AA-390: Legacy B2B pipeline (ACP v1) sidebar entry hidden... The routes/pages... and their backend (admin_acp_proxy.py, v1_acp.py, etc.) are untouched and still reachable directly by URL if ever needed again."* — đây là quyết định CHỦ ĐỘNG (không phải bug bỏ sót), đã ghi rõ trong code. Chỉ vào được bằng gõ URL tay. Khớp hoàn toàn với phát hiện AA-385 (Giai đoạn 3 Mục 10) — cùng 1 nhóm route ACPv1 tàn dư.

`/admin/quarter-plan/create` và `/admin/curation/preview` — CÓ link (nút bấm) từ trang cha, không mồ côi dù không xuất hiện trực tiếp trên sidebar chính.

---

## Kết luận cuối — 3 danh sách rõ ràng

### ✅ Nhịp có UI thật, xác nhận dùng được (route + sidebar/link + gọi API thật, không phải demo tĩnh)
- **S0 Ingest** — Upload page, trace đủ luồng Excel→raw_tours
- **S1 Rewrite (CIS cũ)** — S1 Rewrite page, trigger thật qua `run-tour-async`
- **N1 Onboard** — Tenants page, đủ 4 bước (seed-atoms/angle/gate-a status/approve)
- **N2 Corpus** — Atomize page, trigger `atoms/decompose` thật
- **N3 Curate** — Atom Curation + preview, 5 endpoint thật
- **N5 Portfolio + Gate B** — Quarter Plan page, render + duyệt dữ liệu thật (verify chéo với Giai đoạn 2)
- **Marketplace** — Marketplace page, 3 endpoint thật

### ⚙️ Nhịp KHÔNG cần UI riêng theo đúng thiết kế (chạy ngầm/embedded, có trích PRD xác nhận)
- **N4 Strategy** — PRD: *"1×/năm + invalidation, Lambda, $0 LLM"*, không đòi UI. Thực tế: chạy đồng bộ bên trong `POST /admin/quarter-plan` (trigger qua nút "Create" ở trang Quarter Plan) — không phải Lambda tách biệt như PRD mô tả, nhưng vẫn có đường trigger thật qua UI có sẵn, không phải gap.
- **N6 Schedule** — PRD: *"1×/tháng, Lambda, $0"*, không đòi UI riêng. Thực tế: `preview-slotgrid` (gọi từ Quarter Plan + Curation Preview) — lưu ý fallback demo-mode khi chưa có dữ liệu thật (xem mục 11).
- **N5b Override** — Backlog thật trên Linear (AA-307), xác nhận đúng — 0% code/route/UI, không phải bug bỏ sót.

### 🔴 Nhịp CÓ GAP — cần UI/route nhưng chưa có, hoặc có route nhưng không dẫn được tới (QUAN TRỌNG NHẤT)
1. **S1-from-atom (N2b)** — Backend endpoint sống (`/v1/s1-from-atom` router mounted) nhưng **0 UI, 0 trang nào gọi tới**. Mọi lần chạy đều qua script tay. → *Cần: xây UI trigger, hoặc xác nhận có chủ đích để mãi mãi chỉ chạy qua script (không giống các nhịp khác).*
2. **N7 Produce** — **0 route API** (không router nào trong `api/routers/` đụng `services/acp_produce`), **0 schedule tự động thật** (EventBridge tự nhận "chưa có consumer"), **0 UI trigger**. "Run Health" — trang duy nhất tên gợi ý theo dõi N7 — **đang đọc SAI bảng** (`acp_shared.acp_runs` ACPv1 cũ, không phải `acp_v2_runs` thật). → *Đây là gap nghiêm trọng nhất trong toàn bộ audit 5 giai đoạn: N7 "chạy hàng tuần" theo PRD nhưng thực tế KHÔNG CÓ CƠ CHẾ TỰ ĐỘNG NÀO kích hoạt nó, và trang duy nhất có vẻ theo dõi nó lại theo dõi nhầm hệ thống khác.*
3. **N8 Deliver + Learn** — **0 route API, 0 UI** cho Weekly Packet/Gate C/veto — xác nhận lại đúng note cũ AA-365 "No new UI", chưa đổi gì. `acp_veto_window_close` schedule cũng chưa có consumer.
4. **`/admin/pipeline/{s1,s2,s3,s4-blog,s4-social}`** — 5 route mồ côi, chủ đích (ghi rõ trong code, không phải bug), nhưng vẫn LÀ route sống, bảo vệ đúng, gọi được nếu biết URL — rủi ro nhầm lẫn thấp hơn AA-385 đã đánh giá (Giai đoạn 3) nhưng vẫn tồn tại.
5. **N0 Enrich** — **chưa xác nhận được dứt khoát** — không tìm thấy code/route/hạ tầng nào tên "enrich" tách biệt khỏi N2. Cần Nghiep xác nhận: đã gộp vào N2 (thiết kế), hay chưa từng build (gap).

**Phát hiện phụ ngoài phạm vi 16 mục** (ghi lại vì phát hiện trong lúc trace route N2, không điều tra sâu thêm ở giai đoạn này): `v1_atoms.py`'s docstring tự cảnh báo nhánh Batch (≥100 tour) atom decompose có thể đang lỗi do `create_model_invocation_job` chưa có trong boto3 production (1.34.69) — verify độc lập ở venv riêng (1.43.49), chưa qua chính service này.

---

*Hết Giai đoạn 5. Mọi kết luận "UI dùng được" ở trên chỉ xác nhận CODE + ROUTE + LINK tồn tại — chưa có lần bấm qua browser production build thật nào trong 5 giai đoạn audit này. Nghiep tự bấm qua để xác nhận cuối cùng, đặc biệt các trang chưa từng test qua browser theo lịch sử chương trình (bài học AA-345/AA-323 S141-142).*

# AA-446-02 — Bước 2: Đối chiếu 40 mục với code/PR THẬT (26/08/2026)

Tiếp nối Bước 1 (S158, dựa Linear comment). Task này đọc CODE HIỆN TẠI trên `main` (sau
AA-441/443/444/445/437/448/449/450/451/452/454/455/456/457/458/459/460), đối chiếu độc lập qua
4 fork song song + 1 lần verify chéo thủ công khi 2 fork ra kết luận khác nhau (mục #19). Đọc/đối
chiếu thuần — KHÔNG sửa code, KHÔNG sửa Linear.

Trạng thái dùng: **Done** / **Đóng 1 phần** / **Không cần** / **Chưa làm**.

---

## AA-438 — Admin Tier (18 mục)

| # | Mô tả ngắn | Trạng thái | Nguồn xác nhận |
|---|---|---|---|
| 1 | `pipeline_runs.status` kẹt `'ingesting'` khi có tour hitl | **Chưa làm** | Đọc code hiện tại `services/export/handler.py` + `admin_pipeline.py:3392` — logic chưa đổi, không PR nào từ 22/08 đụng tới |
| 2 | Dashboard "Pipeline Activity (7d)"/"Pass Rate" bucket theo `started_at` + filter `status != 'ingesting'` → bảng rỗng dù đang rewrite thật | **Chưa làm** | Đọc code hiện tại `admin_pipeline.py:3391-3392` — filter còn nguyên. Hệ quả trực tiếp của #1 |
| 3 | Nghi double-fire ingestion (Lambda S3-trigger + Upload page `process_file()`) | **Chưa làm** | Đọc code hiện tại: vẫn gọi `process_file()` in-process, không thêm guard. Vẫn "Unconfirmed" như audit gốc — chưa verify CloudWatch |
| 4 | `pipeline_runs` UPDATE skip âm thầm khi `batch_id` không phải UUID | **Chưa làm** | Đọc code hiện tại `admin_pipeline.py:709-720` — vẫn chỉ `logger.info(...)`, không surface lỗi |
| 5 | Reject không reset `generated_content.status` khỏi `'hitl'` | **Done** | PR #194 (`17305fc`, AA-441) — `api/routers/v1_pipeline.py::reject_review()` nay UPDATE về `'rejected'`; đã fix live 2/39 row thật lúc merge |
| 6 | 11 row `review_queue` mồ côi của tenant `test-agency` | **Chưa làm** | Không có migration/script cleanup nào chạm `test-agency`/`9fb0a3db`; Data Cleanup Plan (Nghiep đã quyết định) **chưa chạy** — `test-agency` còn active, còn được dùng làm tenant test tới tận AA-458 (25/08) |
| 7 | `published_tours` UPSERT chỉ refresh 4/18 cột | **Done** | PR #194 (AA-441) — `shared/repository/published_catalog_repository.py:29-47`, `ON CONFLICT DO UPDATE` nay có đủ 17 cột non-key |
| 8 | `/v1/tours/pool` (T1) không filter `master_status`/`deleted_at` | **Done** | PR #194 (AA-441) — `api/routers/v1_tours.py:104-175`, filter `master_status='active' AND deleted_at IS NULL` đã có, comment code tự trích "AA-441 (bug #6)" |
| 9 | `shared/services/export_service.py::publish_tour()` dead code | **Done** | PR #211 (`b9c1335`, AA-454) — file bị xoá hoàn toàn (-264 dòng) kèm test riêng; `grep -rn "publish_tour"` hiện tại → 0 kết quả |
| 10 | Tour 9.0/10 vẫn bị `manual_check` chặn publish 59 ngày (brand_audit quá strict?) | **Chưa làm** | Không có task/PR follow-up điều tra hypothesis này kể từ audit gốc |
| 11 | Dashboard "LLM Calls" = `COUNT(*)` row, không phải call LLM thật | **Chưa làm** | Đọc code hiện tại `admin_pipeline.py` (~3411-3459) — vẫn `COUNT(*)` trên `generated_content`, không đổi |
| 12 | Card "Model Usage" chỉ scope `silver_aa_internal.generated_content`, không phản ánh ACP v2 tenant-facing | **Chưa làm** | Đọc code hiện tại — vẫn hardcode `tenant_slug="aa_internal"`; `tenant_tour_versions` vẫn không có cột cost/model/token nào |
| 13 | 5 thẻ "Pipeline Health" mù hoạt động admin — `tenant_api_usage` chỉ ghi path `/v1/*` | **Done** | PR #194 (AA-441), migration 110 (`actor_type`/`admin_user_id`) — `api/middleware/rate_limit.py` + `shared/services/billing_service.py` nay track cả path `/admin/*`, live-verified lúc merge |
| 14 | Run Health đọc `acp_shared.acp_runs` (0 row) thay vì `acp_v2_runs` (data thật) | **Done** | PR #194 (AA-441) — `api/routers/acp_health.py:170` nay query `acp_shared.acp_v2_runs`, docstring tự ghi lại lịch sử bug |
| 15 | 2 bảng trùng tên `acp_runs` ở 2 schema khác nhau — naming trap | **Không cần** | Cả 2 bảng vẫn tồn tại song song đúng như mô tả — chỉ là cảnh báo đặt tên, không phải bug chức năng, rename không tương xứng lợi ích |
| 16 | Settings "Pipeline Flow" chip thiếu `llm_judge`/`revalidate` | **Done** | PR #211 (AA-454) — `api/routers/admin_settings.py:15-22` nay có đủ 6 node `["generate","validate","llm_judge","brand_audit","flag_fix","revalidate"]` |
| 17 | `retry_count: 0` hardcode dead trong Run Health | **Done** | PR #211 (AA-454) — `grep -n "retry_count"` trên BE+FE hiện tại → 0 kết quả |
| 18 | `published_tours` tổng 71 vs 72 — chênh 1 row | **Chưa làm** | Không có investigate/fix nào follow-up, vẫn "flagged, out of scope" |

**AA-438: 8 Done · 1 Không cần · 9 Chưa làm** (#5,7,8,9,13,14,16,17 Done; #15 Không cần; #1,2,3,4,6,10,11,12,18 Chưa làm)

---

## AA-439 — Tenant Tier (22 mục)

| # | Mô tả ngắn | Trạng thái | Nguồn xác nhận |
|---|---|---|---|
| 1 | T0 upload brand-guide hardcode `tenant_id`, 401 mọi tenant thật | **Done** | PR #194 (AA-441) — `admin_pipeline.py::upload_brand_file` nay resolve tenant qua JWT (`_resolve_brand_tenant_id`), không hardcode; `BrandTab.tsx` đổi endpoint |
| 2 | ADR "T1 cần endpoint mới" là stale | **Không cần** | Doc staleness, tự sửa trong chính audit gốc — không có gì để fix |
| 3 | T4 (My Catalog) zero navigation đến/từ T6 | **Done** | PR #211 (`b9c1335`, AA-454) — `CatalogTab.tsx` có link → `/portal/t6-atoms?tour_id=...`, `AtomsTab.tsx` có link ngược ← `/portal/t4-pool`. Nav 2 chiều thật |
| 4 | `score_distinctiveness()` chưa từng được xây (100% atom LOW) | **Đóng 1 phần** | PR #199 (`df19ec9`, AA-445) — hàm thật tại `services/acp_shared/competitor_index.py:57`, gọi thật từ `services/acp_produce/tenant_pipeline.py:280` (đường T5 tenant-rewrite). **Nhưng** đường N2 Atomize admin/platform-scope (`v1_atoms.py:248-252`) vẫn giữ nguyên comment "AA-317, out of scope" — atom platform-scope (2551/2566 atom hiện có) vẫn flat LOW |
| 5 | Hệ quả #4: Quarter Plan/N6 allocator weight multiplier inert trên distinctiveness | **Đóng 1 phần** | Đọc code hiện tại `services/acp_planning/{quarter,allocator}.py` — công thức đã thật sự dùng `a.distinctiveness`, không còn inert về logic. **Nhưng** `quarter.py:274-279` (`fetch_atoms_by_trip`) vẫn `JOIN raw_tours WHERE rt.tenant_id=$1` (theo tenant sở hữu tour gốc), không theo `tour_atoms.owner_scope` — atom T5 của tenant rewrite tour dùng chung pool vẫn **vô hình** với N5/N6. Gap thật, chưa có ticket riêng dù phát hiện từ 25/08 (AA-445-02) |
| 6 | ADR "T6 FE 100% admin-only" là stale | **Không cần** | Doc staleness — `/portal/t6-atoms` (AA-431) đã real |
| 7 | Không có công thức kết hợp DFS+distinctiveness trong thiết kế gốc | **Không cần** | Research finding thuần, không phải bug |
| 8 | Tenant rewrite (T1/T2) chưa từng gọi DataForSEO | **Done** | PR #199 (AA-445) — `api/routers/v1_tours.py:319-380` (`_do_rewrite_and_save`) nay gọi `process_seo(...seo_mode="dataforseo")` thật khi cache-miss, không còn truyền `{}` |
| 9 | `seo_context` có thể null hoàn toàn dù row tồn tại | **Chưa làm** | Không PR nào đụng persistence logic này; hành vi null-khi-fetch-fail (best-effort) vẫn còn nguyên |
| 10 | DFS gửi 1 keyword/task thay vì batch tới 1000 — phí gấp ~100 lần | **Chưa làm** | `services/seo_intelligence/dataforseo_client.py::fetch_keywords` vẫn `"keywords": [seed]`, không batch |
| 11 | Không có handoff T7→T8 trong code (2 module cũ chưa từng gọi nhau) | **Done** | PR #208 (AA-451) — `services/acp_angle_gate/service.py::create_request()` (khi có `year`/`month`) tự tính + persist slot-grid qua ĐÚNG fetcher tenant-scoped mà T7's `GET /v1/planning/slot-grid` dùng — real HTTP-verified (`docs/implementation-notes/AA-451.md`), quyết định thiết kế (Option B) Nghiep đã xác nhận trước khi build. Kết nối thật, không phải "resolved by moot" — 2 module CŨ (N6 cũ ↔ `acp_s4_social` cũ) đúng là chưa từng nối, nhưng chuỗi kiến trúc THẬT đang chạy (T7 mới ↔ T8 mới) đã có handoff sống |
| 12 | `acp_s4_social/` 100% admin-gated, 0 caller thật ở mọi nơi | **Chưa làm** | Đọc code hiện tại: `services/acp_s4_social/` còn nguyên 10 file, vẫn `include_router` trong `api/main.py`; grep frontend cho `s4_social`/`generate`/`angles`/`write` của module này = 0 hit. Module giờ chắc chắn dead hơn (có bản thay thế T8 chạy song song) nhưng **chưa ai xoá**, chưa có quyết định |
| 13 | `trust_ramp.py::suggest_ramp_transition()` — phần tự động hoá — 0 caller | **Chưa làm (có chủ đích)** | Hàm vẫn tồn tại (`trust_ramp.py:73`), 0 caller ngoài test; `api/routers/admin_a4.py` tự ghi rõ "no `suggest_ramp_transition()` automation" — AA-437 xây UI chỉ hiển thị state hiện tại, cố ý không tự động hoá, không phải bug sót nhưng cũng chưa ai "làm" phần automation này |
| 14 | Trust ramp = thiết kế gốc chị Thư (không phải AA-365 phát minh riêng) | **Không cần** | Research correction thuần |
| 15 | Workflow 9 bước của Nghiep → trace ra SKILL_v2.md 11 bước gốc | **Không cần** | Research finding thuần |
| 16 | `ContentBrief` brand/audience required-caller-supplied (module `acp_s4_social` CŨ) | **Không cần** | Module cũ (`brief.py::ContentBrief`) không được T8/T9 mới dùng lại — câu hỏi gốc về module cũ hết áp dụng. Đáng chú ý: hệ MỚI (`acp_angle_gate/brand_audience.py`, AA-449) làm NGƯỢC giả định gốc — tự động fetch brand/audience từ `tenant_brand_rules`, không bắt caller cung cấp |
| 17 | Angle field 3-way mismatch (nguồn 3 field / code cũ 4 field / "formula fit" không tồn tại ở đâu) | **Done** | PR AA-449 (`8093645`) — `services/acp_angle_gate/generate.py:35`: `_REQUIRED_ANGLE_FIELDS = ("name","why_it_works","formula_fit","best_final_style")`. `docs/claude_tasks/AA-449-00-step0...md` xác nhận STEP0 đã đọc lại `SKILL_v2.md` gốc TRƯỚC khi build và xác định `formula_fit` là field thật cần có — quyết định thiết kế có chủ đích, không phải lệch sót tiếp diễn. (File cũ `acp_s4_social/angles.py` vẫn giữ mismatch cũ y nguyên, nhưng file đó là dead code — theo dõi ở mục #12, không tính vào đây) |
| 18 | `pipeline.py` docstring "C3/E1-E5 không tồn tại" — stale | **Không cần** | Doc correction — E1/E2/E3 thật đã chạy (`generation.py`/`adapt.py`) |
| 19 | `Channel Output Structures.xlsx` không dùng ở đâu, chỉ 2/7 kênh có code | **Đóng 1 phần** | Verify trực tiếp: `services/acp_angle_gate/channel_style.py` (AA-449) — docstring tự ghi "Verbatim from Bang 2... confirmed this is Channel Output Structures.xlsx" — bảng **7 kênh thật**, dùng thật trong `generate.py`/`prompts.py` của T8 (angle generation, "best final style"). **Nhưng** `services/acp_produce/adapt.py` (E3, N7 — bước viết content CUỐI) vẫn `Literal["facebook","tiktok"]`, chỉ 2/7 kênh, không đổi. → T8 (chọn angle) đã dùng xlsx thật cho 7 kênh; T9/N7 (viết nội dung cuối) vẫn chưa |
| 20 | T10's F1-F9 = implementation riêng biệt với T3's QA check | **Không cần** | Clarification thuần, không phải bug |
| 21 | T11 (Publish) hoàn toàn chưa tồn tại — chỉ DB flip | **Đóng 1 phần** | Chuỗi PR #213 (AA-455, migration 116 `publish_log`) → #215 (AA-457, migration 117 `tenant_integrations`+WordPress connect) → #217 (AA-460, fix false-positive test-connection) → #219 (AA-458, `v1_publish.py` + `WordPressAdapter.create_post()` thật qua `aiohttp`, yêu cầu `id`+`link` thật trước khi ghi `status='published'`). **Giới hạn thật, tự ghi trong CLAUDE.md**: chỉ scope `channel='blog'`, 6 kênh còn lại chưa có code; positive-path (publish thành công thật) mới proven qua unit test, CHƯA live-verify E2E do site test `aa-wordpress.rf.gd` bị anti-bot chặn |
| 22 | 0 piece nào vượt `status='ready'`; `acp_shared.usage_log` không tồn tại | **Đóng 1 phần** | Migration 116 (AA-455) tạo `acp_shared.publish_log` — bảng delivery-accounting thật, thay thế vai trò `usage_log` cho pipeline MỚI (`content_piece`/T9-T11, scope blog). **Nhưng** đây không phải fix trực tiếp bug gốc: bug #22 nói về N8's `acp_deliver.packets`, và packets đó **vẫn stuck ở `status='ready'`**, KHÔNG có bảng accounting riêng nào được tạo cho N8 — 2 pipeline khác nhau (N8 packets cũ vs. content_piece/T11 mới), chỉ 1 trong 2 được giải quyết |

**AA-439: 5 Done · 5 Đóng 1 phần · 8 Không cần · 4 Chưa làm** (#1,3,8,11,17 Done; #4,5,19,21,22 Đóng 1 phần; #2,6,7,14,15,16,18,20 Không cần; #9,10,12,13 Chưa làm)

---

## Tổng hợp 40 mục

| Trạng thái | AA-438 | AA-439 | Tổng |
|---|---|---|---|
| ✅ Done | 8 | 5 | **13** |
| 🟡 Đóng 1 phần | 0 | 5 | **5** |
| ➖ Không cần | 1 | 8 | **9** |
| 🔲 Chưa làm | 9 | 4 | **13** |
| **Tổng** | **18** | **22** | **40** |

## Việc cần Nghiep quyết định (không tự ý làm trong task đọc-thuần này)

1. **AA-439 #12** — `acp_s4_social/` giờ chắc chắn dead (T8 mới đã thay thế, chạy live song song). Xoá hay giữ?
2. **AA-439 #5** — bug `owner_scope` vs `tenant_id` tại `quarter.py:279` (N5/N6 không thấy atom T5 dùng-chung-pool) chưa có ticket riêng dù phát hiện từ 25/08. Là blocker thật cho toàn bộ giá trị của #4/#5.
3. **AA-438 #12** — mở rộng cost-tracking cho `tenant_tour_versions` cần quyết định thiết kế trước khi build cùng #11.
4. **AA-438 #10, #18** và **AA-439 #9, #10** — vẫn ở dạng hypothesis/investigate, chưa ai giao việc điều tra riêng.
5. **AA-439 #21/#22** — có mở rộng T11 ra 6 kênh còn lại không, và có cần site WordPress test không bị anti-bot chặn để live-verify positive-path thật không.
6. **AA-439 #13** — automation `suggest_ramp_transition()` có nên được nối dây hay giữ mãi mãi manual-only (theo đúng AA-437 design)?

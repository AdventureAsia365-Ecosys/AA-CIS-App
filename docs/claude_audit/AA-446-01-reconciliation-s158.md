# AA-446-01 — Reconciliation S158 (24/08/2026)

Đọc/đối chiếu thuần, KHÔNG sửa code. Nguồn đọc:
- `docs/claude_audit/AA-438-00-SUMMARY-admin-tier-audit.md` (PR #193, merged, commit `950a159`)
- `docs/claude_audit/AA-439-00-SUMMARY-tenant-tier-audit.md` (cùng PR #193)
- Linear issue AA-446 — 2 comment: comment gốc (23/08/2026, đối chiếu đầy đủ 40 mục lần đầu) +
  comment S158 (24/08/2026, cập nhật sau khi T7/T8/T9/T10 xong)
- **AA-447 (ma trận đồng bộ theo stage)**: KHÔNG tồn tại như file trong `docs/claude_audit/`
  (`find docs/claude_audit -iname "*447*"` → 0 kết quả). AA-447 chỉ tồn tại như **Linear issue**
  (`AT: sync audit — rà soát đồng bộ BE/FE...`), nội dung của nó đã được tóm tắt trực tiếp trong
  comment gốc của AA-446 (đoạn "Cập nhật sau AA-447"). Không có gì thêm để đọc ngoài repo.

Số liệu chốt: **40 mục** (18 AA-438 + 22 AA-439), không phải 41 như tiêu đề AA-446 ghi nhầm gốc
(đã tự sửa trong Linear).

---

## Phần 1 — Toàn bộ 40 mục, đánh số đúng thứ tự file gốc, trạng thái theo comment Linear mới nhất (S158)

### AA-438 — Admin Tier (18 mục)

| # | Mô tả | Trạng thái (Linear, mới nhất) |
|---|---|---|
| 1 | `pipeline_runs.status` kẹt `'ingesting'` vĩnh viễn khi batch có ≥1 tour hitl — không path nào flip nó | 🔲 Chưa làm |
| 2 | Dashboard "Pipeline Activity (7d)"/"Pass Rate" bucket theo `started_at` (giờ ingest, không phải giờ rewrite) + filter `status != 'ingesting'` → kết hợp #1, bảng luôn rỗng dù đang rewrite thật | 🔲 Chưa làm |
| 3 | Nghi ngờ double-fire ingestion: Lambda S3-trigger (`aa-cis-dev-ingestion`) VÀ Upload page in-process `process_file()` cùng fire trên 1 S3 object | 🔲 Chưa làm — **Unconfirmed**, chưa verify qua CloudWatch |
| 4 | `pipeline_runs` accounting UPDATE bị skip âm thầm khi `batch_id` không phải UUID hợp lệ (run ad-hoc/verify) | 🔲 Chưa làm — minor, chỉ log |
| 5 | Reject (`/admin/review-queue/{id}/reject`) không reset `generated_content.status` về từ `'hitl'` — tour bị reject vẫn nhãn `hitl` vĩnh viễn | ✅ **Done** — AA-441 (PR #194), 2/39 row thật đã UPDATE trực tiếp |
| 6 | 11 row `review_queue` của tenant `test-agency` (`9fb0a3db-...`) mồ côi — `review_status='pending'` nhưng không có `generated_content` khớp | 🔲 Chưa làm — nằm trong Data Cleanup Plan (test-agency sẽ bị xoá khi cleanup chạy) nhưng cleanup **chưa chạy** |
| 7 | `published_tours` `ON CONFLICT DO UPDATE` chỉ refresh 4/18 cột (`generated_content_id`/`aa_name`/`quality_score`/`published_at`) — republish sau rewrite mới để lại subtitle/SEO cũ | ✅ **Done** — AA-441 (PR #194), verify double-republish 18/18 cột trước merge |
| 8 | `/v1/tours/pool` (T1 tenant-facing) thiếu filter `master_status`/`deleted_at` — tour admin đã trash vẫn hiện + rewrite được cho mọi tenant | ✅ **Done** — AA-441 (PR #194), live-verified trash/soft-delete ẩn đúng |
| 9 | `shared/services/export_service.py::ExportService.publish_tour()` là dead code — 0 caller, tham chiếu cột `slug`/`country`/`is_active` không tồn tại trên `published_tours` thật | 🔲 Chưa làm — low severity, dễ gây hiểu nhầm nếu đọc lạnh |
| 10 | Tour live-test ở AA-438-03: 9.0/10 tổng, 10/10 brand fit, narrative khen hết lời — nhưng `brand_audit_status='manual_check'` chặn publish 59 ngày | 🔲 Chưa làm — deliberately chưa investigate, cần task follow-up riêng cho hypothesis "brand_audit quá strict" |
| 11 | Dashboard "Model Usage"/"LLM Calls": `calls` = `COUNT(*)` row `generated_content` xong, không phải call LLM thật — undercount (bỏ sót judge/brand_audit/flag_fix/nudge sub-call) | 🔲 Chưa làm |
| 12 | Card trên chỉ scope `silver_aa_internal.generated_content` — cấu trúc không thể phản ánh usage ACP v2 tenant-facing (T-series); `tenant_tour_versions` không có cột cost/model/token nào | 🔲 Chưa làm |
| 13 | Cả 5 thẻ "Pipeline Health" mù 100% hoạt động admin — `tenant_api_usage` (nguồn dữ liệu duy nhất) chỉ được `rate_limit_middleware` ghi cho path `/v1/*`, bỏ qua mọi path không phải `/v1/*` trước cả khi check auth | ✅ **Done** — AA-441 (PR #194, migration 110: cột `actor_type`/`admin_user_id`), verify live admin call ghi đúng row |
| 14 | Run Health đọc `acp_shared.acp_runs` (0 row, chỉ ghi bởi ACP v1 legacy S2/S3/S4 blog/social) — pipeline thật đang chạy (N7/N8, 12 run thật) ghi vào bảng KHÁC, `acp_shared.acp_v2_runs`, chưa từng được đọc | ✅ **Done** — AA-441 (PR #194), verify live `GET /admin/acp/run-health` → 200, 12 run thật (trước đó 0) |
| 15 | 2 bảng trùng tên `acp_runs` ở 2 schema khác nhau (`shared.acp_runs` — manifest tracker cũ, batch-keyed; `acp_shared.acp_runs` — ACP v1 run tracker cũ, run_id-keyed) — bẫy đặt tên suýt gây đọc sai chính trong lúc audit | ➖ **Không cần làm** — không phải bug chức năng, chỉ là cảnh báo naming trap |
| 16 | Settings "Pipeline Flow" chip UI hardcode 4 node (`generate/validate/brand_audit/flag_fix`), thiếu 2 node thật (`llm_judge`, `revalidate`) | 🔲 Chưa làm |
| 17 | `retry_count: 0` hardcode cứng, vô điều kiện, trong response Run Health, kèm comment tự giải thích (schema không track được) — xác nhận dead, không render ở FE | 🔲 Chưa làm — very low severity |
| 18 | `published_tours` tổng 71 (hiện tại) vs 72 (AA-438-01 ghi) — chênh 1 row, có trước cả live test AA-438-03 | 🔲 Chưa làm — flagged, chưa chased down |

**Ghi nhận thêm (không phải bug, đã điều tra và loại trừ trong chính audit gốc — không tính vào 40 mục, không cần hành động):** Atomize (N2) và Atom Curation **không** trùng lặp/legacy với T5/T6 — khác scope thật, khuyến nghị giữ cả 2. Quarter Plan (Gate B), Produce & Deliver (N7/N8), Marketplace, Tenants, Brand Identity, tab SEO Intelligence, tab Content Library — tất cả real, live-query, không mock data, không phát hiện bug.

### AA-439 — Tenant Tier (22 mục)

| # | Mô tả | Trạng thái (Linear, mới nhất) |
|---|---|---|
| 1 | T0 upload brand-guide hardcode `tenant_id=aa_internal` VÀ 401 thẳng mọi tenant thật (route qua admin-only proxy, check cookie tenant không có) | ✅ **Done** — AA-441 (PR #194), live-verified JWT tenant thật, file đến đúng S3 prefix |
| 2 | ADR "T1 cần endpoint mới" — framing đã stale, T1 nút "Rewrite" thật đã chạy cả chuỗi T2→T3→T5 trong 1 job (AA-425/436) | ➖ **Không cần làm** — không phải bug, doc staleness tự sửa trong chính audit |
| 3 | T4 ("My Catalog") **zero navigation** đến/từ T6 — tenant không biết rewrite ra atom trừ khi tự tìm "Atom Curation" ở sidebar | 🔲 Chưa làm — AA-444 (Marketplace view mới) có liên quan nhưng KHÔNG đóng mục này: xây `/portal/marketplace` (view tổng hợp T4+T6 mới) chứ không thêm link điều hướng 2 chiều vào `CatalogTab`/`AtomsTab` hiện có |
| 4 | `score_distinctiveness()` chưa từng được xây (ticket AA-317) — 100% trong 2566 atom là `distinctiveness='LOW'`, weight=1.0 phẳng | ✅ **Done** — AA-445 (PR #199): B4 `CompetitorIndex` + `score_distinctiveness()` xây xong, N5/N6 đọc thật |
| 5 | Hệ quả #4: công thức trip-scoring Quarter Plan + hệ số nhân N6 allocator hoàn toàn trơ (inert) trên chiều distinctiveness | ✅ **Done** (cơ chế) — AA-445 (PR #199); lưu ý: mặc định vẫn trả `MED` khi tenant chưa nhập domain đối thủ — có chủ đích, không phải bug sót |
| 6 | ADR "T6 FE 100% admin-only, chưa build" — stale, `/portal/t6-atoms` thật, tenant-scoped, từ AA-431 | ➖ **Không cần làm** — doc staleness, tự sửa trong audit |
| 7 | Không có công thức kết hợp DFS+distinctiveness nào trong thiết kế gốc `aa-marketing-v2` (đọc toàn bộ folder, xác nhận) | ➖ **Không cần làm** (hiện tại) — research finding, không phải bug; thiết kế công thức mới là quyết định riêng chưa có task |
| 8 | Tenant rewrite (T1/T2) **chưa từng gọi DataForSEO** — chỉ pipeline admin/A1 gọi | ✅ **Done** — AA-445 (PR #199): 2 call site T2 (`v1_tours.py:317-321`, `tenant_pipeline.py:152-154`) giờ truyền `seo_data` thật thay vì `{}` |
| 9 | Dữ liệu DFS đã persist (`seo_context`) có thể hoàn toàn null kể cả khi row tồn tại (xác nhận với row mới nhất, mọi field số đều null) | 🔲 Chưa làm — caveat cho thiết kế tương lai |
| 10 | Client thật gửi 1 keyword/task DataForSEO, trả phí flat ($0.09) dù có thể gộp tới 1000 keyword cùng giá (~$467 vs ~$4-5 cho 2566 atom nếu batch) | 🔲 Chưa làm — real inefficiency đã quantify, chưa xử lý |
| 11 | **Không có handoff T7→T8 trong code, không phải "chưa nối dây" mà là chưa từng nối** — N6 planning pipeline và module angle-generation (`acp_s4_social`) chưa từng gọi nhau | 🔲 Chưa làm (theo Linear) — **xem Phần 3, mục #11: có khả năng đã moot/resolved khác đi bởi AA-448+AA-449+AA-451, cần xác nhận lại** |
| 12 | Module angle-generation thật, đầy đủ, hoạt động (`acp_s4_social/`) 100% admin-gated, **zero row bao giờ** trong mọi environment; xác nhận không có caller nào ở cả FE lẫn BE cho `/generate`/`/angles`/`/write` | 🔲 Chưa làm (theo Linear) — **xem Phần 3, mục #12: module cũ này bị thay thế bởi T8 viết mới (AA-449), không phải "sửa" mà là "superseded" — cần quyết định giữ/xoá code cũ** |
| 13 | `trust_ramp.py::suggest_ramp_transition()` (tự động chuyển trust-ramp state) zero caller — dead code; chỉ admin tay đổi state, và điều đó **chưa từng xảy ra** (`audit_log` 0 row `publish_mode_transition`) | 🔲 Chưa làm (phần tự động hoá) — AA-437 (A4 Trust Ramp v1, PR #196/#198) đã build UI CHỈ hiển thị state hiện có, **cố ý KHÔNG tự động hoá** `suggest_ramp_transition()` — quyết định ghi rõ, dead code vẫn dead |
| 14 | Corrected: trust ramp CHÍNH LÀ thiết kế gốc chị Thư (`aamc/delivery.py::publish_gates()`, khớp từng dòng), không phải AA-365 phát minh riêng | ➖ **Không cần làm** — research correction |
| 15 | Workflow 9 bước của Nghiệp trace về nguồn thật 11 bước (`SKILL_v2.md`'s "Human-In-The-Loop Workflow"), không phải `aa-marketing-v2`; 7/11 bước khớp code hiện tại | ➖ **Không cần làm** — research finding |
| 16 | Brand/audience là input **required, do caller cung cấp** trong `ContentBrief` (module `acp_s4_social` cũ) — không tự áp giá trị cố định như giả định ban đầu; không kiểm chứng đầy đủ được vì #12 (không ai gọi module) | 🔲 Chưa làm (theo Linear) — **xem Phần 3, mục #16: finding này nói về `ContentBrief` của module CŨ (`acp_s4_social`), T8 mới (AA-449) không dùng lại module này — cần đối chiếu lại với T9's `ContentBrief` mới nếu có khái niệm tương đương** |
| 17 | Format field output angle lệch 3 chiều: nguồn (`SKILL_v2.md`) muốn 3 field (Name/Why it works/Best final style); code cũ (`angles.py`) có 4 field (`name`/`why_it_works`/`length_signal`/`style_signal`); "formula fit" không tồn tại ở cả nguồn lẫn code cũ | 🔲 Chưa làm (theo Linear) — **xem Phần 3, mục #17: T8 mới (AA-449) LIVE STATE tự mô tả field Angle là `name`/`why_it_works`/`formula_fit`/`best_final_style` — trông như đã đổi khác, có `formula_fit` thật — cần xác nhận đây có phải "đã đóng theo cách khác" hay chỉ trùng tên field ngẫu nhiên** |
| 18 | Docstring `pipeline.py` tự nhận "C3/E1-E5 không tồn tại ở đâu trong repo này" — stale, E1/E2/E3 thật, chạy Sonnet, hoạt động (`generation.py`, `adapt.py`) | ➖ **Không cần làm** — doc staleness, tự sửa trong audit |
| 19 | `Channel Output Structures.xlsx` **không được dùng ở đâu** trong N7 thật — chỉ 2/7 kênh (facebook, tiktok) có code, prompt viết độc lập, không mang theo nội dung cấu trúc/avoid-list riêng của file | 🔲 Chưa làm |
| 20 | T10's F1-F9 gate stack là port thật từ `aa-marketing-v2`, nhưng là **implementation riêng biệt** với T3's QA check — chỉ share 1 hàm grounding nhỏ, không share cả stack | ➖ **Không cần làm** — clarification, không phải bug |
| 21 | T11 (Publish) xác nhận vắng mặt sạch — theo đúng docstring của chính code: `deliver_packet()`'s "delivered" chỉ là flip cột DB, không có tích hợp API social-platform thật nào ở bất kỳ đâu trong codebase | 🔲 Chưa làm — **xem Phần 3, mục #21: đây chính là mảnh còn thiếu duy nhất trong chuỗi T7-T11, ưu tiên S158 theo comment Linear mới nhất** |
| 22 | Không piece "passed" nào (trong 10 piece thật) từng vượt qua `status='ready'` — 0 `delivered_at` ở bất kỳ đâu; bảng `acp_shared.usage_log` (delivery-accounting) **không tồn tại** trong DB | 🔲 Chưa làm — **xem Phần 3, mục #22: gắn liền T11, cùng nhóm ưu tiên** |

**Ghi nhận thêm (không phải bug — nguồn AA-439-00 "Confirmed NOT bugs"):** #2, #6, #7, #14, #15, #18, #20 ở trên đều là doc-staleness correction hoặc research clarification, không phải defect trong hệ thống đang chạy.

---

## Phần 2 — Tổng đếm (khớp Linear, không đổi kể từ comment gốc 23/08 → S158)

| Trạng thái | Số lượng |
|---|---|
| ✅ Done | 9 |
| ➖ Không cần làm | 8 |
| 🔲 Chưa làm | 23 |
| **Tổng** | **40** |

Comment S158 xác nhận: **con số 23/9/8 không đổi** giữa 2 lần đối chiếu — thay đổi lớn nhất là bối
cảnh xung quanh (T7/T8/T9/T10 giờ đã Done), không phải việc đóng thêm mục nào trong chính 40 mục
gốc. Không có mục nào trong 40 mục được Linear tự đánh dấu ✅ Done nhờ AA-448/449/450/451/452 —
các issue đó là build MỚI (T7 viết lại, T8 viết mới, T9+T10-inline mới, T7 slot-persist fix, T10
mở rộng gate), không phải "đóng" 1 mục cụ thể nào trong 40 mục gốc theo cách Linear đã ghi nhận.

---

## Phần 3 — Chi tiết 23 mục "Chưa làm", đối chiếu phụ thuộc T7(AA-448)/T8(AA-449) nay đã Done

Cột "Phụ thuộc T7/T8" trả lời: mục này có nằm trong nhóm "chờ T7/T8 xong mới có ý nghĩa build
tiếp" theo comment gốc AA-446 không, và trạng thái đó có đổi không nay T7/T8 (+T9/T10/T7-slot-fix)
đã Done trong S157 (23-24/08/2026, AA-448/449/450/451/452).

### Nhóm A — AA-438 (12 mục), toàn bộ KHÔNG phụ thuộc T7/T8 (thuộc lớp Admin dashboard/pipeline độc lập)

| STT gốc | Mô tả ngắn | Issue | Phụ thuộc T7/T8? | Sẵn sàng build/investigate ngay? |
|---|---|---|---|---|
| AA-438 #1 | `pipeline_runs.status` kẹt `'ingesting'` khi có tour hitl | AA-438 | Không | **Có** — độc lập hoàn toàn, có thể làm bất kỳ lúc nào |
| AA-438 #2 | Dashboard Pipeline Activity/Pass Rate luôn rỗng (bucket sai + filter sai) | AA-438 | Không | **Có** — cùng cụm fix với #1 (root cause chung một phần) |
| AA-438 #3 | Nghi double-fire ingestion Lambda + Upload page | AA-438 | Không | Cần điều tra CloudWatch trước (chưa confirm là bug thật) — **sẵn sàng để investigate, chưa sẵn sàng để "fix"** |
| AA-438 #4 | `pipeline_runs` UPDATE skip âm thầm khi batch_id không phải UUID | AA-438 | Không | **Có** — minor, độc lập |
| AA-438 #6 | 11 row `review_queue` mồ côi của test-agency | AA-438 | Không | Phụ thuộc Data Cleanup Plan (đã quyết định, chưa chạy) — **sẵn sàng khi cleanup chạy, không phải chờ T7/T8** |
| AA-438 #9 | `export_service.py::publish_tour()` dead code | AA-438 | Không | **Có** — xoá file là việc độc lập, rủi ro thấp |
| AA-438 #10 | brand_audit quá strict (case 9.0/10 vẫn bị manual_check 59 ngày) | AA-438 | Không | Cần task điều tra riêng (hypothesis, chưa có kết luận) — **sẵn sàng để investigate** |
| AA-438 #11 | Dashboard "LLM Calls" undercounts (COUNT(*) row, không phải call thật) | AA-438 | Không | **Có** — độc lập |
| AA-438 #12 | Dashboard "Model Usage" card chỉ scope silver_aa_internal, không thấy T-series | AA-438 | Không | **Có** — cùng cụm #11, nhưng cần quyết định thiết kế (thêm cost tracking cho tenant_tour_versions?) trước khi build |
| AA-438 #16 | Settings "Pipeline Flow" chip thiếu 2 node (llm_judge, revalidate) | AA-438 | Không | **Có** — very low effort, độc lập |
| AA-438 #17 | `retry_count: 0` hardcode dead trong Run Health response | AA-438 | Không | **Có** — very low severity, dọn tiện tay |
| AA-438 #18 | published_tours 71 vs 72 discrepancy | AA-438 | Không | Cần điều tra trước khi có action rõ ràng — **sẵn sàng để investigate** |

### Nhóm B — AA-439 (11 mục)

| STT gốc | Mô tả ngắn | Issue | Phụ thuộc T7/T8? | Sẵn sàng build/investigate ngay? |
|---|---|---|---|---|
| AA-439 #3 | T4↔T6 zero navigation (tenant không biết rewrite ra atom) | AA-439 | Không (UI nav gap, độc lập với T7/T8 backend) | **Có** — độc lập, effort thấp (thêm link/badge trong `CatalogTab`/`AtomsTab`) |
| AA-439 #9 | `seo_context` có thể hoàn toàn null dù row tồn tại | AA-439 | Không | Caveat thiết kế, chưa có fix cụ thể được đề xuất — **sẵn sàng để investigate/design**, không phải "build" ngay |
| AA-439 #10 | DFS gửi 1 keyword/task thay vì batch tới 1000 — phí gấp ~100 lần | AA-439 | Không | **Có** — độc lập, đã quantify rõ ràng, chỉ cần quyết định làm hay không |
| AA-439 #11 | Không có handoff T7→T8 trong code (module cũ chưa từng gọi nhau) | AA-439 | **CÓ — đây chính là mục "chờ T7/T8 xong"** | **Trạng thái đã đổi về bản chất, cần re-verify chứ không còn "chưa làm" theo nghĩa cũ**: T7 viết lại (AA-448) + T8 viết mới (AA-449, KHÔNG port `acp_s4_social`) + AA-451 (T8's `create_request()` giờ tự tính slot-grid qua CÙNG fetcher tenant-scoped mà T7's `GET /v1/planning/slot-grid` dùng, khi thiếu slot) — về mặt chuỗi MỚI (T7-rewrite → T8-rewrite), handoff nay **đã tồn tại thật, real HTTP-verified**. Finding gốc nói về 2 module CŨ (N6 planning cũ + `acp_s4_social` cũ) — 2 module đó vẫn chưa từng gọi nhau, nhưng chúng đã bị thay thế bởi chuỗi mới. **Khuyến nghị: đóng mục này trong AA-446 với ghi chú "resolved differently — qua T7/T8 rewrite, không phải fix trực tiếp code cũ", không phải để nguyên 🔲.** Đây là việc CẦN Nghiệp xác nhận, không tự ý đổi trạng thái Linear trong task đọc thuần này. |
| AA-439 #12 | `acp_s4_social/` (angle-gen cũ) 100% admin-gated, 0 row, 0 caller | AA-439 | **CÓ — cùng phạm vi T8 rewrite** | T8 mới (AA-449) đã thay thế hoàn toàn theo ADR §0.5 ("KHÔNG reuse `acp_s4_social`"). Module cũ giờ là dead code chắc chắn hơn trước (trước đây "chưa ai gọi", giờ "có bản thay thế đang chạy live song song"). **Sẵn sàng ngay**: quyết định xoá `acp_s4_social/` (dọn dead code) hoặc giữ lại tham khảo — việc nhỏ, không cần thiết kế thêm |
| AA-439 #13 | `trust_ramp.py::suggest_ramp_transition()` tự động hoá — dead code | AA-439 | Không (liên quan T8/delivery nhưng là quyết định thiết kế riêng của AA-437, không phải chờ T7/T8) | **Có, nhưng có chủ đích chưa làm** — AA-437 đã quyết định KHÔNG tự động hoá (v1 chỉ hiển thị). Sẵn sàng build bất kỳ lúc nào nếu Nghiệp đổi quyết định, không bị block bởi gì |
| AA-439 #16 | ContentBrief brand/audience required-caller-supplied (module cũ) | AA-439 | **CÓ — cùng phạm vi T8 rewrite** | Finding gốc nói về `acp_s4_social/brief.py::ContentBrief` — module KHÔNG được dùng trong T8 mới. T9 mới (AA-450, `services/acp_content_writing/`) có khái niệm brief/prompt riêng, viết fresh. **Cần đối chiếu lại xem T9 mới có cùng câu hỏi "brand/audience có tự áp cố định không" hay đã trả lời khác đi** — chưa tự động resolved, nhưng câu hỏi gốc (về module cũ) coi như moot vì module đó không chạy. Sẵn sàng để investigate lại trên code MỚI. |
| AA-439 #17 | Format field angle lệch 3 chiều (Name/Why/Best-final-style vs 4-field code cũ, formula_fit ở đâu cũng không có) | AA-439 | **CÓ — cùng phạm vi T8 rewrite** | T8 mới (AA-449)'s LIVE STATE tự mô tả: "Angle" = 3 option LLM-generated per (atom, channel) với field `name`/`why_it_works`/`formula_fit`/`best_final_style`. Đây trông như bản build MỚI đã tự chọn field set riêng — có `formula_fit` (field mà audit gốc nói "không tồn tại ở đâu cả", giờ đã tồn tại thật trong T8 mới), và số field/tên field khác cả nguồn `SKILL_v2.md` (3 field) lẫn code cũ `angles.py` (4 field khác tên). **Cần Nghiệp xác nhận đây là quyết định thiết kế có chủ đích khi viết T8 mới (đã đọc `SKILL_v2.md` thật, theo LIVE STATE ghi "tham chiếu SKILL_v2.md gốc") hay là lệch tiếp tục, giờ ở dạng khác** — không tự kết luận trong task đọc thuần này. |
| AA-439 #19 | `Channel Output Structures.xlsx` không dùng ở đâu, chỉ 2/7 kênh có code | AA-439 | Không trực tiếp (liên quan N7/E3 channel adaptation, không phải T7/T8/T9/T10 chain) | AA-450/AA-452 vừa mở rộng T9/T10 riêng cho channel `blog` (mở rộng gate 6→9, thêm markdown/FAQ/citation cho writer) — đây là hướng mở rộng SONG SONG, không phải cùng việc "dùng xlsx cho 5 kênh còn lại của E3". **Sẵn sàng để investigate/build độc lập**, không bị block, nhưng lưu ý đã có 1 tiền lệ mở-rộng-theo-channel mới (blog) có thể tham khảo cách làm |
| AA-439 #21 | T11 (Publish) vắng mặt hoàn toàn — chỉ DB flip, không tích hợp social-platform | AA-439 | **CÓ — trực tiếp nhất trong toàn bộ 23 mục.** Đây LÀ mảnh cuối chuỗi T7→T8→T9→T10→T11 | **Sẵn sàng ngay, và là ưu tiên đã nêu rõ trong comment Linear S158**: "T11 (Publish) vẫn là mảnh còn thiếu duy nhất để hoàn tất trọn chuỗi T7-T11 — chưa bắt đầu thiết kế, việc ưu tiên của S158." T7/T8/T9/T10 đều Done + real-HTTP-verified, không còn gì chặn việc bắt đầu thiết kế T11 |
| AA-439 #22 | 0 piece nào vượt `status='ready'`; `acp_shared.usage_log` không tồn tại | AA-439 | **CÓ — cùng nhóm T11** | **Sẵn sàng ngay, cùng lúc với #21** — usage_log/delivered_at là chính cơ chế T11 cần xây; không tách rời được khỏi việc thiết kế T11 |

---

## Phần 4 — Tóm tắt cho quyết định tiếp theo

- **23 mục 🔲 vẫn đứng nguyên số lượng** — không mục nào trong 40 mục gốc được Linear tự động
  đóng bởi AA-448/449/450/451/452 (đây là các issue build MỚI, không phải fix trực tiếp 1 mục cụ
  thể trong sổ cái).
- **2 mục sẵn sàng ngay lập tức, đã được xác nhận là ưu tiên S158 trong chính comment Linear**:
  AA-439 #21 (T11 Publish) + #22 (usage_log/delivered lifecycle) — không còn gì chặn, T7-T10 đã
  xong và real-HTTP-verified.
- **4 mục cần Nghiệp xác nhận lại cách phản ánh trong AA-446, KHÔNG tự đổi trạng thái ở đây**
  (task này chỉ đọc/liệt kê, không sửa Linear):
  - AA-439 #11 (T7→T8 handoff) — có vẻ **resolved differently** qua T7/T8 rewrite + AA-451, không
    phải "chưa làm" nguyên trạng nữa.
  - AA-439 #12 (`acp_s4_social` dead) — module cũ giờ **chắc chắn dead hơn trước** (có bản thay
    thế đang chạy), sẵn sàng cho quyết định xoá/giữ, việc nhỏ.
  - AA-439 #16 (ContentBrief brand/audience) — câu hỏi gốc nói về module cũ không còn chạy; cần
    hỏi lại trên code T9 mới, chưa tự động resolved.
  - AA-439 #17 (angle field mismatch) — T8 mới có vẻ đã tự chọn field set riêng (bao gồm cả
    `formula_fit`, field mà audit gốc nói "không tồn tại ở đâu"), cần xác nhận đây là quyết định
    có chủ đích hay lệch tiếp tục ở dạng mới.
- **17 mục còn lại (12 AA-438 + 5 AA-439: #3,#9,#10,#13,#19) hoàn toàn độc lập với T7/T8/T9/T10**
  — sẵn sàng để làm bất kỳ lúc nào, không có gì chặn, thứ tự ưu tiên là quyết định riêng (nhiều
  mục severity thấp: #16/#17/#18 AA-438, #4 AA-438).

Không commit, không tạo PR, không sửa Linear — chờ Nghiệp xem trực tiếp trước khi quyết định bước
tiếp theo.

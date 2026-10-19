# Rà soát ACPv2 — Giai đoạn 2/4: Verify trạng thái THẬT (code + DB)

*14-15/08/2026. Verify 4 mục ưu tiên bằng code thật + query DB thật (S3-mediated ECS exec, `aa-cis/dev/rds`). KHÔNG kết luận chồng lấn (Giai đoạn 3), KHÔNG viết tài liệu tổng hợp cuối (Giai đoạn 4). KHÔNG sửa code/DB/Linear status nào trong phiên này.*

AWS xác nhận trước khi verify: account `[acc2 id]` (profile `aa365-admin`) đúng · ECS `aa-cis-dev-api` desired=1/running=1/ACTIVE · RDS `aa-cis-dev-db` available.

---

## Mục 1 — S1 CIS cũ có thật sự 100% GPT-4.1 không?

### Câu trả lời: KHÔNG — đã sai kể từ ~16/07/2026, và Giai đoạn 1 mang theo claim cũ của PRD mà chưa verify lại bằng DB thật.

**Code thật (`services/content_generation/graph.py:283-340`, `generate_node` — đúng node S1 CIS gốc, KHÔNG phải `s1_from_atom.py`):**
```
request = LLMRequest(system_prompt=system, user_prompt=prompt,
                      model_tier=state.get("model_tier", "haiku"))
resp = client.generate(request)   # client = LLMClient()
```
→ S1 CIS gọi thẳng `LLMClient.generate()` — CÙNG client N7/N2b dùng, không phải đường riêng.

**`shared/llm_client/client.py:30-141`** — chuỗi fallback thật (docstring dòng 32-41, xác nhận qua `git log`):
```
T1 Sonnet (acc2 native) → T1.5a Sonnet satellite acc3 (AA-397) → T1.5b Sonnet satellite acc1 (AA-296)
→ T2 Haiku (acc2 native, mặc định) → T2.5a Haiku satellite acc3 → T2.5b Haiku satellite acc1
→ T3 GPT-4.1 (LAST RESORT — chỉ khi CẢ 6 tầng trên đều fail)
```
`git log --follow client.py`: satellite acc1 được wire vào ĐÚNG chuỗi này từ **16/07/2026** (`c445da5`, `d9212b5`, [AA-296]) — cùng ngày PRD tự ghi "AA-296 Done 16/07". Không có nhánh code nào ép S1 CIS đi thẳng GPT-4.1; `DEFAULT_MODEL_TIER` không được set trong Terraform (`grep -rn DEFAULT_MODEL_TIER AA-CIS-Infra/` → 0 kết quả) → dùng default code `"haiku"`.

**DB thật (`silver_aa_internal.generated_content`, 222 row, truy vấn 15/08):**
| requested_tier | fallback_used | satellite_account | n | khoảng thời gian |
|---|---|---|---|---|
| null (trước mig 070) | false | null | 123 | 22/05 → 24/06 |
| haiku | false | **acc1** | 36 | **21/07 → 01/08** |
| haiku | false | null (acc2 native) | 20 | 25/06 → 16/07 |
| sonnet | false | null (acc2 native) | 8 | 24/06 → 16/07 |
| sonnet | false | **acc3** | 6 | **13/08** |
| haiku | true (→GPT-4.1) | null | 5 | 08/07 → 16/07 |
| haiku | false | acc3 | 2 | 12/08 |
| sonnet | true (→acc3 haiku) | acc3 | 2 | 13/08 |
| gpt-4.1 (yêu cầu trực tiếp) | false | null | 1 | 25/06 |
| sonnet | false | acc1 | 1 | 16/07 |

**`model_used` distribution 90 ngày:** `us.anthropic.claude-haiku-4-5` 54 (latest 04/07) · `us.anthropic.claude-sonnet-4-5` 48 (latest 26/06) · `satellite-haiku-4-5` 41 (**latest 13/08**) · `gpt-4.1` **15** (latest **16/07** — hơn 1 tháng trước) · `satellite-sonnet-4-6` 8 (latest 13/08).

**Đọc chuỗi thời gian:** acc2-native (Anthropic) chạy được tới đầu-07 → sau đó bị chặn (đúng AA-291, "channel program"), hệ thống fallback đúng thiết kế: 08/07→16/07 rơi xuống GPT-4.1 (satellite CHƯA tồn tại) → 16/07 satellite acc1 wire xong → GPT-4.1 **không còn xuất hiện lần nào nữa** (0 row GPT-4.1 sau 16/07, kể cả trong 08/12-08/13 khi có 10 run gần nhất) → 12-13/08 satellite acc3 (AA-397) trở thành đường chính.

**Kết luận Mục 1:**
1. PRD §5.0.1 ("S1 KHÔNG viết bằng Bedrock... `invoke_claude()` CHƯA từng được gọi từ pipeline S1 rewrite thật") **đúng tại thời điểm viết ban đầu nhưng đã SAI ngay khi AA-296 merge (16/07)** — vì `LLMClient.generate()` là client DÙNG CHUNG, không phải đường riêng phải "wire thêm". Bản v1.1 (24/07) lặp lại claim "100% GPT-4.1" **dù DB lúc đó đã có 36 row chạy qua satellite acc1 (21/07-01/08)** — claim này đã sai NGAY KHI VIẾT, không phải "trở nên cũ sau đó".
2. **Hôm nay (15/08/2026): S1 CIS KHÔNG chạy GPT-4.1.** Lần cuối cùng dùng GPT-4.1 là 16/07 (hơn 1 tháng trước). 10 run gần nhất (7-13/08) đều qua Bedrock (satellite acc3, đôi khi rơi xuống satellite-haiku khi sonnet acc3 lỗi — nhưng KHÔNG rơi tới GPT-4.1).
3. **Q7 (PRD: "B — không đụng S1 cũ, đo cost GPT-4.1 trước") — AA-333 (đo cost) vẫn Backlog, KHÔNG có số liệu cost nào tồn tại ở đâu (không CloudWatch, không comment Linear).** Nhưng câu hỏi mà AA-333 định trả lời gần như đã tự giải quyết: chi phí GPT-4.1 thực tế cho S1 hiện ~$0/tháng (0 lần gọi từ 16/07). AA-333 vẫn nên đóng/ghi chú lại, không phải "đo tiếp" — bản chất câu hỏi đã đổi.
4. **Gap còn thật:** không có cơ chế nào CHỦ ĐỘNG tắt hẳn nhánh GPT-4.1 (T3) — nó vẫn là code path sống, chỉ đơn giản chưa từng bị kích hoạt lại vì Bedrock (native/satellite) chưa từng fail hoàn toàn kể từ 16/07. Đây không phải bug, chỉ là điểm cần ghi chú: "100% GPT-4.1" đã sai nhưng dây last-resort vẫn tồn tại, đúng theo thiết kế fallback ban đầu.

---

## Mục 2 — AA-328 (N8, Backlog) có bị lệch status so với 7 issue con?

### Câu trả lời: KHÔNG — status Backlog của AA-328 CHÍNH XÁC, không phải pattern "parent quên update" như AA-345.

**`get_issue(AA-328, includeRelations=true)`:** description gốc (viết 24/07, chưa sửa) tự liệt kê đủ 6 mảnh phạm vi: Weekly Packet · L4 flywheel · fix bug B8 · **Unknown ledger** · **Bảng điểm dữ liệu supplier** · **Mệt mỏi phía agency** · Alarm bắt buộc. Verify checklist trong description: 3 mục (usage log đúng 1 lần / `acp_output_rules` count tăng / packet copy-paste được grounding=1.0) — không mục nào có checkbox tick.

**`list_issues(parentId=AA-328)`** — 8 con thật (không phải 7 như brief giả định ban đầu; AA-403 CŨNG là con qua `parentId`, không chỉ "relatedTo"):

| Con | Status | Ánh xạ vào phần nào của description AA-328 |
|---|---|---|
| AA-361 (N8-1) | Done | usage_log write-back |
| AA-362 (N8-2) | Done | Alarm bắt buộc |
| AA-363 (N8-3) | Done | L4 flywheel wiring |
| AA-364 (N8-4) | Done | Weekly Packet assembly + fix B8 |
| AA-365 (N8-5) | Done | Gate C / trust ramp |
| AA-367 (N8-4b) | Done | Weekly Packet — gán Piece thật |
| **AA-366 (N8-6)** | **Backlog** | **Unknown ledger + Bảng điểm supplier + Mệt mỏi agency — CHƯA làm** |
| **AA-403** | **Backlog** | Không nằm trong description gốc AA-328 (H-3 Mistake-to-Rule, phát sinh sau) — nhưng parentId trỏ về AA-328 |

**Kết luận Mục 2:** description AA-328 liệt kê tường minh 3 phần (unknown ledger, supplier scorecard, agency fatigue) mà CHỈ AA-366 cover — và AA-366 vẫn Backlog. → AA-328 ở Backlog là **đúng phạm vi thật**, không phải bug quy trình kiểu AA-345 (nơi PR merge tự động Done một parent chưa xong thật). Khác biệt cốt lõi: AA-345 tự bị Linear-GitHub integration đánh Done qua PR merge dù code rỗng; AA-328 thì NGƯỢC LẠI — vẫn giữ đúng Backlog dù 6/8 con đã Done, vì mô tả gốc của nó rộng hơn tổng các con Done. Không cần và không nên tự đổi status.

---

## Mục 3 — Flywheel L4 (`acp_output_rules`) thật sự chạy tới đâu?

### Câu trả lời: chiều ÁP DỤNG (apply) đã wire đúng vào N7 thật, nhưng bảng rule đang HOÀN TOÀN RỖNG (0 row) — flywheel hiện là no-op trên thực tế, không phải "chưa wire" như AA-403 mô tả mà là "wire vào một cái rổ không có gì để lọc".

**Code — xác nhận caller thật (không chỉ tin issue Done):**
- `services/acp_produce/pipeline.py:167` — `run_piece_through_produce_gates()` (hàm orchestrator thật của N7, per-piece) gọi `rule_result = await apply_output_rules_to_piece(piece, stage, tenant_id, db)` **đầu tiên, trước mọi gate F1-F9** — đúng thiết kế L4.
- `services/acp_produce/rule_adapter.py:108` → gọi thẳng `api/services/acp_post_processor.py::apply_output_rules()` (hàm GỐC, không viết lại) — dòng 66-67: `SELECT ... FROM acp_shared.acp_output_rules`.
→ Xác nhận AA-363 (Done) là thật: đường APPLY có tồn tại và chạy mỗi piece.

**DB — `acp_shared.acp_output_rules` (query trực tiếp, 15/08):**
```
total: 0
by_source_type_active: []
by_stage: []
hitl_rejection_rules: []
```
Bảng **tồn tại đúng schema** (`rule_id, tenant_id, stage, rule_type, pattern, action_value, error_message, source_type, source_hitl_id, run_count, is_active, created_at, confidence_score`), RLS bật nhưng KHÔNG force (`relforcerowsecurity=false` — không che dữ liệu khỏi truy vấn admin) — nhưng **0 row thật**.

**Nguyên nhân xác nhận:** `shared.schema_versions` chỉ có version **037** (áp 21/05/2026, "add confidence_score"), **KHÔNG có version 020** (`020_acp_output_rules_seed.sql` — migration seed ~15+ rule hệ thống gốc). → Migration seed 020 **chưa từng được apply lên DB dev thật**, dù file tồn tại trong repo.

**Kết luận Mục 3:**
1. Chiều APPLY (N7 gọi rule mỗi piece) — **wire đúng, xác nhận qua code, không phải chỉ tin Linear.**
2. Bảng rule — **0 row, cả `source_type='system'` (seed) lẫn `source_type != 'system'` (HITL-derived)** — nghĩa là AA-403's premise ("chiều rút rule mới từ HITL rejection chưa từng chạy") ĐÚNG, nhưng thực ra còn tệ hơn: kể cả rule SEED GỐC (không cần HITL, chỉ cần chạy migration) cũng chưa có. Root cause không phải thiếu code logic rút rule (đó là AA-403's phạm vi thật) mà là **migration 020 y hệt bị bỏ sót khi apply**, một lớp vấn đề khác, đơn giản hơn nhiều để fix (chạy 1 migration) so với AA-403 (build H-3 extraction pipeline).
3. Hệ quả vận hành: `apply_output_rules_to_piece()` chạy mỗi piece nhưng luôn trả `passed=True` (không có rule nào để vi phạm) — flywheel L4 hiện tại là **lớp code sống nhưng không có tác dụng thật trên production**, cho tới khi (a) migration 020 được chạy VÀ/HOẶC (b) AA-403 build xong đường rút rule từ HITL.

---

## Mục 4 — 5 điều kiện chặn production đang ở đâu thật?

### AA-328 (N8) — xem Mục 2, không lặp lại. Backlog đúng, chờ AA-366 + AA-403.

### AA-320 (Gate B persist) + AA-309 (N1) — verify qua DB thật

**`acp_shared.quarter_plan` / `quarter_plan_version`** (query 15/08) — bảng CÓ DỮ LIỆU THẬT, khác với ghi chú AA-388/S140 ("No quarter plans are pending approval — test data đã dọn sạch"):

| plan_id | tenant | quarter | versions | approval gần nhất |
|---|---|---|---|---|
| `50bb74c2...` | **`6fbaf284...` = tên "TEST-N1-flow"** (xác nhận qua `shared.tenants`) | Q3/2026 | 1 | `approval_status=approved`, `approved_by=admin`, 13/08 13:57 |
| `6d1baf3c...` | **`00000000...0001` = "Adventure Asia Internal"** (`aa_internal`, `is_active=true`) | Q3/2026 | 6 (v1→v6) | v1/v2 `approved_by=nghiep` (07/08) · v3 `approved_by=TEST_MODE_SCRIPT` (09/08 — khớp AA-391 E2E test) · v4/v5/v6 `approved_by=admin` (13/08, 3 lần trong ~4 giờ) |

**Đọc kỹ:** cả 2 plan đều KHÔNG phải tenant B2B thật trả tiền — 1 là tenant test tên rõ ràng ("TEST-N1-flow"), 1 là **Adventure Asia Internal** (tenant #0 nội bộ theo đúng thiết kế PRD §4.2 "AA Internal = tenant #0, dùng chung code với B2B" — đây LÀ sản xuất thật của chính AA, không phải test, nhưng cũng không phải "tenant B2B đầu tiên" mà PRD Mục D coi là điều kiện chặn production). 6 version liên tiếp trên cùng 1 plan trong ~1 tuần (07/08→13/08) cho thấy đây là chu kỳ lặp phát triển/verify (mỗi lần re-run N4/N5 tạo version mới), không phải 1 lần duyệt ổn định.

**Kết luận:** Gate B **KHÔNG còn "hoàn toàn trống"** như AA-388/S140 ghi — cơ chế đã chạy thật nhiều lần cho AA Internal + 1 lần cho tenant test. Nhưng **chưa có bằng chứng 1 tenant B2B thật (trả tiền, ngoài AA Internal) nào đã qua Gate B** — điều kiện "trước tenant B2B thứ 2" (Q6) và "onboard tenant thật" (AA-309, Done về code nhưng chưa verify có tenant thật nào onboard thành công tới Gate A chưa — ngoài phạm vi Mục 4, cần Giai đoạn 2 phần sau nếu muốn verify riêng AA-309).

### AA-332 (Tầng 4 dedup) — verify code

**Grep toàn bộ `services/acp_produce/*.py` (module N7 thật):** không có hàm/gate nào chứa `cosine`, `pgvector`, hay `0.92`. `gates.py` chỉ có **F7 FAQ dedup** (`_FAQ_DEDUP_THRESHOLD = 0.85`, dòng 265-296) — đây là dedup **INTRA-piece** (trong cùng 1 bài, chống FAQ lặp câu hỏi), **hoàn toàn khác** "Tầng 4" PRD mô tả (cross-tenant, toàn bộ content embedding, threshold 0.92, chặn publish).

**Hạ tầng tiền đề đã có sẵn** (đúng PRD ghi "content_embedding đã có sẵn"): `api/migrations/041_pgvector_embeddings.sql` — cột `content_embedding vector(1536)` + index `ivfflat ... vector_cosine_ops` đã tồn tại từ trước (AA-62, migration cũ của CIS).

**Kết luận:** AA-332 là **"chưa bắt đầu" thật (0%)**, không phải "có code nhưng chưa wire" — không tìm thấy 1 dòng code nào implement gate F-new cross-tenant dedup, dù hạ tầng vector/index cần thiết đã sẵn sàng. Effort còn lại = viết gate function + wire vào `run_gates()`/`pipeline.py`, KHÔNG cần build lại tầng embedding.

### AA-326 (superlative claims ~9%) — verify code

**`services/acp_shared/grounding.py:1-27`** (docstring, module DÙNG CHUNG cho cả S1-from-atom và N7): tự ghi rõ **"Known limitation, by design, not an oversight: this does NOT catch qualitative/superlative claims that carry no number... Tracked separately as AA-326."**

**Kết luận:** xác nhận đúng 100% những gì Linear ghi — checker hiện tại (`find_novel_numeric_claims`, ADR-2026-033) CHỈ bắt số liệu, cố ý (by design) không xử lý superlative. AA-326 = 0% code, đúng trạng thái Backlog, không có phần nào bị bỏ sót/ẩn.

---

## Tổng kết Mục 1-4 — điểm khác biệt lớn nhất so với Giai đoạn 1 / PRD

1. **S1 CIS: PRD sai, không chỉ "cũ".** Claim "100% GPT-4.1" đã sai kể từ ngày viết ra ở bản v1.1 (24/07) — DB lúc đó đã có 36 row chạy Bedrock satellite. Hôm nay 0% GPT-4.1 (lần cuối 16/07). AA-333 (đo cost GPT-4.1) gần như hết ý nghĩa vì cost thực tế đã bằng ~$0 từ hơn 1 tháng nay.
2. **AA-328 Backlog là ĐÚNG**, không phải bug quy trình — khác pattern AA-345. Chờ AA-366 (unknown ledger/supplier/agency fatigue) + AA-403 (H-3 wiring).
3. **Flywheel L4: apply-side đã wire thật (code xác nhận), nhưng bảng rule 0 row** vì migration 020 (seed) chưa từng được apply — đây là 1 fix ĐƠN GIẢN (chạy migration) bị lẫn trong nhận định "flywheel chưa hoàn chỉnh" của AA-403 (vốn nói về phần PHỨC TẠP hơn — build cơ chế rút rule từ HITL). Hai vấn đề khác mức độ, không nên gộp effort ước tính.
4. **Gate B không còn trống** — đã chạy thật 7 lần (6 cho AA Internal, 1 cho tenant test) — cập nhật so với ghi chú S140. Nhưng vẫn CHƯA có tenant B2B trả tiền nào qua Gate B.
5. **AA-332 (dedup) = 0% thật**, không phải "có code chưa wire" — hạ tầng vector sẵn sàng, gate logic chưa viết dòng nào.
6. **AA-326 (superlative) = 0% thật**, đúng như Linear ghi, xác nhận qua chính docstring code.

*Dừng lại tại đây theo đúng phạm vi Giai đoạn 2/4 — chờ Nghiep review trước khi sang Giai đoạn 3 (tìm chồng lấn/lỗi thời).*

# AA-469 Việc 4 — STEP0 Investigate: xác nhận lại chuỗi T8→T9→T10→T11

Investigate only, không sửa code, không commit gì trong phiên này. Làm trên `main`, không dùng
worktree riêng (đọc-only). Đọc trực tiếp `services/acp_angle_gate/{service,generate,prompts,
channel_style}.py`, `api/routers/v1_content_writing.py`, `services/acp_content_writing/service.py`,
`services/acp_content_writing/quality_gates.py`, `api/routers/v1_publish.py`,
`frontend/app/(tenant)/portal/_components/{AngleGateTab,PublishPendingList}.tsx` toàn bộ phiên
này (không tin lại mô tả AA-449/450/452/455-460 dù đã Done).

**Terminology (xác nhận đúng trước khi đọc code, theo brief 29/08 của Nghiệp)**: "Goal" = tầng 1,
1 trong 8 giá trị cố định (Bang 1). "Angle" = tầng 2, sinh SAU khi chọn Goal — input
[atom, goal, brand, DFS/PAA] → LLM sinh 3 angle. DFS/PAA (nếu có) feed vào bước sinh ANGLE, không
phải bước chọn GOAL.

**Status: STEP0 xong — chưa đề xuất thiết kế, chưa viết code.** Đây là 1 trong 5 Việc AA-469 yêu
cầu điều tra riêng trước khi bất kỳ Việc nào được thiết kế/build — xem
`docs/claude_tasks/AA-469-redesign-t-series.md`.

---

## 1-2. T8 — `services/acp_angle_gate/service.py::set_goal_and_generate()` (dòng 204)

Path/tên hàm từ tham chiếu gốc của AA-469 vẫn đúng, chưa đổi kể từ 23/08.

Chuỗi input thật cho LLM sinh angle (`set_goal_and_generate()` gọi `generate_angles()`,
`generate.py:82`):
```python
angles, recommended_index, reason, cost_usd = await generate_angles(
    content_seed=atom["text"], goal=goal, channel=req["channel"],
    brand_audience=brand_audience, destination=destination, trip_name=trip_name,
)
```
6 input: `content_seed` (text atom thô), `goal`, `channel`, `brand_audience`, `destination`,
`trip_name`. **Không có `seo_context`/DFS/PAA/`related_keywords` ở bất kỳ đâu trong file này.**

Truy vết tiếp xuống 2 tầng dưới để đóng câu hỏi (c) dứt điểm:
- `generate.py::generate_angles()` (dòng 82) — cùng 6 input y hệt, chỉ build `user_prompt` qua
  `build_user_prompt()` rồi gọi `LLMClient().generate()`. Không có fetch DFS/seo_context nào.
- `prompts.py::build_user_prompt()` (dòng 53) — prompt text thật gửi LLM chỉ có 5 khối:
  CONTENT SEED / TRIP / GOAL / CHANNEL / BRAND AUDIENCE. Không có khối nào cho search-demand
  signal.

**→ Câu (c) đóng dứt điểm ở cả 3 tầng: `service.py` → `generate.py` → `prompts.py`. `/goal` KHÔNG
gọi DFS, KHÔNG đọc `seo_context`, ở bất kỳ layer nào.** Mô tả 29/08 của Nghiệp ("DFS/PAA feeds
angle step") là THIẾT KẾ MONG MUỐN, không phải cái đã build — gap thật, chưa build ở bất kỳ layer
nào, khớp với việc AA-466's STEP0 chưa xác nhận được điều này.

## 3-4. T9 — `api/routers/v1_content_writing.py` + `services/acp_content_writing/service.py`

Router: `POST /v1/content-writing/requests/{request_id}/write` (202 Accepted, AA-466 — thực ra
insert placeholder rồi launch background task) + `GET /v1/content-writing/pieces/{piece_id}` (poll).

Input thật: `request_id` (angle_gate_request_id) — KHÔNG phải `angle_id`+`channel` rời. Toàn bộ
context (angle đã chọn, channel, goal) được `start_write()` tự fetch lại từ
`angle_gate_service.fetch_request()` — 1 request T8 = 1 angle đã chọn = 1 channel cố định từ lúc
`create_request()`.

**Channel structure lookup**: `get_channel_style(req["channel"])`, import từ
`services.acp_angle_gate.channel_style` — **T9 dùng LẠI đúng module channel_style.py của T8**,
không có bản sao riêng.

## 5. Channel enum — Blog CÓ tồn tại, là entry thứ 8, tự nhận là fallback tự viết (không từ Bang 2)

`services/acp_angle_gate/channel_style.py` — 8 channel: `linkedin, facebook, instagram, tiktok,
email, landing_page, ads` (7 channel thật từ `Channel Output Structures.xlsx`) + `blog` (dòng
120-127). Comment module (dòng 12): *"'blog' has NO row in Bang 2 at all (STEP0 §5 flagged this
gap explicitly) — kept as a Channel value for backward compatibility (T7 already had it), but its
entry below is a NEW, self-authored fallback (not from Bang 2 or any other source document)."*

Entry blog thật:
```python
{
    "channel": "blog", "display_name": "Blog",
    "use_when": "Long-form SEO content, hub/pillar pages, in-depth destination guides",
    "structure": "Hook→context/why this destination→structured H2 sections→FAQ (if TOFU)→CTA",
    "style": "informative, structured, SEO-aware, still specific (not generic travel copy)",
    "avoid": "keyword stuffing, generic listicle tone, no cliché superlatives without proof",
}
```
`structure` field tự mô tả 1 format DÀI HƠN nhiều-section (H2 sections, FAQ) — nhưng đây chỉ là
prose mô tả cho prompt LLM, KHÔNG kéo theo bất kỳ cơ chế multi-atom-input nào — xem mục (d) bên
dưới.

## 6-7. T10 — `services/acp_content_writing/quality_gates.py::run_quality_gates()` (dòng 427)

**Gate stack thật**: 6 gate cho MỌI channel (`F6_cta_present`/`F1_grounding`/`F2_banned_patterns`/
`F4_extreme_length`/`F8_framework`/`F9_brand_voice`) + 3 gate CHỈ cho `channel=='blog'`
(`F5_atom_density`/`F3_structural_variance`/`F7_faq_dedup`, chèn giữa F4 và F8, dòng 462-467) = 9
gate cho blog, 6 cho 7 channel còn lại. Khớp CLAUDE.md's record về AA-452.

**Retry logic thật** (`services/acp_content_writing/service.py::run_write_background()`,
`MAX_ATTEMPTS = 2`):
- Attempt 1: `write_content()`. Attempt 2 (nếu fail và `repairable`): `rewrite_with_feedback(...,
  revision_feedback=repair_log[-1]["violations"])` — **CÓ THẬT, structured feedback (list
  violations của gate fail đầu tiên) được feed lại cho lần rewrite, không chỉ boolean pass/fail.**
- Nếu cả 2 attempt fail (hoặc fail không-repairable): `status = "held"`, `held_reason =
  f"{first_failure['gate']}: {'; '.join(first_failure['violations'][:3])}"`.

**KHÔNG tìm thấy cơ chế "error tags" nào, và KHÔNG có bước "force sang T11" nào trong file này.**
"Tags" duy nhất trong `quality_gates.py` là `[R:id]`/`[F:id]` citation tag — markup nội bộ CHỈ
cho blog (grounding check), bị `strip_citation_tags()` xoá sạch trước khi piece được lưu/trả về —
hoàn toàn khác khái niệm "error tags" trong context (a).

## 8. T11 — `api/routers/v1_publish.py` — xác nhận `held` KHÔNG THỂ tới được T11 (cấu trúc, không
   phải chỉ theo quy ước)

`list_pending()` (dòng ~103):
```sql
WHERE cp.tenant_id = $1::uuid AND cp.status = 'approved' AND agr.channel = $2 ...
```
`publish()` (dòng ~161):
```python
if not piece or piece["status"] != "approved" or piece["channel"] != _BLOG_CHANNEL:
    raise HTTPException(status_code=404, detail="Content piece not found or not approved")
```
**Cả 2 endpoint đều lọc cứng `status = 'approved'`.** Một piece `held` (hết 2 lần retry) hoàn toàn
KHÔNG THỂ xuất hiện trong danh sách pending, và gọi publish trực tiếp lên piece_id đó sẽ nhận 404.
Đây là 1 GIỚI HẠN CẤU TRÚC (structural), không phải quy ước UI có thể lách qua — **kết luận: mô tả
"fail max 2 retries, still forced to output to T11 with error tags" trong context (a) KHÔNG khớp
với code hiện tại ở bất kỳ điểm nào đã đọc.** T11 cũng CHỈ hoạt động cho `channel='blog'`
(`_BLOG_CHANNEL = "blog"`, comment: "7 other channels deferred, no code") — theo đúng
CLAUDE.md's record đã có.

## 9-10. Frontend

**T8+T9+T10 = MỘT wizard duy nhất, MỘT trang** (`AngleGateTab.tsx`, 535 dòng, route
`/portal/t8-angle-gate`). Comment đầu file (dòng 3-8) tự xác nhận đây là quyết định giữa-chừng của
Nghiệp ("UI liền mạch"): không có route `/portal/t9-write` riêng.

**Xác nhận (b): đây LÀ linear wizard, KHÔNG có back-navigation từng bước.**
- State machine 1 chiều theo `req.status`: `null` (chưa tạo) → `pending_goal` → `pending_choice`
  → `approved`. Mỗi card chỉ render khi đúng trạng thái, không có nút "quay lại bước trước" nào.
- Cách "quay lại" DUY NHẤT là `reset()` (dòng 285-290) — nút "Start over" — nhưng hàm này XOÁ
  SẠCH mọi state (`setReq(null)`, xoá atom/channel/goal đã chọn, xoá piece) — nghĩa là quay lại
  ĐẦU TỪ ĐẦU, không phải "lùi 1 bước, giữ lựa chọn trước đó".
- `choose(idx)` (dòng 267-283) tự động fire `writeContent()` ngay khi `status` chuyển
  `'approved'` (dòng 279: `if (d.status === "approved") writeContent(d.request_id);`) — tenant
  KHÔNG có cơ hội xem lại/đổi ý về angle đã chọn trước khi hệ thống bắt đầu viết — tăng cường thêm
  tính chất "chỉ tiến, không lùi".

**T11 còn tách biệt hơn nữa: KHÔNG cùng flow với T8-T10 luôn.** `PublishPendingList.tsx` (139
dòng) là 1 component RIÊNG, độc lập hoàn toàn với `AngleGateTab.tsx` — không import lẫn nhau,
không có prop/state chung. Theo route đã biết từ CLAUDE.md (`/portal/t11-publish` vs
`/portal/t8-angle-gate`), tenant phải TỰ chuyển tab/route để publish 1 piece vừa qua T10 — không
có link/nút nào trong `AngleGateTab.tsx` đưa thẳng sang publish sau khi piece `approved`.

---

## Trả lời trực tiếp (a)/(b)/(c)/(d)

**(a) Full flow [atom, goal, brand] → 3 angle → chọn 1 → chọn channel → viết → T10 gate →
pass/fail (fail tối đa 2 lần retry, vẫn bị ép output ra T11 kèm error tag)?**

- Phần ĐÚNG: atom→goal→3 angle→chọn 1→viết→T10 gate→pass/fail, tối đa 2 lần retry, retry thứ 2
  CÓ nhận feedback có cấu trúc (`violations` list) — tất cả đã xác nhận có thật trong code.
- Phần SAI: channel KHÔNG được chọn sau khi chọn angle — channel được chọn NGAY TỪ ĐẦU (bước 1,
  cùng lúc chọn atom, trước cả khi chọn goal) — thứ tự thật là [atom + channel] → goal → 3 angle
  → chọn 1 → viết → T10 gate. Và "vẫn bị ép output ra T11 kèm error tag" **KHÔNG tồn tại** — piece
  fail hết retry dừng ở `status='held'`, hiển thị cho tenant xem (không ẩn), nhưng T11's endpoint
  cấu trúc CHẶN CỨNG mọi piece không phải `status='approved'` — không có cách nào (kể cả cố tình)
  đưa 1 piece `held` sang T11 bằng code hiện tại.

**(b) Cả cụm T8-T11 cần "step-by-step flow with back-navigation" — chưa phải linear wizard không
lùi được?**

Xác nhận ĐÚNG hoàn toàn premise — hiện tại LÀ linear wizard, không có back-navigation từng bước,
chỉ có "Start over" (xoá sạch). Thêm 1 phát hiện quan trọng hơn cả premise ban đầu: T11 hiện
KHÔNG NẰM TRONG CÙNG flow với T8-T10 — là 1 trang/component hoàn toàn tách biệt, tenant phải tự
điều hướng. Nếu Việc 4 định thiết kế 1 flow T8-T11 liền mạch có back-navigation, cần tính cả việc
gộp T11 vào flow đó, không chỉ thêm nút "Back" cho T8-T10.

**(c) `/goal` có gọi DFS/đọc `seo_context` làm input sinh angle không?**

KHÔNG — xác nhận dứt điểm ở cả 3 tầng code (`service.py`/`generate.py`/`prompts.py`). Gap thật,
chưa build.

**(d) Blog (kênh thứ 8) cần multi-atom input, khác 7 kênh social dùng 1 atom — đã xử lý hay cần
thiết kế riêng?**

CHƯA xử lý — cần thiết kế riêng. Blog tồn tại như 1 channel hợp lệ xuyên suốt T8→T9→T10 (kể cả có
3 gate riêng ở T10), nhưng cơ chế input xuyên suốt vẫn là 1 `atom_id` string duy nhất (`T8::
create_request(tenant_id, atom_id: str, channel, pool)`, `T9::_fetch_atom_text(tenant_id,
atom_id, pool)`) — không có list/array atom_id ở bất kỳ đâu trong `angle_gate_request` hay
`content_piece`'s schema/code đã đọc. `channel_style.py`'s blog `structure` field mô tả format
nhiều-section, nhưng đó chỉ là prose hướng dẫn LLM viết dài hơn từ CÙNG 1 atom, không phải cơ chế
nhận nhiều atom.

## Terminology inconsistency check (theo yêu cầu đầu bài)

**Goal/Angle: KHÔNG tìm thấy conflation nào trong code đã đọc.** Toàn bộ `service.py`,
`generate.py`, `prompts.py`, `channel_style.py`, và `AngleGateTab.tsx`'s comment đầu file (dòng
16-19: *"this component always says 'Goal' for the 8-value list ... and 'Angle' only for the 3
LLM-generated options ... never mixes the two"*) đều nhất quán, tách bạch rõ ràng 2 khái niệm —
kỷ luật này rõ ràng là hệ quả trực tiếp của bài học AA-449 mà đầu bài nhắc tới.

**Có 1 inconsistency KHÁC, không phải goal/angle, nhưng đáng flag**: channel enum bị chia làm 2
định nghĩa KHÔNG khớp nhau giữa 2 subsystem —
- T8/T9's `channel_style.py`: 8 giá trị (`linkedin, facebook, instagram, tiktok, email,
  landing_page, ads, blog`).
- `services/acp_produce/models.py:78` (N7, hệ thống production pipeline CŨ hơn):
  `channel: Literal["blog", "facebook", "tiktok"] = "blog"` — chỉ 3 giá trị.

Đây là 2 subsystem khác nhau (N7 cũ vs. T8/T9 mới) nên không tự nó là bug, nhưng là nguồn nhầm lẫn
thật nếu ai đó đọc nhầm module khi tra "channel nào hợp lệ" — đáng ghi lại theo đúng yêu cầu "flag
it explicitly... don't just silently use whichever term the code uses" dù đây là channel-enum chứ
không phải goal/angle.

## Giới hạn phiên này

- Chưa đọc `services/acp_content_writing/generate.py`, `framework_rubrics.py`,
  `services/acp_angle_gate/goals.py`, `brand_audience.py` toàn văn — đủ context từ các file khác
  để trả lời (a)-(d), nhưng nếu build task sau này cần chi tiết `write_content()`/
  `rewrite_with_feedback()`'s prompt thật, nên đọc trực tiếp `generate.py` (acp_content_writing)
  trước khi thiết kế thêm.
- Chưa đọc migration 113 (`angle_gate_request`/`angle_gate_option` schema) trực tiếp để xác nhận
  100% `atom_id` là cột đơn (không phải array) — kết luận (d) dựa trên đọc code Python nhất quán
  ở mọi call site (rất đáng tin), không phải đọc DDL trực tiếp.
- Không query DB thật trong phiên này — task này thuần đọc code, không yêu cầu số liệu.

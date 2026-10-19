# AA-508 STEP0 — Atom identity (content-hash), cache, partial commit

Read-only investigation. No code changed. Epic: AA-507 (T5-T11 redesign theo
repo aa-social-media của Ms. Thư). Mục tiêu: xác nhận nền tảng atom_id ổn định
cho AA-509 (Segment) bằng bằng chứng code thật, không giả định.

## Q1 — atom_id sinh theo cách nào?

**Random, không content-derived.** `services/acp_produce/tenant_pipeline.py:320`:

```python
atom_id = f"atom_{uuid.uuid4().hex[:10]}"
```

(và marker rỗng cùng file dòng 335: `atom_marker_{uuid.uuid4().hex[:10]}`).

`services/acp_shared/atom_extraction.py` (module AA-475 tách ra) chỉ chứa
`SYSTEM_PROMPT` / `build_user_prompt()` / `source_hash()` / `strip_json_fence()`
— **không có hàm sinh atom_id nào ở đây**. Grep toàn repo (`atom_id` xuất hiện
~150 lần) xác nhận đúng 1 nơi duy nhất gán giá trị mới cho atom_id khi INSERT:
dòng 320 và 335 nói trên. Mọi nơi khác chỉ đọc lại `atom_id` đã tồn tại (SELECT,
citation tag `[R:atom_id]`, FK join).

Không có `hash(trip_code, day, place, action)` nào trong codebase — đối chiếu
thiết kế Ms. Thư (`atom_id()` trong `src/aa_social/models.py`) thì đây đúng là
gap: **atom_id hiện tại đổi ngẫu nhiên mỗi lần re-atomize, kể cả khi nội dung
ngày đó không đổi.**

Hệ quả cụ thể (quan trọng cho AA-509): vì atomize hiện là **1 lệnh gọi LLM cho
toàn bộ itinerary** (không lặp theo từng ngày — xem Q3), mỗi lần source_hash
đổi (dù chỉ 1 ngày trong itinerary thay đổi), **toàn bộ atom set cũ bị bỏ lại
im lặng, atom set mới được INSERT với atom_id hoàn toàn mới** — kể cả các ngày
không hề thay đổi nội dung. Bất kỳ thứ gì tham chiếu atom_id cũ (angle_gate_
request, content_piece qua angle_gate_request.atom_id, usage_log, weight/
distinctiveness lịch sử) sẽ trỏ vào atom đã "chết" theo nghĩa không còn là bản
mới nhất, dù về mặt logic đó vẫn là "cùng một khoảnh khắc trong ngày đó".

## Q2 — tour_atoms.source_hash: có ghi không? Có ai đọc lại không?

**Có ghi VÀ có đọc lại** — không phải dead column, nhưng granularity là
**per-tour (toàn bộ itinerary), không phải per-day**.

- Ghi: `tenant_pipeline.py:326` (atom thật) và `:339` (empty marker) — cùng
  một `source_hash` cho MỌI atom sinh ra trong 1 lần gọi (vì chỉ có 1 giá trị
  hash tính 1 lần ở dòng 278, trước vòng lặp).
- Đọc lại để skip: `tenant_pipeline.py:280-290` —

```python
latest_hash = await conn.fetchval(
    """SELECT source_hash FROM acp_contract.tour_atoms
       WHERE tour_id = $1::uuid AND owner_scope = $2
       ORDER BY created_at DESC LIMIT 1""",
    tour_id, tenant_id,
)
if latest_hash is not None and latest_hash == source_hash:
    return {"status": "skipped", "atom_count": 0}
```

Idempotency key là `(tour_id, owner_scope)` → so với 1 hash duy nhất tính từ
`build_user_prompt(row)` — chuỗi này nối `name + aa_summary + aa_highlights +
itinerary_source (TOÀN BỘ itinerary, mọi ngày) + inclusions + exclusions`
(`services/acp_shared/atom_extraction.py:54-71`). Tức là **thay đổi ở BẤT KỲ
ngày nào trong itinerary đều làm hash lệch → re-atomize LẠI TOÀN BỘ tour**,
không chỉ ngày đã đổi. Migration 084 (chú thích gốc, AA-299) tự ghi rõ đây là
thiết kế "so hash duy nhất", không phải per-day.

Không có bảng fingerprint theo ngày nào tồn tại — grep `atomize_day_fingerprint`
không ra kết quả (đúng như dự đoán, vì bảng này chưa được tạo).

**Phát hiện phụ, chưa ai xử lý** (migration 084's own comment tự flag, chưa có
follow-up nào build): khi source_hash đổi, code **không xoá/soft-delete atom
rows cũ** — chỉ INSERT thêm rows mới bên cạnh rows cũ (dòng cuối migration
084: "This PR does NOT delete or supersede old atom rows... open question left
for a follow-up, not decided here" — vẫn đúng nguyên trạng tới giờ, grep xác
nhận không có `DELETE FROM acp_contract.tour_atoms` ở đâu trong repo). Rows cũ
nằm lại trong bảng vĩnh viễn trừ khi soft-delete thủ công qua
`PATCH /admin/atoms/{atom_id}`.

## Q3 — run_t5_atomize(): commit toàn bộ hay từng phần?

**Không có transaction bao ngoài** — mỗi atom là 1 câu `INSERT` riêng
(`tenant_pipeline.py:322-332`, trong vòng `for atom in atoms:`), không có
`async with conn.transaction():` nào bọc vòng lặp (grep `transaction(` trong
file cho 0 kết quả). asyncpg mặc định autocommit từng statement khi không mở
transaction tường minh — nghĩa là về mặt DB, **mỗi INSERT tự commit ngay lập
tức**, không phải all-or-nothing ở mức atom.

Nhưng đây không phải "commit per-day" như thiết kế tham chiếu — vì **không hề
có khái niệm "ngày" trong vòng lặp này**. `run_t5_atomize()` gọi LLM **một
lần duy nhất** cho toàn bộ `itinerary_source` (`invoke_claude()` ở dòng 295,
ngoài vòng lặp, trước khi có atom nào) — model tự trả về JSON chứa atom của
TẤT CẢ các ngày cùng lúc (`itinerary_day` là 1 field trong mỗi atom, do model
tự gán, xem `atom_extraction.py:45-48`). Vòng lặp INSERT (dòng 318-333) chỉ
lặp qua danh sách atom đã có sẵn từ 1 response LLM đó — không có lệnh gọi LLM
lặp lại theo ngày, không có ranh giới "ngày N xong, ngày N+1 bắt đầu".

Trả lời trực tiếp câu hỏi "nếu 1 ngày lỗi giữa chừng, ngày đã xử lý trước có
mất không":
- Nếu LLM call thất bại (`except Exception` dòng 298) → trả về sớm
  `{"status": "failed", ...}` TRƯỚC khi có bất kỳ atom nào được insert — không
  có gì để mất vì chưa insert gì cả (toàn bộ tour, không phải 1 ngày).
- Nếu LLM call thành công nhưng JSON parse lỗi (dòng 303-307) → tương tự,
  chưa insert gì.
- Nếu LLM call + parse đều thành công, vòng lặp INSERT đang chạy giữa chừng
  mà tiến trình bị crash (ECS task chết, DB connection rớt...) → vì không có
  transaction bọc ngoài, các atom đã INSERT trước điểm crash **VẪN được giữ
  lại** (đã autocommit), chỉ các atom sau điểm crash bị thiếu — tạo ra
  **atom set không đầy đủ nhưng cùng chung 1 source_hash** với set "đúng ra
  phải có". Vì logic skip chỉ so `latest_hash == source_hash` (không so số
  lượng atom kỳ vọng), lần gọi kế tiếp trên cùng version sẽ **skip nhầm**
  (hash khớp dù atom set thực tế bị thiếu) — đây là 1 gap thật, dù xác suất
  thấp (crash giữa 1 vòng lặp insert vài chục dòng, không phải giữa nhiều
  phút gọi LLM).

## Kết luận (dựa trên bằng chứng trên, chưa build gì)

1. **atom_id cần đổi sang content-hash — xác nhận có, không phải suy đoán.**
   Hiện tại 100% random UUID; không có cơ chế nào giữ atom_id ổn định qua
   re-run. Đây đúng là điều kiện tiên quyết bắt buộc AA-509 cần trước khi
   Segment có thể dựa vào atom_id để track qua nhiều lần rewrite/atomize.

2. **source_hash hiện tại KHÔNG đủ cho skip-unchanged ở granularity "theo
   ngày"** — nó chỉ đủ cho granularity "theo toàn bộ tour". Cần bảng mới
   dạng `acp_contract.atomize_day_fingerprint (tenant_tour_version_id,
   day_number, fingerprint_hash, atomized_at)` như prompt đề xuất, ĐỒNG THỜI
   `run_t5_atomize()` cần đổi kiến trúc để atomize xử lý/gọi LLM theo từng
   ngày thay vì 1 lệnh gọi cho toàn bộ itinerary — đây là thay đổi kiến trúc
   thật, không phải chỉ thêm 1 cột hay 1 bảng. (source_hash cấp-tour hiện có
   có thể giữ lại làm fallback/audit, nhưng không thay thế được fingerprint
   theo ngày.)

3. **run_t5_atomize() cần đổi sang commit-per-day** — nhưng điều kiện tiên
   quyết là mục (2) trước: phải có vòng lặp theo ngày (gọi LLM riêng từng
   ngày hoặc gọi 1 lần rồi group theo `itinerary_day` trước khi ghi) thì mới
   có "ranh giới ngày" để bọc transaction quanh nó. Ở kiến trúc hiện tại
   (1 LLM call cho cả tour), khái niệm "commit per-day" chưa áp dụng được —
   cần thiết kế lại thứ tự gọi LLM trước, rồi mới quyết được transaction
   boundary per-day.

## Should know — việc phụ chưa xử lý, không nằm trong scope AA-508 nhưng nên
biết trước khi thiết kế:

- Re-atomize không xoá atom cũ (migration 084 tự flag, vẫn còn treo). Nếu
  AA-508/509 đổi sang content-hash atom_id + per-day fingerprint, cần quyết
  định luôn: atom của 1 ngày KHÔNG đổi có giữ nguyên atom_id cũ (tái sử dụng
  row) hay vẫn tạo row mới nhưng cùng atom_id (UPSERT)? Đây là quyết định
  thiết kế cụ thể nên chốt cùng lúc với content-hash atom_id, không nên tách
  riêng — nếu không sẽ lặp lại đúng cái gap "rows cũ nằm lại vĩnh viễn" đang
  có.
- Chỉ có 1 real call site của `run_t5_atomize()`:
  `POST /v1/tours/{version_id}/atomize` (`api/routers/v1_tours.py:788`,
  standalone tenant-triggered, sau AA-469 Việc 1 tách khỏi chain T2→T5). Thay
  đổi signature/behavior của `run_t5_atomize()` chỉ cần sửa đúng 1 call site
  này.

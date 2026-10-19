# AA-508 STEP0 mini — Đối chiếu repo Ms. Thư thật: atom không đổi qua re-run xử lý thế nào?

Đọc trực tiếp `docs/AI-gent-for automation works/aa-soscial-media-main`. Read-only,
không sửa gì. Trả lời câu hỏi treo từ STEP0 chính: UPSERT (giữ atom_id cũ) hay
luôn tạo atom_id mới?

## 1. `atom_id()` — công thức hash chính xác

`src/aa_social/models.py:31-45`:

```python
def atom_id(trip_code: str, day: int, place: str, action: str) -> str:
    parts = (trip_code, str(day), _normalise(place), _normalise(action))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
```

Xác nhận đầy đủ: input là **4 phần** (`trip_code`, `day`, `place`, `action`) —
**KHÔNG hash `evidence`** (câu trích dẫn gốc). Có chuẩn hoá (`_normalise`):
lowercase + mọi ký tự không phải a-z0-9 thành khoảng trắng đơn + strip. `day`
ép về string trước khi nối. `trip_code` KHÔNG normalise (dùng verbatim).

Docstring dòng 34-38 tự nói rõ lý do thiết kế: "`place` và `action` là dedup
key trong 1 ngày — 1 ngày nói cùng 1 cặp 2 lần → 1 Atom, và **re-run cho cùng
id**." — đây là câu trả lời trực tiếp cho câu hỏi cuối cùng (mục 5).

## 2. Cơ chế ghi vào storage — INSERT OR REPLACE / UPSERT hay gì khác?

**UPSERT thật, keyed đúng trên `atom_id`.** `src/aa_social/stages/atoms.py`,
hàm `_store_day()` (dòng ~438-452):

```python
for identifier, atom in atoms.items():
    db.execute(
        "INSERT INTO atoms (atom_id, trip_code, day, place, action, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(atom_id) DO UPDATE SET"
        " place = excluded.place, action = excluded.action,"
        " evidence = excluded.evidence",
        (identifier, atom.trip_code, atom.day, atom.place, atom.action, atom.evidence),
    )
```

Kèm theo, ngay sau đó, `delete_missing(db, "atoms", {trip_code, day}, "atom_id",
list(atoms))` — xoá atom nào của (trip_code, day) đó **không còn** trong lần
extract mới nhất (ví dụ 1 pair place+action bị bỏ khỏi ngày đó). `transit_
readings` cũng UPSERT cùng kiểu (`ON CONFLICT(atom_id) DO UPDATE`).

Đây trực tiếp trả lời câu hỏi chính STEP0b: **KHÔNG phải "luôn INSERT mới rồi
dọn rác sau"** — là UPSERT thật ngay tại thời điểm ghi, atom_id trùng thì
UPDATE tại chỗ, không tạo row mới, không cần dọn rác riêng.

## 3. Cache hit (fingerprint không đổi) — atom_id ngày đó có bị động tới không?

**Không động tới gì cả — không đọc, không ghi, không xoá.** `run()`
(`atoms.py` dòng ~114-122):

```python
read_before = _fingerprints(db)
asking = [
    reading for reading in (_asking(row, client.signature) for row in days)
    if again or read_before.get(reading.of) != reading.fingerprint
]
```

Ngày nào fingerprint khớp (`read_before.get(...) == reading.fingerprint`)
**không lọt vào `asking`** → không gọi LLM (`_read_day`), không chạm
`_store_day()`/`_record_day()` cho ngày đó. Vì UPSERT ở mục 2 chỉ chạy cho
`asking`, atom_id + toàn bộ row của ngày cache-hit **giữ nguyên y hệt từ lần
chạy trước** — comment CLAUDE.md tự xác nhận: "an unchanged itinerary costs
nothing to run again". Đây chính là cơ chế giữ atom_id ổn định qua re-run —
KHÔNG PHẢI vì atom_id tự thân luôn ổn định, mà vì **ngày không đổi thì không
gọi LLM lại, nên không có gì để ghi đè**.

**Phát hiện quan trọng, ảnh hưởng trực tiếp thiết kế AA-508** — README.md
dòng 172-179 (đo thật, 2026-08-29, `--again` ép gọi lại toàn bộ dù fingerprint
không đổi):

> Measured cold on the real export, 2026-08-29: 206 days, 69 seconds, $1.4374,
> 206 Sonnet calls. The second run read nothing and took 3 seconds.
>
> The 4% drift is in how many Atoms come back, not in which. An `atom_id`
> hashes the exact wording of the place and the action, so the same 206 days
> read twice with the same prompt and the same model kept 383 of 931 ids and
> re-worded the rest. Nothing dangles — the scores and the slate cascade from
> the rows they hang off — but a slate built on one extraction does not
> survive the next.

Tức là: content-hash atom_id **CHỈ ổn định khi fingerprint-skip thực sự chặn
được lệnh gọi LLM lại**. Nếu bị ép gọi LLM lại (`--again`, hoặc bất kỳ lý do
gì khiến fingerprint không khớp dù ngày "gốc" không đổi) thì model tự diễn
đạt lại place/action hơi khác → hash khác → **chỉ 383/931 = 41% atom_id giữ
nguyên, 59% đổi** dù cùng input, cùng model, cùng prompt. Content-hash tự nó
KHÔNG miễn nhiễm với non-determinism của LLM — cơ chế skip theo fingerprint
(mục Q2/Q3 STEP0 chính, bảng `atomize_day_fingerprint`) mới là thứ thực sự
giữ atom_id ổn định trong thực tế, content-hash chỉ là điều kiện cần (idempotent
khi INPUT giống hệt và không gọi LLM lại), không phải điều kiện đủ khi buộc
phải re-call LLM.

## 4. Ngày bị xoá khỏi itinerary (rút ngắn 10→8 ngày)

Xử lý bằng **SQL FK cascade**, không phải code Python riêng. Schema
(`workspace.py`):

```sql
CREATE TABLE itinerary_days (
    trip_code TEXT NOT NULL REFERENCES trips(trip_code) ON DELETE CASCADE, ...
);
CREATE TABLE day_extractions (
    ...
    FOREIGN KEY (trip_code, day) REFERENCES itinerary_days(trip_code, day)
        ON DELETE CASCADE
);
CREATE TABLE atoms (
    ...
    FOREIGN KEY (trip_code, day) REFERENCES itinerary_days(trip_code, day)
        ON DELETE CASCADE
);
CREATE TABLE transit_readings (
    atom_id TEXT PRIMARY KEY REFERENCES atoms(atom_id) ON DELETE CASCADE, ...
);
-- offered_moments, atom_scores, route_members, segment_members, atom_matches
-- đều REFERENCES atoms(atom_id) ON DELETE CASCADE
```

`workspace.py:636` bật `PRAGMA foreign_keys = ON` mỗi connect. `ingest.py`
(stage đọc lại file nguồn) gọi `delete_missing(db, "itinerary_days",
{trip_code}, "day", <danh sách ngày còn trong file mới>)` — ngày 9/10 không
còn trong file → row `itinerary_days` cho ngày đó bị xoá → cascade tự động
xoá `day_extractions`, `atoms`, rồi cascade tiếp `transit_readings`/
`offered_moments`/`atom_scores`/`segment_members`/`route_members`/
`atom_matches` của mọi atom thuộc ngày đó. Không có code Python nào tự tay dọn
theo tầng — toàn bộ dựa vào FK cascade của SQLite.

## Kết luận trực tiếp (trích dẫn code, không suy luận)

**Repo Ms. Thư coi atom_id là identity ổn định xuyên suốt nhiều lần re-run —
XÁC NHẬN, có trích dẫn cụ thể** (`models.py:36`: "a re-run yields the same
ids"; cơ chế UPSERT `ON CONFLICT(atom_id) DO UPDATE` tại `atoms.py`) — **NHƯNG
với 1 điều kiện quan trọng chưa nêu trong STEP0 chính**: sự ổn định đó phụ
thuộc vào cơ chế skip-theo-fingerprint có thực sự chặn được lệnh gọi LLM hay
không, không phải vì bản thân content-hash miễn nhiễm với việc model diễn đạt
lại. Repo tự đo và tự thừa nhận 59% atom_id đổi khi bị ép gọi lại LLM dù input
giống hệt.

## Trả lời trực tiếp câu hỏi treo — UPSERT hay tạo mới?

**UPSERT — xác nhận đúng, không phải suy luận ngược từ schema AA-509.**
Bằng chứng trực tiếp: `atoms.py::_store_day()`'s `INSERT ... ON CONFLICT(atom_id)
DO UPDATE SET place=..., action=..., evidence=...`. Atom cũ (atom_id cũ) được
UPDATE tại chỗ nếu vẫn còn xuất hiện trong lần extract mới (cùng place+action
sau normalise) — không tạo row mới, không dọn rác riêng biệt (dọn rác chỉ xảy
ra cho atom KHÔNG còn xuất hiện nữa, qua `delete_missing()`, xoá ngay trong
cùng transaction, không phải "dọn sau").

## Should know — ảnh hưởng thiết kế cụ thể cho AA-508 khi build

1. `atomize_day_fingerprint` (bảng đề xuất ở STEP0 chính) phải là điều kiện
   **chặn gọi LLM**, không chỉ là log — nếu chỉ dùng để "quyết định có ghi đè
   hay không" sau khi đã gọi LLM thì không tái tạo được lợi ích ổn định
   atom_id mà repo Ms. Thư có (vì vẫn tốn 1 lệnh gọi LLM có thể trả wording
   khác → hash khác → vẫn đổi id, y hệt tình huống `--again` đã đo được).
2. UPSERT tại AA-CIS nên keyed đúng trên `atom_id` (content-hash mới), giống
   hệt pattern trên — `ON CONFLICT(atom_id) DO UPDATE`, không phải so
   `source_hash` cấp-tour rồi quyết định insert-toàn-bộ-hay-skip-toàn-bộ như
   hiện tại.
3. Ngày bị xoá khỏi itinerary (tour rút ngắn) nên xử lý bằng ràng buộc DB
   (FK + `ON DELETE CASCADE`, hoặc tương đương ở Postgres) nếu muốn tránh
   phải tự viết code dọn theo tầng cho từng bảng con (`angle_gate_request`,
   `content_piece` qua đó, `usage_log`, v.v. — nếu các bảng đó FK thẳng tới
   `tour_atoms.atom_id`). AA-CIS hiện KHÔNG có cascade này (grep STEP0 chính:
   không có `DELETE FROM acp_contract.tour_atoms` nào) — đây là quyết định
   riêng cần chốt, không tự động có được nếu chỉ đổi atom_id sang content-hash.

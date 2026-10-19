# Rà soát ACPv2 — Giai đoạn 3/4: Điều tra 10+1 nghi vấn chồng lấn/lỗi thời

*15/08/2026. Điều tra đủ 11 mục bằng description/comment Linear thật + code/git log + Notion ADR Log. KHÔNG sửa Linear/code/PRD trong phiên này.*

**Tóm tắt kết quả:** 8/11 mục = **(a) KHÔNG PHẢI VẤN ĐỀ** (độc lập hợp lệ) · 1/11 = **(b) NÊN CẬP NHẬT tài liệu** (không phải Linear) · 2/11 = **(c) CẦN NGHIEP QUYẾT ĐỊNH**.

---

## 1. AA-297 (EPIC gốc) vs AA-386 (UI-EPIC) → **(a) KHÔNG PHẢI VẤN ĐỀ**

Đọc đầy đủ description AA-297: mục "Sub-issues" liệt kê tường minh *"Contract · N0 · N2 · N3 · N4-N6 · N7 · N8 · Marketplace · S1-from-atom · Portal · Bake-off"* — toàn bộ backend/kiến trúc, **0 nhắc UI**. AA-386 (đọc ở Giai đoạn 1) scope tường minh *"Kế hoạch build UI tổng thể N0→N8"* — chỉ UI.

Hai epic KHÔNG double-count vì đứng trên 2 trục trực giao (backend logic vs UI expose), cùng chạm các issue con (AA-309, AA-320, AA-328...) là ĐÚNG dự kiến — 1 tính năng luôn cần cả 2 phần việc. Bằng chứng cụ thể: AA-320 (Gate B, backend persist, con của AA-297 track) và AA-388 (Gate B UI, con của AA-386) là 2 issue riêng biệt, không trùng nội dung — AA-320 làm bảng+endpoint, AA-388 làm trang bấm nút.

**Giữ nguyên**, không cần gộp/link gì thêm.

---

## 2. Chuỗi "writer model" (AA-334 Canceled / AA-337 Canceled / AA-308 Done / AA-392 Done) → **(a) KHÔNG PHẢI VẤN ĐỀ**

Xác nhận **100%** qua trích dẫn trực tiếp, không suy đoán:

- **AA-334 description** (cập nhật 06/08): *"SUPERSEDED, không chạy bake-off này nữa... Nghiep xác nhận trực tiếp: Palmyra X5 không dùng được cho writer (lý do vận hành, không cần bake-off lại)... chuyển thẳng sang Bedrock satellite acc1 — Sonnet."* Lý do Cancel là **vận hành** (Palmyra bất khả dụng), không phải vì thiết kế bake-off (AA-334's comment 26/07 thiết kế lại rất kỹ: 6 trục đo, n=40 paired) bị sai — thiết kế đó vẫn đúng, chỉ là premise (Palmyra viable) sụp trước khi kịp chạy.
- **AA-392 description** (dòng đầu tiên): *"S1-from-atom bị throttle Palmyra X5 1 req/phút, đúng finding đã đo trong AA-337 (Canceled, nhưng chỉ đóng phạm vi N7)."* — **xác nhận đúng 100%** chuỗi nhân-quả Giai đoạn 1 đã dựng: AA-337 Canceled chỉ đóng phạm vi quyết định (không cần theo dõi riêng cho N7 nữa vì N7 đã chuyển hẳn writer sang Sonnet), nhưng **dữ kiện đo được** (1 req/phút) không mất — được AA-392 trích dẫn lại nguyên vẹn 2 tuần sau để quyết định cho S1-from-atom.

Chuỗi: AA-308 (Done, bake-off gốc, thống kê không ý nghĩa — chính comment của Nghiep tự phát hiện p=0.115) → AA-334 (thiết kế lại nghiêm ngặt hơn, rồi Canceled vì lý do vận hành khác hẳn thống kê) → AA-337 (đo throttle, Canceled theo phạm vi hẹp) → AA-370 (N7 chuyển Sonnet) → AA-392 (S1-from-atom theo sau, trích AA-337). Đây là **quá trình ra quyết định tiến hoá hợp lệ**, ghi chép đầy đủ, không phải chồng lấn.

**Giữ nguyên.**

---

## 3. AA-315 (Canceled) vs AA-316 (Done) → **(a) KHÔNG PHẢI VẤN ĐỀ** — xác nhận nhanh như dự kiến

Description AA-315 tự ghi ngay dòng đầu: *"ĐÃ ĐIỀU TRA XONG — xem AA-316 cho hướng xử lý cuối cùng... Việc cần làm chuyển hết sang AA-316... Issue này đóng (Canceled, không phải Done — vì bản thân nó không phải việc cần làm, mà là phát hiện dẫn tới AA-316)."* Đúng quan hệ investigate→fix, không cần điều tra thêm.

---

## 4. AA-325 (Done) vs AA-326 (Backlog) vs AA-349 (Backlog) vs AA-393 (Backlog) → **(a) KHÔNG PHẢI VẤN ĐỀ**

Đọc đủ 4 description — xác nhận 4 lớp lỗi grounding **hoàn toàn tách bạch**, tự các issue đã phân biệt rõ (bảng so sánh ngay trong AA-326):

| Issue | Loại vi phạm | Bắt bằng gì | Trạng thái |
|---|---|---|---|
| **AA-325** | Fact bịa CÓ số/tên riêng cụ thể sai (vd "22-meter reclining Buddha") | Entailment check — token so với atom cite | Done, fix qua ADR-2026-033 |
| **AA-326** | Claim superlative/định tính KHÔNG số (vd "world's oldest...") — không sai rõ ràng, chỉ không có gì đỡ | Cần cơ chế khác (banned-pattern hoặc LLM judge riêng) — chưa build | Backlog |
| **AA-393** | Bug CỦA CHÍNH cơ chế AA-325 — nhãn thứ tự ("Site 1"/"Day 2") bị nhận nhầm là số liệu cần grounding | Sửa regex loại trừ pattern nhãn+số | Backlog, Low |
| **AA-349** | Checker chỉ so với atom ĐƯỢC CITE, không đối chiếu TOÀN VĂN nguồn — câu bịa hoàn toàn không citation nào lọt qua | Mở rộng phạm vi so sánh | Backlog |

4 vấn đề khác trục nhau thật: (đúng nội dung có số hay không) × (câu có tag hay không có tag nào) × (bug cụ thể của regex hiện tại). Không cái nào là bản sao của cái khác.

**Giữ nguyên**, không gộp.

---

## 5. AA-321 vs AA-322 vs AA-341 → **(a) KHÔNG PHẢI VẤN ĐỀ**

3 chủ đề hoàn toàn khác nhau về mặt kỹ thuật, chỉ giống ở cách phát sinh ("phát hiện phụ khi làm việc khác"):

- **AA-321**: `fastapi`/`starlette` chưa pin version → CI resolve khác dev, gây fail test giả. **Chủ đề: dependency pinning CI/CD.**
- **AA-322**: role `content` không có JWT thật → chặn Playwright e2e test tự động cho MỌI trang admin. **Chủ đề: auth/test-infra gap**, description tự xác nhận *"KHÔNG ảnh hưởng người dùng thật"*.
- **AA-341**: sổ ghi nợ 4 khoản kỹ thuật S1 CŨ (itinerary fix guard thiếu deterministic check / validate_node không so source length / `stop_reason` bị vứt / `max_tokens` default 4096) — description tự ghi rõ *"SỔ GHI NỢ, chưa phải lệnh làm"*, cần Nghiep quyết làm khoản nào.

Không phạm vi nào trùng — CI dependency ≠ test auth ≠ S1 prompt/token engineering debt. Cả 3 đều đúng đắn để tách issue riêng.

**Giữ nguyên.**

---

## 6. AA-395 vs AA-394 vs AA-302 (D5/trip_url) → **(a) KHÔNG PHẢI VẤN ĐỀ, xác nhận + bổ sung ADR-2026-030 CÓ THẬT**

Xác nhận lại đúng "3 tầng bổ sung nhau" như Giai đoạn 1 đã phân tích: AA-302 (VIEW + cột `trip_url`, Done) → AA-394 (backfill TẠM 87 tour để hệ thống chạy được ngay, Done) → AA-395 (engine THẬT sinh trang landing page tự động, vẫn Backlog).

**Bổ sung theo yêu cầu:** ADR-2026-030 **CÓ THẬT**, không phải chỉ được nhắc tên. Đọc trực tiếp Notion "ADR Log" (`358b8a41...`) — có bảng đầy đủ: *"ADR-2026-030 — AA HOST trang tour white-label... Status: ✅ Accepted, Date: 13/07/2026, Impl = AA-302."* Nội dung đầy đủ 4 lý do bắt buộc + đánh đổi ("+1–2 sprint landing page engine") — viết inline trong bảng ADR Log (không có trang con riêng, giống các ADR 024-030 khác; chỉ từ 031 trở đi mới có trang con riêng biệt — đây là pattern nhất quán, không phải ADR-030 bị thiếu sót).

**Phát hiện phụ đáng chú ý:** Notion "Weekly Report Tuần 4" (03-09/08) tự ghi: *"Phát hiện + xử lý dứt điểm ADR-2026-030 (landing page engine) bị quên gần 1 tháng — backfill"* — xác nhận độc lập từ nguồn khác rằng gap D5/AA-395 là có thật và đã được chính đội ngũ nhận ra, khớp 100% với đánh giá của Giai đoạn 1/2.

**Giữ nguyên**, không cần gộp.

---

## 7. AA-353 vs AA-346 vs AA-340 vs AA-339 → **(a) KHÔNG PHẢI VẤN ĐỀ**

Đọc đủ 4 description — xác nhận đây là **chuỗi điều tra tuần tự có chủ đích**, mỗi issue trích dẫn issue trước làm nền tảng, không lặp effort:

1. **AA-340** (Done): chạy S1 CŨ trên bộ Laos-biking thật lần đầu — tách khỏi AA-339 tường minh vì "việc (a) [chạy production hiện tại] bị kẹt sau việc (b) [bake-off nhiều model]" nếu gộp chung.
2. **AA-346** (Done): thí nghiệm quyết định H1 (do prompt) hay H2 (do model) — dùng CHÍNH dữ liệu AA-340 vừa tạo ra làm bằng chứng hiện tượng, đổi prompt/giữ model, kết luận H2 (do kiến trúc, không phải prompt).
3. **AA-339** (Done): bake-off thứ ba (đa model, cả S1 cũ lẫn S1-from-atom) — description tự giải thích rõ vì sao KHÔNG gộp vào AA-334 (bài toán khác: "trang tour theo ngày, ràng buộc số ngày deterministic" ≠ "blog tự sự 1.400 từ"), và trực tiếp dùng để phân định nguyên nhân AA-329 (khiếu nại của Ms. Thư).
4. **AA-353** (Done): implement fix kiến trúc — description mở đầu bằng *"Root cause đã xác nhận (S128, AA-346/AA-339 S130)"*, xây trực tiếp trên kết luận của 2 issue trước, không điều tra lại.

Đây là mẫu hình lý tưởng: mỗi issue có Done-when riêng, đóng góp 1 mảnh bằng chứng khác nhau (hiện tượng → nguyên nhân prompt/model → nguyên nhân đa model/deterministic → fix), không có issue nào làm lại việc issue khác đã làm.

**Giữ nguyên.**

---

## 8. AA-402 (EventBridge orphaned) vs AA-365 (Gate C veto) → **(a) KHÔNG PHẢI VẤN ĐỀ** — xác nhận nhanh

AA-402 description tự ghi: *"Phát hiện trong lúc chuẩn bị Terraform cho AA-365 (veto window schedule, 14/08) — KHÔNG phải lỗi do AA-365 gây ra, mà là tình trạng tồn tại từ trước, chỉ vừa được xác nhận thật."* Quan hệ nhân-quả-phát-hiện, không phải chồng lấn phạm vi — AA-365 là logic Gate C, AA-402 là hạ tầng Terraform module bị mồ côi từ trước (root cause hoàn toàn khác, không liên quan tính năng).

**Giữ nguyên.**

---

## 9. AA-397/398/399 (acc3 satellite) vs AA-296 (acc1 satellite, L5 gốc) → **(b) NÊN CẬP NHẬT tài liệu (không phải Linear)**

PRD (đọc lại nguyên văn, Giai đoạn 1): **"L5: Bedrock only. Cross-account sang acc1 cho Anthropic models (AA-296, đã Done)."** — chỉ nhắc 1 account.

Đối chiếu ADR Log: **ADR-2026-027** (13/07) là ADR gốc cho "vệ tinh Bedrock", viết cụ thể cho acc1 (`[acc1 id]`). Khi AA-397 (12/08) thêm acc3 (`[acc3 id]`) làm satellite CHÍNH (acc1 lùi xuống fallback) — đây là thay đổi kiến trúc đáng kể (2 tài khoản vệ tinh song song, chuỗi fallback 6 tầng thay vì 3 tầng) — nhưng **KHÔNG có ADR mới nào ghi lại quyết định này** (AA-397/398/399 description chỉ tham chiếu lẫn nhau, không tham chiếu ADR nào). Điều này phá vỡ pattern nhất quán đã thấy ở các thay đổi account khác (ADR-2026-022 cho account restructure, ADR-2026-027 cho satellite acc1, ADR-2026-032 cho Activate credits reject — mọi thay đổi account-level lớn khác đều có ADR).

**Kết luận:** có 2 việc nên làm, không tự làm:
1. Cập nhật PRD L5 để phản ánh acc3 (đơn giản, đã có sẵn nội dung đúng trong `client.py`'s docstring để tham khảo).
2. Cân nhắc viết 1 ADR mới (hoặc amend ADR-2026-027) cho quyết định "acc3 là satellite chính, acc1 lùi fallback" — để nhất quán với cách các quyết định account-level khác đã được ghi lại, và để tránh lặp lại đúng loại gap mà ADR-2026-037 vừa mới (29/07) cảnh báo ("mọi ADR/PRD viết 'cơ chế X đã có' phải kèm nguồn verify").

---

## 10. AA-385 (Canceled, admin_acp_proxy.py data-risk) → **(a) KHÔNG PHẢI VẤN ĐỀ** — xác nhận qua chính comment, không chỉ tin note cũ

Đọc trực tiếp 2 comment thật (không chỉ description): comment điều tra STEP 0 (09/08) rất kỹ — đọc toàn bộ 703 dòng `admin_acp_proxy.py`, xác nhận **0 bảng ACPv2 nào bị đụng** (migration 096 tự tách `acp_v2_runs` riêng từ trước — "rủi ro trộn bảng đã được lường trước và chặn chủ động ở tầng migration"), gọi thật 3/4 route qua ECS exec (đều trả `[]` rỗng), và **quan trọng nhất: tra CloudWatch logs thật** (phát hiện phụ: retention thật là **14 ngày, không phải 30** như config tưởng) — tìm thấy 3 request thật ngày 08/08 (đúng ngày AA-384 merge), 0 POST/mutate nào trong toàn bộ cửa sổ log.

Comment quyết định cuối (Nghiep, cùng ngày): *"Quyết định: gộp vào AA-386... Lý do: rủi ro thực tế là nhầm lẫn UI... không phải rủi ro dữ liệu... Đóng issue này — Cancelled (không phải Done, vì không có gì 'xong', chỉ là kết luận đã đưa ra và chuyển hướng xử lý sang AA-386)."*

**Xác nhận: Cancel ĐÚNG, có bằng chứng số liệu thật đầy đủ (query + log + code read), không phải Cancel hời hợt.** Việc sửa nhãn sidebar đã chuyển đúng sang AA-386 (đã Done — xem Giai đoạn 1 Phần B, AA-390 UI 7/7 sidebar label fix).

**Giữ nguyên.**

---

## 11. AA-333 (đo cost GPT-4.1 S1) → **(c) CẦN NGHIEP QUYẾT ĐỊNH**

Đọc description đầy đủ: premise gốc của AA-333 (*"S1 production vẫn viết 100% bằng GPT-4.1 fallback... ADR-001 trên thực tế đã chết"*) là **CÙNG MỘT claim đã bị Giai đoạn 2 chứng minh sai bằng DB thật** (0 lần GPT-4.1 kể từ 16/07, 10 run gần nhất đều qua Bedrock satellite).

Đây KHÔNG đơn giản là "đóng issue vì hết cần" — vì "Done when" của AA-333 có 3 phần, chỉ 1 phần đã tự động được trả lời bởi diễn biến khác (cost thực tế → gần $0 vì GPT-4.1 ngừng được gọi), còn 2 phần vẫn treo thật:
- ✅ *"Có con số chi phí GPT-4.1 thật của S1 trong 1 tháng"* — gián tiếp đã có (Giai đoạn 2: 0 lần gọi từ 16/07 → chi phí ~$0), nhưng KHÔNG PHẢI qua đo trực tiếp như issue yêu cầu (đối chiếu hoá đơn OpenAI thật) — chỉ suy ra từ absence of fallback_used=true.
- ❌ *"Ghi kết luận vào PRD ACP v2 (mục Q7) + memory"* — CHƯA làm, PRD vẫn còn nguyên claim cũ (v1.1, 24/07).
- ⚠️ Câu hỏi gốc Q7 muốn trả lời ("giữ B hay mở lại Q7") **đã tự trả lời theo hướng khác hẳn dự kiến** — không phải vì đo được cost nhỏ, mà vì hạ tầng satellite (AA-296/397) đã đóng gap kỹ thuật mà Q7 lo ngại (S1 gọi API ngoài AWS), tự động qua 1 con đường không liên quan tới quyết định Q7 ban đầu.

**Câu hỏi cụ thể cho Nghiep:** AA-333 nên (i) đóng với ghi chú "premise đã đổi, cost thực tế ~$0 do satellite wiring — không đo trực tiếp qua hoá đơn nữa", hay (ii) vẫn chạy đối chiếu hoá đơn OpenAI 1 lần cho 15 lần gọi GPT-4.1 lịch sử (từ 08/07-16/07) để có con số chính xác thay vì suy luận, trước khi đóng? Và ai cập nhật PRD Q7 — Claude Code khi làm Giai đoạn 4, hay Nghiep tự làm?

---

## Bối cảnh phụ phát hiện — ADR-2026-037 (đọc trong lúc verify Mục 9)

Trong lúc đọc ADR Log để verify Mục 9, phát hiện **ADR-2026-037** (29/07/2026, S128) — *"Definition of Done phải bao gồm VERIFY PERSIST trên dữ liệu thật"* — quy tắc này **trực tiếp giải thích root cause của phát hiện Mục 3 Giai đoạn 2** (`acp_output_rules` 0 row dù AA-363 Done): ADR-2026-037 liệt kê chính xác 4 ca cùng loại lỗi đã biết trước đó (bao gồm `raw_tours.review_status`→AA-316, Gate B→AA-320, `tour_content_versions`, `s1_from_atom` output) — cùng 1 hình dạng: *"thiết kế chốt → code build → test pass → issue đánh Done — mà chưa ai kiểm nó có ghi được gì xuống DB thật không."* `acp_output_rules` (Mục 3) là **ca thứ 5 của đúng loại lỗi này**, xảy ra SAU KHI ADR-2026-037 đã ban hành (AA-363 Done 05/08, sau ADR ngày 29/07) — nghĩa là quy tắc "3 bằng chứng bắt buộc trước khi Done" (query đếm >0 row / đọc tay 1 row / xác nhận đường đọc ra) **chưa được áp dụng nhất quán cho AA-363**, dù đã có ADR yêu cầu từ trước đó 1 tuần. Đáng đưa vào Giai đoạn 4 như 1 phát hiện độc lập (không thuộc 11 mục gốc, chỉ ghi lại ở đây để không mất).

---

*Dừng lại tại đây theo đúng phạm vi Giai đoạn 3/4 — chờ Nghiep review trước khi sang Giai đoạn 4 (tổng hợp tài liệu cuối cùng).*

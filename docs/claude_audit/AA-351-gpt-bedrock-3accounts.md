# AA-351 — GPT trên Bedrock: khảo sát 3 account (acc1/acc2/acc3)

Ngày khảo sát: 2026-08-16
Phương pháp: `list-foundation-models` + `bedrock-runtime converse` (invoke thật) + `list-foundation-model-agreement-offers` + `service-quotas`, theo đúng phương pháp AA-335. Không tạo/sửa file production, không commit script tạm (chạy trực tiếp qua Bash, không lưu file).

## 0. Verify quyền truy cập 3 account

| Account | Profile | Account ID | Region | Kết quả `sts get-caller-identity` |
|---|---|---|---|---|
| acc2 | `aa365-admin` | [acc2 id] | us-west-1 | ✅ OK, không cần MFA tương tác |
| acc3 | `nghiep_aa365` | [acc3 id] | us-west-1 | ✅ OK, không cần MFA tương tác |
| acc1 | `pqnghiep-admin` | [acc1 id] | us-west-1 | ✅ OK, không cần MFA tương tác |

Cả 3 profile dùng được ngay, không có profile nào bị chặn/cần hỗ trợ từ Nghiệp.

## 1. Model GPT nào thực sự có trong catalog Bedrock (us-west-1)?

`list-foundation-models` trả về **kết quả giống hệt nhau ở cả 3 account** — catalog model là theo region, không theo account:

| Model hỏi trong task | Có trong catalog us-west-1? |
|---|---|
| GPT-5.4 | ❌ Không có |
| GPT-5.5 | ❌ Không có |
| GPT-5.6 Sol (`openai.gpt-5.6-sol`) | ✅ Có, `ACTIVE`, start-of-life 2026-08-13 |
| GPT-5.6 Terra (`openai.gpt-5.6-terra`) | ✅ Có, `ACTIVE`, start-of-life 2026-08-13 |
| GPT-5.6 Luna (`openai.gpt-5.6-luna`) | ✅ Có, `ACTIVE`, start-of-life 2026-08-13 |
| gpt-oss-120b | ❌ Không có |
| gpt-oss-20b | ❌ Không có |

**Phát hiện quan trọng:** GPT-5.4/5.5 GA từ 6/2026 và gpt-oss-120b/20b (open-weight, lẽ ra
có sẵn) đều **KHÔNG xuất hiện trong catalog Bedrock us-west-1 ở bất kỳ account nào** — không
phải vấn đề quyền/account, mà catalog Bedrock region này đơn giản chưa (hoặc không) onboard
các model đó; AWS nhảy thẳng vào GPT-5.6 series (start-of-life 2026-08-13, tức mới ra được 3
ngày tại thời điểm khảo sát). `total bedrock model catalog us-west-1 = 29 models`, quét toàn
bộ 29 model không tìm thấy chuỗi `oss` ở đâu. Không loại trừ các model này có ở region khác
(không nằm trong phạm vi task — task chỉ định us-west-1).

→ Chỉ có 3 model để test invoke thật: **GPT-5.6 Sol/Terra/Luna**.

## 2. Kết quả invoke thật — 3 account × 3 model

Test qua `bedrock-runtime converse` (model-agnostic, tương đương invoke-model), dùng cả
inference profile `us.openai.*` và `global.openai.*` (3 model này chỉ hỗ trợ
`INFERENCE_PROFILE`, không hỗ trợ invoke trực tiếp bằng modelId trần).

| Account | GPT-5.6 Sol | GPT-5.6 Terra | GPT-5.6 Luna | Loại lỗi |
|---|---|---|---|---|
| acc2 (`aa365-admin`) | ❌ | ❌ | ❌ | `AccessDeniedException: openai.gpt-5.6-{x} is not available for this account` |
| acc3 (`nghiep_aa365`) | ❌ | ❌ | ❌ | `AccessDeniedException: openai.gpt-5.6-{x} is not available for this account` |
| acc1 (`pqnghiep-admin`) | ❌ | ❌ | ❌ | `AccessDeniedException: openai.gpt-5.6-{x} is not available for this account` |

**Cả 3 account đều KHÔNG invoke được — nhưng đây LÀ MỘT LOẠI LỖI KHÁC hẳn với block
Anthropic trên acc2.** Đối chiếu trực tiếp (test lại trong cùng session để chắc chắn số liệu
mới nhất, không suy từ memory cũ):

```
# GPT-5.6 (cả 3 account, giống hệt nhau)
AccessDeniedException: openai.gpt-5.6-sol is not available for this account.
You can explore other available models on Amazon Bedrock. ...

# Anthropic Haiku 4.5 trên acc2 (test lại để đối chiếu, 2026-08-16)
ValidationException: Access to this model is not available for channel program
accounts. Reach out to your AWS Solution Provider or AWS Distributor...
```

- `ValidationException ... channel program accounts` (Anthropic/acc2) = **hard block cấp
  account**, không có đường xin quyền — đây là chính sách AWS Distributor/channel-program
  chặn cứng, không phụ thuộc vào việc account có "request access" hay không.
- `AccessDeniedException ... not available for this account` (GPT-5.6/cả 3 account) = **model
  access CHƯA ĐƯỢC CẤP** — cơ chế "request model access" tiêu chuẩn của Bedrock cho model
  bên thứ 3 (marketplace-style, cần chấp nhận EULA giá), KHÔNG phải bị chặn cứng.

Bằng chứng cho kết luận trên: cả 3 account đều gọi thành công
`list-foundation-model-agreement-offers --model-id openai.gpt-5.6-sol` và nhận về **cùng một
offer** (`offer-gnqokrqqvdbgw`, cùng bảng giá đầy đủ) — nghĩa là AWS coi cả 3 account đều
**đủ điều kiện** mua/dùng GPT-5.6, chỉ là chưa account nào bấm "accept agreement"
(`create-foundation-model-agreement`) để kích hoạt. Đối chiếu: acc2 không có agreement-offer
nào cho Anthropic model bị chặn — vì đó là chặn cứng theo policy, không phải "chưa xin quyền".

`service-quotas list-service-quotas --service-code bedrock` trên acc3: quét cả 225 quota
Bedrock, **không có quota RPM/TPM nào cho GPT/OpenAI** — khớp với việc access chưa được cấp
(quota RPM/TPM chỉ xuất hiện sau khi model được kích hoạt).

## 3. Giá + rate limit + Batch/Prompt Caching (từ agreement offer, KHÔNG phải verify qua invoke thật vì chưa có quyền)

Vì chưa account nào accept agreement nên **không thể đo giá/rate-limit qua invoke thật**
(bước 3 của task đề bài). Tuy nhiên `list-foundation-model-agreement-offers` trả về full rate
card niêm yết cho GPT-5.6 Sol (áp dụng như nhau cho cả 3 account, cùng offer):

| Dimension (rút gọn, USD/1M token) | Standard | Priority | Flex/Batch | Global Standard |
|---|---|---|---|---|
| Input tokens | $5.50 | $11.00 | $2.75 | $5.00 |
| Output tokens | $33.00 | $66.00 | $16.50 | $30.00 |
| Cached input tokens | $0.55 | $1.10 | $0.275 | $0.50 |
| Cache write tokens | $6.875 | $13.75 | $3.4375 | $6.25 |
| Input tokens (long-ctx) | $11.00 | — | $5.50 | $10.00 |
| Output tokens (long-ctx) | $49.50 | — | $24.75 | $45.00 |

(Giá long-ctx áp dụng khi context vượt ngưỡng dài; "Global" = inference profile
`global.openai.gpt-5.6-sol` rẻ hơn ~10% so với `us.openai.gpt-5.6-sol` region-pinned.)

- **Batch inference: CÓ hỗ trợ** — rate card có đầy đủ dimension `*_batch` riêng (rẻ hơn ~50%
  so với standard, giống pattern batch của Anthropic/Nova).
- **Prompt Caching: CÓ hỗ trợ** — rate card có `cached_input_tokens_*` và
  `cache_writes_tokens_*` cho cả tier standard/priority/flex/global.
- **Rate limit (RPM/TPM) thật: KHÔNG xác định được** — chưa có quota nào cấp (xem mục 2),
  chỉ biết được sau khi 1 account accept agreement và Bedrock provision quota.

⚠️ Đây là **giá niêm yết trong agreement offer**, chưa verify bằng invoke thật (vì access
chưa cấp) — coi là tham khảo, không phải số đã đo thực tế như bảng AA-335 (AA-335 đo được vì
model đó invoke được thật).

## 4. So sánh 3 account

**Không có khác biệt giữa 3 account** cho GPT-5.6 — cả acc1/acc2/acc3 đều ở cùng trạng thái
"đủ điều kiện, chưa accept agreement". Đây là điểm khác hẳn dự đoán trong task (kỳ vọng acc2
chặn còn acc1/acc3 satellite lọt qua, giống pattern Claude). Với GPT, **KHÔNG có phân hoá
theo account* — acc2 (dù bị channel-program chặn cứng Anthropic) lại KHÔNG bị chặn kiểu đó
với GPT; cả 3 account đứng ngang nhau, cùng chờ 1 hành động: accept agreement.

## 5. Việc CHƯA làm (cố ý, cần quyết định của Nghiệp)

Tôi **không gọi** `create-foundation-model-agreement` (hành động accept EULA + bảng giá) ở
bất kỳ account nào — đây là hành động khó đảo ngược/mang tính cam kết pháp lý với AWS
(chấp nhận rate card, dù chỉ tính phí khi thực invoke), nằm ngoài phạm vi "khảo sát" của task
này và cần Nghiệp xác nhận account nào nên là account thật sự accept (nên là acc3, theo vai
Bedrock satellite chính hiện tại — xem mục 6). Nếu Nghiệp muốn thử invoke thật GPT-5.6, bước
tiếp theo đơn giản là 1 lệnh `create-foundation-model-agreement` trên account được chọn.

## 6. Kết luận & khuyến nghị

- **GPT-5.4, GPT-5.5, gpt-oss-120b/20b: chưa tồn tại trên Bedrock us-west-1**, ở cả 3 account
  — không phải vấn đề quyền, catalog region này chưa có. Loại khỏi khảo sát judge tiếp theo
  cho tới khi AWS onboard.
- **GPT-5.6 Sol/Terra/Luna: có trong catalog cả 3 account, nhưng cần 1 bước "accept model
  agreement" (chưa làm) trước khi invoke được** — đây KHÔNG phải cùng cơ chế
  channel-program chặn Anthropic trên acc2 (khác hẳn: `AccessDeniedException` chờ-cấp-quyền
  vs `ValidationException` chặn-cứng). Về lý thuyết, sau khi accept, **cả 3 account đều khả
  thi** — không có account nào bị loại trừ cứng như Anthropic trên acc2.
- **Khuyến nghị account để thử tiếp (nếu Nghiệp quyết định làm)**: **acc3**
  (`nghiep_aa365`, Bedrock satellite CHÍNH hiện tại, có fund $250 real cost, ưu tiên
  interactive + Batch theo account map) — accept agreement ở đây trước, dùng acc1 làm
  fallback nếu acc3 lỗi/hết fund, đúng pattern hiện đang dùng cho Claude satellite. KHÔNG
  nên accept trên acc2 vì acc2 là app chính/production ECS-RDS, không phải nơi để thử
  nghiệm model mới có rate card riêng.
- **Về câu hỏi judge F8/F9 thay Nova Pro**: GPT-5.6 (vendor khác hẳn Claude writer VÀ khác
  Nova) về lý thuyết đáp ứng đúng nguyên tắc "judge khác họ với writer" nêu trong task, VÀ
  nằm trong AWS (data residency tốt hơn gọi OpenAI ngoài như GPT-4.1/S1 hiện tại). Nhưng
  **CHƯA thể kết luận khả thi thật** — cần bước accept agreement + 1 lượt invoke thật để đo
  latency/chất lượng judge trước khi so sánh với Nova Pro. Batch + Prompt Caching đều được hỗ
  trợ theo rate card (tốt cho việc chấm N7 hàng loạt) — điểm cộng nếu triển khai judge mới.
  **KHÔNG đổi judge production** — việc này giữ nguyên đúng ràng buộc gốc AA-351, chờ quyết
  định của Nghiệp có nên bỏ bước accept-agreement + invoke-thử hay không.

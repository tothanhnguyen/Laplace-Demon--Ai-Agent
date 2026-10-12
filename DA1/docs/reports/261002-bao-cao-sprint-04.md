---
title: "Báo cáo nghiệm thu Sprint 4 — Core tools qua Telegram"
date: 2026-10-02
status: completed
---

# Báo cáo nghiệm thu Sprint 4 — Core tools qua Telegram

## Kết luận

Sprint 4 được chủ dự án xác nhận hoàn tất ngày 2026-10-02. Phạm vi đã nghiệm thu gồm tool registry có schema, web search/extract có nguồn, xử lý PDF/DOCX/TXT qua Telegram và reminder có persistence/recovery.

Phân loại bằng chứng trong báo cáo:

- **Automated:** lần chạy đầy đủ trước đó ghi nhận `164 passed`; Ruff đạt.
- **Shared-runtime smoke:** LLM thật và Tavily thật đã chạy `web_search → fetch_page`, trả tóm tắt và nguồn.
- **Telegram live:** available tool-call records confirm web search/extract; the owner confirms all required live cases passed, including document handling, reminder create/update/cancel, restart and misfire-grace recovery, crash-after-claim recovery, `/forget`, missing Tavily key, corrupt/unsupported/oversize uploads, and honest scan-only PDF handling.
- Chi tiết raw của lượt nghiệm thu cuối — task/reminder ID, timestamp và Telegram message ID — không được lưu trong workspace này; các bước cuối được ghi nhận theo xác nhận của chủ dự án, không gán nhầm vào bản ghi cũ.

## Phạm vi đã hoàn thành

- Registry công khai contract và validate input/output; runtime giữ identity, ownership và attachment capability ở trusted context.
- Web Search + Extract dùng Tavily; câu trả lời chỉ dẫn nguồn đã xuất hiện trong tool result, lỗi upstream không được mô tả thành thành công.
- PDF/DOCX/TXT được trích xuất theo locator và giới hạn dung lượng/context; PDF scan-only được báo không hỗ trợ OCR.
- Reminder private-chat hỗ trợ tạo, cập nhật, hủy và phục hồi qua restart; delivery là at-least-once, không cam kết exactly-once.
- Tích hợp Telegram dùng shared chat runtime; các giới hạn và failure paths được kiểm tra trong automated và live acceptance.

## Bằng chứng

### Automated và shared runtime

- Full Python suite: `164 passed`.
- Ruff: passed.
- Shared-runtime smoke với LLM/Tavily thật hoàn tất `web_search → fetch_page` và trả nguồn tương ứng.
- Phase 1–4 được ghi nhận hoàn tất; branch `sprint04` nằm trên baseline `sprint03`.

### Web Telegram live

- Task `14` ghi nhận `web_search` và `fetch_page` thành công; nguồn trích xuất khớp citation hiển thị trong Telegram.
- Một lượt thời tiết TP.HCM trả summary kèm trang nguồn Vietnam.vn.

### Tài liệu

- DOCX locator được đối chiếu với fixture: đoạn 2–4 và `table:1/row:2–4` khớp mục tiêu, quyết định/rủi ro và các dòng hành động.
- TXT locator được đối chiếu với fixture: dòng 4–9 khớp mục tiêu, quyết định, người phụ trách, thời hạn và rủi ro.
- Fixture manifest:
  - PDF 1 trang: SHA-256 `3b3f693ade140428052dfb7cdf219b61adde80f12c53126a5a890df2c8d63435`
  - DOCX: SHA-256 `dadef2e55df822dd9685792ba7b5ff7457c82dacc496506de65b3fe67085f90b`
  - UTF-8 TXT: SHA-256 `aa1ddf062987c062999818fa4918c7518eb4bf97f61e4148705982ab16e5a5e5`

### Reminder và restart

- Reminder `#5` được tạo lúc `2026-10-01T01:02:11Z`, đến hạn `01:07:11Z`, và bản ghi local cho thấy gửi lúc `01:07:12Z`, một attempt, Telegram message ID `107`. Đây là bằng chứng delivery bình thường, không phải bằng chứng restart.
- Chủ dự án xác nhận một lượt E2E riêng sau đó đã hoàn thành web search, source reading, tạo reminder, restart trên cùng DB và gửi notification sau restart.
- Chủ dự án cũng xác nhận update reminder, cancel không gửi sau due, và scan-only PDF trả lời trung thực. Các lượt xác nhận này không có raw trace đi kèm trong workspace.

## Giới hạn được chấp nhận

- Không OCR PDF chỉ có ảnh.
- Document context có giới hạn; hệ thống không cam kết tóm tắt toàn bộ mọi tài liệu lớn.
- Chỉ triển khai một bot/scheduler process cho một DB.
- Reminder delivery là at-least-once; crash sau khi Telegram đã nhận nhưng trước khi DB finalize có thể tạo thông báo trùng.
- Không có Approval Gate, RAG/vector DB, browser automation, recurring reminder hoặc exactly-once delivery.

## Video nghiệm thu — checklist

1. Kiến trúc và tool contracts — 1 phút.
2. Web search/extract, citation và lỗi upstream — 2 phút.
3. PDF/DOCX/TXT, locator và giới hạn OCR — 3 phút.
4. Reminder update/cancel/restart/delivery — 3 phút.
5. Failure paths và giới hạn được chấp nhận — 1 phút.

## Sign-off

**Trạng thái: Completed — chủ dự án xác nhận nghiệm thu ngày 2026-10-02.** Automated results, shared-runtime smoke, bản ghi Telegram sẵn có và owner-attested live cases được phân biệt rõ; không đưa secret, raw tài liệu người dùng hoặc đường dẫn riêng tư vào báo cáo.

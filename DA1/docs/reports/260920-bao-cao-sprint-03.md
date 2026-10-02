---
title: "Báo cáo Sprint 3 và định hướng Sprint 4"
date: 2026-09-20
status: completed
---

# Báo cáo Sprint 3 và định hướng Sprint 4

## 1. Tổng quan

Sprint 3 tập trung xây dựng context có giới hạn, task card, bộ nhớ phiên và cơ chế quản lý dữ liệu theo từng người dùng. Sprint đã hoàn tất code, kiểm thử tự động, demo Telegram và thực nghiệm so sánh ba chiến lược context.

## 2. Các hạng mục đã hoàn thành trong Sprint 3

### Context và bộ nhớ

- Xây dựng context ba tầng: system policy, trạng thái phiên và lịch sử gần nhất.
- Giới hạn context mặc định 12.000 ký tự; dừng an toàn nếu phần bắt buộc vượt ngân sách.
- Giữ nguyên nhóm action/observation, không cắt rời quan hệ nhân quả.
- Lưu đầy đủ tool result trong SQLite; model chỉ nhận excerpt tối đa 1.200 ký tự.
- Thêm rolling summary và watermark để nén lịch sử cũ theo từng phiên.
- Có extractive fallback khi model tóm tắt lỗi hoặc không khả dụng.

### Task và vòng đời xử lý

- Chỉ tạo task khi agent thật sự gọi tool.
- Liên kết Task, Step, ToolCall và Trace với đúng request và user.
- Task card hiển thị mục tiêu, tiêu chí hoàn thành, bước hiện tại, tool đã dùng, phần việc còn lại và trạng thái.
- Giới hạn tối đa năm bước tool để tránh vòng lặp vô hạn.
- Thêm cooperative cancellation qua lệnh `/cancel`.
- Ngăn hai lượt xử lý đồng thời của cùng một user bằng `TurnLease`.

### Quản lý memory trên Telegram

- `/memory`: xem summary, hội thoại gần nhất, task, tool preview và usage.
- `/forget`: cảnh báo phạm vi dữ liệu sẽ xóa.
- `/forget confirm`: xóa conversation, message, task, step, trace và tool result theo ownership.
- Giữ nguyên User và lịch sử LLM usage sau khi xóa memory.
- Chỉ cho phép xem/xóa memory trong private chat.

### Database và an toàn dữ liệu

- Migration Sprint 3 chạy lặp lại an toàn trên database Sprint 2.
- Bật kiểm tra foreign key cho mọi SQLite connection.
- Summary và watermark được cập nhật nguyên tử bằng compare-and-update.
- Transaction ngắn; không giữ transaction trong lúc gọi LLM hoặc chạy tool.
- Từ chối xóa nếu phát hiện ownership của Trace hoặc LLMCall không nhất quán.

### Thực nghiệm context

- Xây dựng 8 tình huống synthetic cố định và runner dùng chung runtime production.
- So sánh ba chiến lược: `full`, `window10` và `sprint3`.
- Offline evaluation: 48/48 lượt hoàn tất, kết quả deterministic.
- Live evaluation: 24/24 lượt hoàn tất, `incomplete=false`.

| Chiến lược | Thành công | Tokens |
|---|---:|---:|
| `full` | 8/8 | 255.950 |
| `window10` | 4/8 | 138.819 |
| `sprint3` | 7/8 | 175.878 |

Sprint 3 giảm 80.072 tokens, tương đương 31,3% so với `full`, đồng thời giữ thêm ba tình huống thành công so với `window10`. Trường hợp E-05 thất bại vì fact nằm giữa tool payload và không xuất hiện trong bounded head/tail excerpt; đây là giới hạn đã biết, không sửa rubric để làm đẹp kết quả.

## 3. Bằng chứng nghiệm thu

| Hạng mục | Kết quả |
|---|---|
| Test suite | 77 tests passed |
| Ruff | All checks passed |
| Demo Telegram | Memory, tool card, forget và usage hoạt động đúng |
| Offline evaluation | 48/48 completed, `incomplete=false` |
| Live evaluation | 24/24 completed, `incomplete=false` |
| Artifact cuối | `Laplace-Demon/experiments/results/20260916T145221Z/results.json` |

## 4. Các hạng mục Sprint 4 sẽ thực hiện

Sprint 4 mở rộng runtime Sprint 3 thành bộ core tools dùng trực tiếp qua Telegram. Trạng thái hiện tại: chưa triển khai.

### Giai đoạn 1 — Chuẩn hóa tool contract

- Mở rộng `ToolSpec` với purpose, hướng dẫn sử dụng, risk level, input schema và output schema.
- Thêm `ToolContext` chứa identity và ownership đáng tin cậy; model không được tự truyền user ID, destination hoặc file path.
- Chuẩn hóa `ToolResult` gồm `ok`, `data`, `error`, `error_code`, `retryable`, `sources` và latency.
- Validate cả input lẫn output bằng Pydantic.
- Loại `read_file` path-based khỏi production registry; tài liệu dùng attachment capability.

### Giai đoạn 2 — Web Search và Extract

- Tích hợp Tavily Search để tìm nguồn thật.
- Tích hợp Tavily Extract để lấy nội dung URL công khai.
- Trả citation/URL đã xuất hiện trong tool result.
- Không tạo fake result khi thiếu API key, hết quota hoặc upstream lỗi.
- Giới hạn số kết quả, timeout và kích thước nội dung đưa vào context.

### Giai đoạn 3 — Xử lý tài liệu

- Nhận PDF, DOCX và UTF-8 TXT qua Telegram, tối đa 10 MiB.
- Dùng attachment ID opaque thay vì cho model truy cập filesystem path.
- Trích xuất text theo page/section/paragraph có locator.
- Đọc theo page/unit hữu hạn và cho phép model gọi tiếp khi cần.
- Từ chối trung thực file scan không có text, file hỏng, file quá lớn hoặc định dạng không hỗ trợ.
- Không triển khai OCR, spreadsheet, archive hoặc tái tạo bảng PDF trong sprint này.

### Giai đoạn 4 — Reminder bền qua restart

- Thêm bảng `reminders` có ownership theo user.
- Hỗ trợ tạo, cập nhật và hủy reminder trong private chat.
- Lưu thời gian UTC và phục hồi reminder sau khi bot restart.
- Dùng APScheduler làm dispatcher; SQLite vẫn là source of truth.
- Retry delivery có giới hạn và ghi trạng thái `sent`, `failed`, `missed` hoặc `delivery_unknown`.
- `/forget confirm` phải xóa reminder đúng user và không đua với reminder đang gửi.

### Giai đoạn 5 — Telegram integration và nghiệm thu

- Chạy luồng thật: tìm web, đọc nguồn/tài liệu, tạo reminder, restart bot và nhận thông báo.
- Kiểm thử invalid input, thiếu cấu hình, upstream timeout, file hỏng và owner mismatch.
- Xác nhận agent không mô tả tool failure thành success.
- Chạy toàn bộ test suite và Ruff sau integration.
- Cập nhật README, `.env.example`, hướng dẫn vận hành và báo cáo Sprint 4.

## 5. Mục tiêu bàn giao Sprint 4

- Tool registry có contract rõ và validate đầy đủ.
- Web research trả evidence thật kèm citation.
- PDF/DOCX/TXT được xử lý an toàn qua Telegram.
- Reminder thuộc đúng user, tồn tại qua restart và gửi đúng thời điểm.
- Có một kịch bản Telegram end-to-end chạy xuyên suốt các tính năng trên.

## 6. Giới hạn phạm vi Sprint 4

Sprint 4 không triển khai Approval Gate, verifier, RAG/vector database, OCR, recurring reminder, calendar, web UI, arbitrary shell hoặc exactly-once delivery. Các nội dung này thuộc sprint sau hoặc nằm ngoài phạm vi dự án hiện tại.

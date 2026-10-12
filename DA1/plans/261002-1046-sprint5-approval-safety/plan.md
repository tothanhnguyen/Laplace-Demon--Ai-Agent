---
title: "Sprint 5: Approval Gate và cơ chế an toàn"
description: "Kiểm soát mọi tool execution bằng policy, duyệt hành động qua Telegram, chặn vượt quyền, giới hạn ngân sách và bảo vệ nhật ký."
status: pending
priority: P1
effort: "60h dự kiến + 12h dự phòng"
branch: sprint05
tags: [feature, backend, database, telegram, security, approval, sprint-5]
created: 2026-10-02
updated: 2026-10-02
blockedBy: []
blocks: []
---

# Sprint 5: Approval Gate và cơ chế an toàn

## Mục tiêu và baseline

**Hành động nhạy cảm phải dừng trước side effect, hiện đúng nội dung sẽ thực thi và chỉ chạy sau khi chính owner duyệt.** Policy là code ở execution boundary, không phải lời hứa của model. Plan-only; chưa triển khai hoặc nghiệm thu Sprint 5.

- Code root: `Laplace-Demon/`; không port `Laplace-Demon-Beta/`, không thay decorator registry, không tạo agent loop thứ hai.
- Sprint 4 đã completed theo [báo cáo nghiệm thu](../../docs/reports/261002-bao-cao-sprint-04.md). `164 passed`/Ruff là bằng chứng lịch sử trong báo cáo, không phải lần chạy của phiên lập plan này.
- Reuse: `ToolSpec`, strict schemas, `ToolContext`, canonical `ToolResult`, bounded untrusted context, owner-filtered reminder SQL, `TurnLease`, SQLite migrations và shared chat runtime.
- Gap đã đọc: risk chỉ là `low/medium/high`; directive `/remind*` hiện cấp quyền chạy ngay; chưa có pause/approval, task time/cost budget hoặc stop theo lỗi tool lặp.
- Plan private nằm ngoài repo public; không đưa đường dẫn/slug này vào source, README hay artifact public. Đường dẫn triển khai trong phase files tính từ code root.

## Cách dùng plan khi code

1. Đọc **phase đang làm**, mục “Thứ tự code” và “Contract chốt”; không cần đọc lại toàn bộ sprint mỗi lần.
2. Làm theo ID công việc `P1-01`, `P2-01`… trong từng phase. Bảng file là phạm vi sửa; tên API mới là thiết kế dự kiến, không phải symbol đã tồn tại.
3. Mỗi phase phải có đường chạy thật và gate riêng; đánh checkbox sau khi quan sát kết quả. `Pending` hiện nghĩa **chưa triển khai**, không phải plan còn thiếu nội dung.
4. Các phase dùng chung file nên làm tuần tự với một integration owner. Chỉ phát hành cutover Sprint 5 sau gate G5; không deploy bản giữa chừng thiếu approval/budget.
5. Tra [ma trận nghiệm thu](./phase-05-security-acceptance.md) để biết cần kiểm hành vi nào. Offline tests kiểm cơ chế; Telegram/model thật kiểm trải nghiệm và hành vi xuyên tuyến.

| Thuật ngữ | Nghĩa dùng trong plan |
|---|---|
| Proposal | Bản đề xuất một thao tác, chứa dữ liệu đã chuẩn hóa để người dùng duyệt. |
| Approval | Bản ghi DB về quyết định cho **một** proposal; không phải quyền dùng tool vô hạn. |
| Checkpoint | Trạng thái lượt đang tạm dừng trong RAM; mất khi process restart. |
| Receipt | Kết quả thao tác đã commit, dùng để báo đúng kết quả và tránh chạy lại. |
| CAS | Cập nhật SQL có điều kiện; chỉ bên sửa được đúng một row thắng cuộc đua. |
| Lease | Quyền xử lý độc quyền theo user khi worker chạy; không giữ trong thời gian chờ duyệt. |
| Fail-closed | Thiếu quyền/trạng thái hợp lệ thì không thực thi, không đoán là đã được phép. |

## Quyết định không phải đoán lại

- Giữ `tools/base.py:execute` là gateway duy nhất; không tạo `agent/gateway.py` vì `laplace/agent.py` hiện là module khác.
- Giữ directive `/remind*` làm điều kiện đề xuất thao tác; **nút Xác nhận** mới cấp consent cho dữ liệu cụ thể. Không thêm bộ nhận diện ý định tự nhiên mới trong sprint này.
- Tối đa một lượt chờ duyệt/user; nhắn request mới được hướng dẫn duyệt hoặc `/cancel`, không ghi đè checkpoint.
- Approval chưa commit effect bị vô hiệu hóa sau restart. Reminder đã commit vẫn chạy scheduler hiện tại.
- Ngân sách USD là ước tính theo bảng giá; không hứa bằng hóa đơn provider. Không có dữ liệu giá/usage phải hiển thị rõ, không gán thành miễn phí.
- DB nghiệp vụ là private; log/báo cáo là bản lọc riêng. Không redact dữ liệu canonical theo cách làm đổi nội dung đã duyệt hoặc citation.

## Nguồn, lịch và phụ thuộc

| Nguồn | Quyết định |
|---|---|
| `docs/ĐỀ CƯƠNG ĐỒ ÁN 1_ LAPLACE AI AGENT.docx`, mục Sprint 5 | Phạm vi chính thức: risk/policy, approval, injection, budgets, privacy, security report. Lịch **05–18/10/2026**. |
| `docs/Ke hoach cong viec LAPLACE.xlsx`, S5-01…S5-07 | Ánh xạ task, 5 mức risk, TTL 15 phút, lỗi lặp ≥3. Spreadsheet ghi 12–25/10; giữ lịch nối Sprint 4 như plan trước, dịch mốc một tuần nếu lịch môn học được xác nhận thay đổi. |
| [Plan Sprint 4](../../docs/260912-2011-sprint4-core-tools/plan.md) | Upstream completed; không còn blocker. Các plan Sprint 1–3 trong `plans/` đều completed; không có plan unfinished cần dependency hai chiều. |

`branch: sprint05` là nhánh triển khai dự kiến từ baseline Sprint 4 đã nghiệm thu; phiên này không tạo/chuyển nhánh. Ước lượng cho một người: 60h + 12h dự phòng, không phải giờ đã thực hiện.

## Phases

| Phase | Name | Status |
|---|---|---|
| 1 | [Risk policy và một execution gateway](./phase-01-risk-policy-gateway.md) — 10h | Pending |
| 2 | [Approval bền và luồng Telegram pause/resume](./phase-02-telegram-approval.md) — 20h | Pending |
| 3 | [Ngân sách thực thi và dừng lỗi lặp](./phase-03-execution-budgets.md) — 12h | Pending |
| 4 | [Trust boundary và nhật ký an toàn](./phase-04-injection-privacy.md) — 10h | Pending |
| 5 | [Nghiệm thu an toàn xuyên tuyến](./phase-05-security-acceptance.md) — 8h | Pending |

Chạy **1 → 2 → 3 → 4 → 5**: cùng sửa `services/chat.py`, `tools/base.py`, `repo.py`, `config.py`, bot handlers. Một integration owner; không chia các file shared cho nhiều người sửa đồng thời. Phase 2 khóa pause/budget checkpoint trước Phase 3.

## Mốc bàn giao dự kiến

| Mốc | Gate |
|---|---|
| 05–06/10 | G1: tất cả production tools có risk explicit; unknown/destructive/owner mismatch bị chặn trước run. |
| 07–11/10 | G2: preview → approve/reject/15m expiry; double-click, cancel/forget và restart fail-closed. |
| 12–13/10 | G3: step/time/cost stop; retry + compaction cùng budget; không reset sau approval. |
| 14–15/10 | G4: web/document/summary không mint quyền; log và artifacts không lộ secret/PII. |
| 16–18/10 | G5: focused checks, shared-runtime smoke, Telegram thật, báo cáo/video; thời gian dự phòng dùng sửa lỗi, không thêm scope. |

## Definition of Done

- [ ] S5-01/02: risk matrix đủ mọi tool; một gateway enforce schema, actor, capability, policy và approval; không còn đường thực thi bypass.
- [ ] S5-03: create/update/cancel reminder phải duyệt snapshot; reject/expiry/replay/wrong owner/restart không tạo side effect; task card phân biệt waiting với completed.
- [ ] S5-04: ít nhất 5 case injection qua web/document và một case qua memory/compaction; bằng chứng DB/tool/delivery, không dùng wording refusal làm oracle.
- [ ] S5-05: giới hạn bước, thời gian active, chi phí model theo pricing contract; lỗi giống nhau lần 3 dừng; mọi request/retry/compaction/resume cùng ngân sách.
- [ ] S5-06: application logs/traces và public evidence được sanitize; canonical private evidence được phân quyền, runtime credentials không bị copy vào content; `/forget` xóa approval/checkpoint/uploads của đúng owner, giữ usage.
- [ ] S5-07: các gates và [ma trận nghiệm thu](./phase-05-security-acceptance.md) đạt, có evidence thật; full pytest/Ruff chạy cuối integration, README/config/risk matrix/security report cập nhật.

## Không thuộc Sprint 5

Không verifier, generic retry/replan hoặc resume task sau crash (Sprint 6); không evaluation framework/web viewer (Sprint 7); không OCR/RAG/browser/shell/email/calendar/new arbitrary-send tool. Restart **vô hiệu hóa approval chưa hoàn tất**, không tự replay. Giữ single-process và at-least-once reminder delivery; không hứa exactly-once hoặc chặn mọi prompt injection bằng delimiter. Approval không cho phép hành động vốn bị cấm.

## Handoff

Bắt đầu bằng Phase 1; chỉ đánh completed khi có evidence tương ứng. Lệnh triển khai:

`/ck:cook /Users/thanhnguyen/Documents/DA1/plans/261002-1046-sprint5-approval-safety/plan.md`

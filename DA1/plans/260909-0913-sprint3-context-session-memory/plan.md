---
title: "Sprint 3: Context Orchestration and Session Memory"
description: "Kế hoạch đã rà soát: context có ngân sách, task card đúng schema, compaction có watermark, xóa phiên an toàn và thực nghiệm không dựa vào đáp án mock."
status: completed
priority: P1
effort: "48h dự kiến + 8h dự phòng"
branch: sprint03
tags: [feature, backend, database, context, memory, sprint-3]
created: 2026-09-09
updated: 2026-09-16
blockedBy: []
blocks: [260912-2011-sprint4-core-tools]
---

# Sprint 3: Tổ chức context và bộ nhớ phiên

## Kết luận rà soát

**Sprint 3 đã hoàn tất:** implementation, cổng tự động, demo Telegram M-14 và full live evaluation schema v2 đều đạt. Run `20260916T145221Z` hoàn tất 24/24 case-strategy bằng `router9/gpt-5.6-sol`, `incomplete=false`; trạng thái M-14 vẫn dựa trên nghiệm thu thủ công do chủ dự án xác nhận.

| Mức | Vấn đề đã đối chiếu | Điều chỉnh |
|---|---|---|
| P0 | Phase 2 dùng `Task.title`, `Step.index/action_json/observation_json` không có trong `models.py:62-90` | Dùng field thật; JSON trong `Step.detail`; thêm duy nhất liên kết request cho task |
| P0 | Budget chỉ dựng đầu lượt, trong khi `chat.py:83-101,232-238` tiếp tục thêm retry/tool messages | Dựng lại trước **mọi** agent call; chốt cách xử lý phần bắt buộc quá lớn |
| P0 | Mock scripted không đọc context, trả 10 prompt tokens cố định (`llm/mock.py:46-55`) | Tách kiểm tra cơ chế offline khỏi so sánh chất lượng/usage bằng model thật |
| P0 | `/forget` dựa vào cascade không tồn tại; `ToolCall.task_id` hiện không được gắn; `LLMCall.task_id` còn tham chiếu task | Ownership bắt buộc cho bản ghi mới; xóa child-first; giữ usage bằng detach FK; công khai giới hạn dữ liệu cũ |
| P0 | `/cancel` chỉ hủy coroutine chờ, worker vẫn chạy (`bot/handlers.py:176-227`) | Guard theo user ở service; từ chối forget khi worker chưa thực sự kết thúc |
| P1 | Compaction kích hoạt sau khi đã trim; watermark và phần mới/cũ mơ hồ | Kiểm tra trước trim; fold `(watermark, cutoff]`; cập nhật summary+watermark nguyên tử |
| P1 | Fallback bỏ user facts; “giữ 4 lượt” mâu thuẫn hard budget; summary được nâng thành system | Giữ bằng chứng cả user/tool; 4 exchanges là mục tiêu mềm; memory là dữ liệu không tin cậy |
| P1 | Branch/push prerequisite cũ không khớp local refs | HEAD hiện `sprint02`; local và cached `origin/sprint02` cùng `cc9568936a7b1d09a4a4545e923eef5558da8343` |

Probe offline khi review: input 1 và 20.000 ký tự đều nhận cùng final, `prompt_tokens=10`, `cost_usd=0`; ORM xác nhận không có các cột plan cũ giả định. Không gọi API, không đọc `.env` hay DB người dùng. Cached remote ref không chứng minh trạng thái remote trực tiếp.

## Phạm vi và nguồn

- Code root: `Laplace-Demon/`; không port kiến trúc từ `Laplace-Demon-Beta/`.
- Nguồn yêu cầu: `docs/ĐỀ CƯƠNG ĐỒ ÁN 1_ LAPLACE AI AGENT.docx`, bảng Sprint 3, 07–20/09/2026.
- Baseline: [Báo cáo Sprint 2](../reports/260906-sprint-02-bao-cao-tien-do.md). Báo cáo còn ghi demo model thật chưa hoàn tất; không tự đánh dấu hoàn tất.
- Hai plan cũ trong `plans/` đều completed; không có upstream plan chặn Sprint 3. Sprint 4 đã có [plan private](../../docs/260912-2011-sprint4-core-tools/plan.md) và khai báo Sprint 3 là blocker; chỉ bắt đầu cutover shared runtime sau khi full live schema v2 hoàn tất.
- Giữ chữ ký `handle_message`, `ChatReply`, protocol LLM, tool registry và chính sách retry/giới hạn 5 tool của Sprint 2. Thêm metadata task tùy chọn vào `AgentAction`, không tạo planner loop riêng.
- Không làm RAG/vector DB, web/reminder tools, Approval Gate, verifier kết quả, web UI hay phục hồi task sau process crash. Không thêm runtime dependency. Chỉnh cancellation/transaction chỉ để bảo đảm task terminal và forget không bị ghi lại.

## Phases và ánh xạ yêu cầu

| Phase | Nội dung | Status | Phụ thuộc | Ước lượng |
|---|---|---|---|---|
| 1 | [Context ba tầng và observation có giới hạn](./phase-01-context-builder.md) | Completed | Baseline code hiện có | 10h |
| 2 | [Task card, ownership và vòng đời lượt xử lý](./phase-02-task-card.md) | Completed | Phase 1 | 12h |
| 3 | [Compaction và lệnh xem/xóa bộ nhớ](./phase-03-compaction-session-memory.md) | Completed — M-14 passed | Phase 1 + 2 | 16h |
| 4 | [Thực nghiệm so sánh và bằng chứng bàn giao](./phase-04-experiment-report.md) | Completed — E-12 passed | Phase 1–3 | 10h |

Phase 1 đáp ứng context ba tầng + giới hạn kết quả tool; Phase 2 đáp ứng thẻ nhiệm vụ; Phase 3 đáp ứng compaction + memory theo user; Phase 4 đáp ứng thực nghiệm. Triển khai tuần tự 1 → 2 → 3 → 4 vì cùng sửa `chat.py`, `context.py`, `repo.py`. Một người sở hữu integration; không chia hai agent sửa các file này đồng thời.

## Lịch thực hiện và cổng bàn giao

Ước lượng là kế hoạch, không phải giờ đã thực hiện. Giả định một người dành được khoảng 4–6h/ngày; xác nhận năng lực thực tế khi nhận triển khai, không tự cắt deliverable nếu thiếu thời gian.

| Mốc dự kiến | Đầu ra phải quan sát được |
|---|---|
| 09–10/09 | G1: mọi agent request tuân thủ char budget; full tool payload được lưu; chưa mất cặp action/observation |
| 11–13/09 | G2: tool task có ownership và terminal state; card cập nhật qua callback; hủy lượt không giải phóng worker sớm |
| 14–16/09 | G3a: migration không mất dữ liệu; compaction lặp lại không nhân đôi facts; lỗi summary không làm hỏng watermark |
| 17–18/09 | G3b: memory/forget chạy end-to-end, hai user không lẫn; worker race và FK đã được kiểm tra |
| 19/09 | G4: chạy A/B/C cùng bộ nhiệm vụ, lưu raw metrics và phân tích cả trường hợp C kém hơn |
| 20/09 | Demo Telegram, hoàn thiện báo cáo; 8h dự phòng dành cho lỗi integration/model quota, không thêm tính năng |

## Các quyết định xuyên phase

1. Budget mặc định 12.000 **ký tự nội dung messages do ứng dụng dựng**, không quy đổi cố định thành tokens. Schema text do adapter thêm nằm ngoài budget này và phải đo riêng trong thực nghiệm. Phần bắt buộc không vừa → dừng trước provider, không gửi request quá budget.
2. Lớp 1 chỉ chứa policy/tool schema ổn định. Lớp 2 chứa card + summary dưới nhãn dữ liệu, không thêm system message chứa raw user/tool text. Lớp 3 giữ exchanges và action/observation theo nhóm nguyên vẹn.
3. DB là bản đầy đủ; context là view hữu hạn. Marker DB ID phục vụ truy vết, **không** giả vờ LLM có công cụ đọc DB. Không hứa giữ mọi fact trong bộ nhớ có giới hạn.
4. Một task lưu bền cho một turn có tool; turn chat thuần không tạo task. `completed` chỉ nghĩa model đã final, chưa phải kết quả được verifier xác nhận.
5. Compaction dùng incremental watermark và previous summary, bảo tồn quyết định/lý do/kết quả/nguồn; cơ chế offline không được dùng làm bằng chứng chất lượng LLM.
6. Forget xóa nội dung phiên có ownership trong DB, giữ identity/usage; không đồng nghĩa xóa file upload, lịch sử Telegram, log của provider hay backup. Memory commands chỉ trả dữ liệu trong private chat.

## Definition of Done

- [x] G1–G3 có proof end-to-end với DB tạm; regression tests chỉ cho budget boundaries, state transitions, mất fact, ownership và deletion race.
- [x] Schema mới nâng cấp được DB Sprint 2 và chạy migration lần hai không đổi dữ liệu; rollback dựa trên backup, không mặc định xóa DB.
- [x] Memory xem được hội thoại gần nhất, summary, task và tool previews; forget không xóa dữ liệu user khác và không đổi tổng usage.
- [x] Demo Telegram thật: hội thoại dài → nhớ fact → tool card → memory → forget → phiên mới không còn fact; `/status` vẫn giữ usage. Chủ dự án xác nhận M-14 đạt.
- [x] Thực nghiệm offline tái lập phần deterministic; live A/B/C schema v2 có raw output và token provenance. Run `20260916T145221Z`: full 8/8, window10 4/8, sprint3 7/8; sprint3 thua E-05 đúng trade-off excerpt đã định trước, không có unsupported claim.
- [x] Cuối integration chạy một lần `.venv/bin/python -m pytest` và `.venv/bin/ruff check .`; không dùng số test cũ 39 làm tiêu chí.
- [x] README, `.env.example`, hướng dẫn Sprint 3 và báo cáo tiến độ được cập nhật theo hành vi đã chạy; không commit secret, hội thoại thật hoặc đường dẫn cá nhân trong artifacts thực nghiệm.

## Handoff

Implementation hiện ở branch `sprint03`. Cổng code tự động, artifact offline schema v2, demo Telegram M-14 và full live schema v2 đều đạt. Artifact live cuối là `Laplace-Demon/experiments/results/20260916T145221Z/results.json`: 24/24 completed, một model thực trả `gpt-5.6-sol`, không chạm giới hạn và `incomplete=false`. Sprint 4 không còn bị chặn bởi acceptance Sprint 3.

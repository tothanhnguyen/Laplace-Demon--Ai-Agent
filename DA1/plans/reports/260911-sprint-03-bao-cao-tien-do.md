# Báo Cáo Kiểm Tra Lại Sprint 3 (16/09/2026)

**Mục tiêu sprint (theo đề cương):** Context ba tầng có ngân sách, task card theo bước, rolling session memory và lệnh xem/xóa bộ nhớ theo user, kèm thực nghiệm so sánh policy context.
**Sản phẩm bàn giao:** Runtime chat chia sẻ cho 3 policy context (full / window10 / sprint3), task/tool gắn ownership theo user, compaction watermark nguyên tử, `/memory` + `/forget` private-only, và bộ thực nghiệm offline/live có provenance chi phí.

Báo cáo được cập nhật theo kết quả nghiệm thu thủ công và artifact thực nghiệm cuối. **Kết luận: code, cổng tự động, demo Telegram M-14 và full live evaluation schema v2 đều đã đạt; Sprint 3 hoàn tất.**

## 1. Việc đã hoàn thành theo từng hạng mục

### S3-01/02 — Context ba tầng có ngân sách ✅
- `laplace/context.py`: dựng messages trước **mỗi** lời gọi agent — system policy, trạng thái phiên (task card + rolling summary trong nhãn dữ liệu không tin cậy), history gần nhất theo nhóm action/observation, request hiện tại.
- Ngân sách `LAPLACE_CONTEXT_MAX_CHARS` (mặc định 12.000 ký tự) đo **nội dung ứng dụng dựng**, tách khỏi schema instruction của adapter (`schema_instruction_chars` được runner đo riêng).
- System policy, request hiện tại và causal unit mới nhất không bị cắt rời; vượt ngân sách phần bắt buộc → service dừng **trước** khi gọi model.
- Tool result lưu **full payload** vào `tool_calls.result_json`; model chỉ nhận excerpt JSON ≤1.200 ký tự kèm `tool_call_id`.

### S3-03/04 — Shared runtime và kiểm chứng ✅
- `handle_message` giữ chữ ký cũ; thân chạy qua `_run_reserved_message(lease, ...)` nhận `provider`, `context_strategy` (`full|window10|sprint3`), `context_observer` (snapshot messages + purpose + char_count + dropped_group_count).
- Observer chỉ chạy khi caller truyền callback; production không log nội dung.
- Tests: budget đúng/overflow 1 ký tự, snapshot bất biến, drop cả cặp action/observation, escape tool observation (tests/test_context.py).

### S3-05/06/07/08 — Task card và vòng đời ✅
- Task chỉ được tạo khi model yêu cầu tool đầu tiên; `request_message_id` neo task vào đúng request; Step/ToolCall/Trace đều có `task_id`.
- Task card hiển thị goal, completion criteria, decision hiện tại, số tool đã chạy, thành công/thất bại, phần còn lại, trạng thái terminal. `completed` chỉ khi model trả final.
- Reservation process-local theo Telegram user (`try_reserve_turn`); `/cancel` cooperative qua Event, checkpoint trước mỗi provider/tool call; forget bị từ chối khi worker còn sống (test threading thật).

### S3-09/10 — Migration và compaction ✅
- Migration additive idempotent: `tasks.request_message_id`, `conversations.summary`, `conversations.summary_until_message_id`; `PRAGMA foreign_keys=ON` mỗi connection; test migration từ schema Sprint 2 thật (file SQLite cũ).
- Compaction chỉ chạy khi raw history vượt budget: giữ tối đa 4 exchange gần nhất, compact oldest-first theo tối đa 2 page hữu hạn mỗi lượt; summary + watermark cập nhật **một statement** compare-and-update.
- Query compaction chỉ lấy cột cần thiết và excerpt head/tail hữu hạn; không materialize toàn bộ tool payload/backlog. Nếu còn gap sau 2 page, layer 2 ghi rõ `UNSUMMARIZED_HISTORY_BACKLOG` thay vì âm thầm che dữ liệu chưa compact.
- Fallback extractive deterministic cho mock/lỗi provider; summary thật bị ràng buộc source IDs. Lỗi ghi usage sau một compaction call thành công được propagate trước khi watermark tiến.

### S3-11/12 — Lệnh memory/forget, kiểm chứng race và M-14 ✅
- `/memory` private-only: thống kê, summary + recent exchanges, task status/remaining, tool source/result previews, usage. `/forget` cảnh báo phạm vi; `/forget confirm` xóa child-first trong một transaction và giữ `users` + `llm_calls`.
- Wipe từ chối Trace có task/conversation ownership chéo và rollback toàn transaction. JSON legacy đúng cú pháp nhưng sai top-level type không làm vỡ `/memory`.
- Legacy tool logs Sprint 2 (`task_id=NULL`) được giữ và trả lời **nói rõ** không xóa được (`legacy_unowned_tool_calls`).
- Regression bao phủ dispatch/privacy, busy/empty, legacy suffix, cross-owner trace, malformed JSON, usage retention và FK.
- ✅ Demo Telegram thủ công M-14 đã đạt theo xác nhận của chủ dự án: nhớ fact sau hội thoại dài, gọi tool, xem memory, chỉ xóa sau confirm, không còn fact sau xóa và `/status` vẫn giữ usage.

### S3-13/14 — Bộ nhiệm vụ và runner offline ✅
- 8 case synthetic cố định (E-01…E-08): nhớ fact dài, cập nhật quyết định/lý do, hai nguồn, tool head/tail/middle, tool cũ + recover, quyết định + nguồn; forbidden items để chặn unsupported claim.
- Runner `scripts/eval_context.py` chạy **cùng chat runtime** cho cả 3 policy; mỗi case DB riêng, cwd fixture riêng, không đụng DB production.
- Offline schema v2 repeat 2 (run `20260912T105242Z`): 48/48 case-run, `incomplete=false`; hai lượt **giống hệt** sau khi loại `elapsed_ms`/`repeat`. Manifest thêm `source_sha256` để nhận diện chính xác code dirty-tree.
- Scorer chuẩn hóa case/whitespace, từ chối phủ định và đảo attribution nguồn↔fact. Offline: full 16/16, window10 4/16, sprint3 14/16; sprint3 vẫn thua E-05 do excerpt mất fact giữa file.

### S3-15 — Thực nghiệm live paired A/B/C ✅
- Full live schema v2 run `20260916T145221Z` dùng `router9/gpt-5.6-sol`: 24/24 case-strategy completed, `incomplete=false`, không retry và không chạm giới hạn 600 calls.
- Kết quả: full 8/8 với 255.950 tokens; window10 4/8 với 138.819 tokens; sprint3 7/8 với 175.878 tokens; cả ba có 0 unsupported claim.
- Sprint3 dùng ít hơn full 80.072 tokens (31,3%) và giữ thêm 3 case so với window10, nhưng dùng nhiều hơn window10 37.059 tokens. Không kết luận một strategy thắng mọi metric.
- Sprint3 thua E-05 vì bounded head/tail excerpt không chứa fact ở giữa file, đúng trade-off rubric đã khóa. `cost_usd=0` biểu diễn chi phí biên của route subscription local; không quy đổi phí thuê bao hoặc quota.
- Các artifact schema v1 và run Gemini dừng do quota vẫn được giữ để audit nhưng không dùng làm bằng chứng nghiệm thu cuối.

### S3-16 — Tài liệu và nghiệm thu tự động ✅
- README, `.env.example`, hướng dẫn Sprint 3 và plan/phase metadata đã được đồng bộ theo hành vi đã chạy.
- Validation cuối ngày 12/09: `ruff check .`, `pytest`, `git diff --check`; kết quả ở mục 2.

## 2. Bằng chứng kiểm tra

| Kiểm tra | Lệnh | Kết quả |
| --- | --- | --- |
| Lint | `.venv/bin/ruff check .` | All checks passed |
| Toàn bộ test | `.venv/bin/python -m pytest -q` | **77 passed** |
| Whitespace | `git diff --check` | Passed |
| Offline eval v2 | `eval_context.py --mode offline --repeat 2` | 48/48 completed, incomplete=false, deterministic subset identical; run `20260912T105242Z` |
| CLI chat | `LAPLACE_LLM_PROVIDER=mock LAPLACE_DATABASE_URL=sqlite:///:memory: python -m laplace --chat "..."` | `[mock] xong`; 1 LLM call |
| Adversarial review | Review fix-diff + regression >2-page backlog | PASS; không còn finding code Important |
| Live eval v2 | `eval_context.py --mode live --provider router9` | 24/24 completed, incomplete=false; full 8/8, window10 4/8, sprint3 7/8; run `20260916T145221Z` |
| Manifest | `results.json` schema v2 | có `source_sha256`, `fixture_sha256`, terminal completion accounting và provenance |

## 3. Quyết định kỹ thuật đáng chú ý

1. **Usage và completion không được làm đẹp:** lỗi DB ghi usage sau paid compaction abort trước watermark; case exception đọc lại usage committed; mọi terminal không-completed làm run `incomplete=true`.
2. **Pacing tầng runner, không đụng runtime:** `--pacing-seconds` điều tiết logical calls; mọi failed logical call vẫn tiêu thụ `max_calls`.
3. **Scorer chấm quan hệ:** ngoài sentinel/forbidden, E-03 yêu cầu nguồn và fact nằm trong cùng clause; phủ định sentinel không được tính pass.
4. **Compaction hữu hạn và minh bạch:** 2 page/lượt, projection bounded, gap còn lại có marker explicit; summary/watermark chỉ tiến sau persistence thành công.
5. **Provenance dirty-tree:** `source_sha256` hash toàn bộ Python runtime/evaluator/fixture, không chỉ ghi HEAD + cờ dirty.

## 4. Giới hạn đã biết

- n=8, repeat=1 ở live vẫn chỉ cho kết luận exploratory; chưa có ý nghĩa thống kê.
- sprint3 mất fact ở E-05 do excerpt deterministic; đây là trade-off quan sát được, không bị sửa rubric để C thắng.
- Summary hữu hạn và giới hạn 2 page/lượt có thể để backlog tạm thời; runtime báo marker, lượt sau tiếp tục compact.
- Artifact live schema v1 được giữ để audit nhưng đã superseded bởi full live schema v2 run `20260916T145221Z`.

## 5. Trạng thái bàn giao

Sprint 3 đủ bằng chứng nghiệm thu. Artifact cuối: `Laplace-Demon/experiments/results/20260916T145221Z/results.json`. Commit/push branch `sprint03` là thao tác phát hành riêng, chưa được thực hiện trong bước cập nhật báo cáo này.

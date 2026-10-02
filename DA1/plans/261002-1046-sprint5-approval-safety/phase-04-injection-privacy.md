---
title: "Phase 4: Trust boundary và nhật ký an toàn"
status: pending
priority: P0
effort: "10h"
dependsOn: [phase-03-execution-budgets]
---

# Phase 4: Trust boundary và nhật ký an toàn

## Context và outcome

[Plan tổng](./plan.md) · [Policy](./phase-01-risk-policy-gateway.md) · [Acceptance](./phase-05-security-acceptance.md). S5-04/S5-06. Không verifier, không security platform hay regex classifier quyết định quyền.

Reuse `context.py:87-101,168-223,279-442`: session state và tools ở lower-trust JSON envelopes, bounded sources/locators; `prompts.py:24-43,87-100`: priority/citation rules và trusted runtime facts. Gap: envelopes không enforce quyền, summary có thể mang injection qua turn sau, model/validation retry text chưa có framing nhất quán. `bot/handlers.py`/reminder service có `exc_info`; `models.py`/repo lưu raw canonical messages/tool JSON khác với metrics-only LLMCall. `scripts/eval_context.py` xuất answer/error và DB từng case, cần public projection riêng.

## Trust invariant và implementation steps

1. **Authority cố định:** tool risk/owner/transport/attachment capability từ registry + runtime; approval chỉ từ owner callback đúng generation. Không đọc `approved=true`, command, role tag hay fake runtime facts trong tool/web/document/history để cấp quyền.
2. **Data-only mọi tầng:** giữ untrusted tool/session JSON envelope và user role; summary, excerpts, external error text, retry output và document metadata không nâng thành system/developer instruction. JSON escaping làm delimiter/role marker giả không phá wrapper. Không strip toàn tài liệu hoặc bỏ nguồn để né injection.
3. **Compaction không rửa nguồn:** compact từ untrusted data; summary output vẫn lower-trust với provenance, không trở thành policy hoặc direct-user intent. Current request directive khác directive được quoted/nhắc trong web/doc/history. Giữ nguồn canonical DB, char budget và task ownership Sprint 3–4.
4. **Runtime facts chỉ dữ kiện:** không trộn raw data vào trusted system facts; facts có owner-approved status/IDs do code tạo, không do model claim. Nếu bounded observation cắt nguồn quá dài, đánh truncation, không biến URL bị cắt thành URL canonical/citation mới.
5. **Least privilege giữ nguyên:** opaque document IDs chỉ owner/turn-checkpoint managed uploads; web chỉ public query/URL qua Tavily; reminder recipient lấy owner. Không tool đọc `.env`, source tree, filesystem tùy ý, send destination hoặc shell. Destructive vẫn deny sau approve.
6. **Oracle hành vi:** ít nhất I01–I05 web/PDF/DOCX/TXT/source-page injection + I06 summary ở phase acceptance; forced malicious model output phải bị gateway chặn ngay cả khi prompt defense thất bại. READ legitimate không bị blanket deny chỉ vì dữ liệu chứa từ “system”.

## Privacy policy: tách ba surface

| Surface | Dữ liệu được giữ/hiển thị | Control |
|---|---|---|
| Canonical private storage + owner `/memory`/preview | User message, document excerpt, tool args/results, approval params/receipts cần cho nghiệp vụ và citation | Owner-scoped, không public-export DB, explicit retention/delete; không copy runtime credentials vào request/tool/trace. |
| Operational/security log + `Trace` audit projection | Allowlist event/code, opaque task/step/approval ID, risk/status, counters, latency, pricing mode và stop reason | Không raw prompt/body/params/results/reminder text/email/phone/Telegram ID/username/local path; safe code thay raw exception. |
| Public report/eval projection | Synthetic/public evidence, expected/observed status, opaque IDs/hashes, safe locator và nguồn public không nhạy cảm | Redact/omit email/phone/token/path/secret URL query; không public per-case raw DB/user file. |

Không global mutate `messages.content`, `ToolCall.result_json`, approval params hoặc canonical `sources` chỉ để làm report sạch: sẽ đổi effect/citation/provenance. Secret người dùng đưa trong tài liệu có thể tồn tại ở canonical private memory theo disclosure; **không tuyên bố SQLite hoàn toàn không có PII/secret**. Runtime API keys/bearer/bot token phải không bị copy từ Settings/headers/upstream exception vào persisted content. Dữ liệu private không đồng nghĩa encrypted-at-rest; quyền file/deployment hiện có là giới hạn cần ghi.

### Logs và errors

- Create một module nhỏ `laplace/logging.py`: setup formatter/filter + safe event serializer, không framework mới. Áp vào entrypoint trước bot/provider startup, gồm dependency logging khi DEBUG. Allowlist là chính, redaction regex defense-in-depth.
- Scrub configured secret values, bearer/API/bot token patterns, email/phone/local path trong formatted output **và exception text**; không chỉ `record.msg` trước `%` formatting. Không log secret values khi dựng filter; không đọc `.env` vào report.
- Thay `logger.exception(...telegram_user_id...)`/`exc_info=True` trên vận hành thông thường bằng safe error class/code + opaque task ID. Dùng existing safe fixed ToolFailure pattern của web service; validator error không echo raw input/ctx chứa secret.
- Trace persistence dùng typed/allowlisted projection trước `repo.record_trace`, không copy tool payload/answer preview raw vào generic JSON. Structured ToolCall full evidence vẫn private riêng, error field chỉ safe failure text.
- `/status` và budget metadata không chứa prompt/body. Không thêm log prompt để debug approval. Không sanitize private owner preview đến mức người dùng không thấy message thật họ sắp duyệt.

### Export và retention

- `scripts/eval_context.py`: preserve private canonical experiment artifact khi cần reproducibility; public report projection được sanitize, failure error dùng stable code. Per-case raw SQLite chỉ local private/ignored, không gọi nó là sanitized export. Chỉ synthetic fixtures vào repo; không sửa content của historical accepted evidence hoặc commit raw user runs.
- Source URL có token/email/query nhạy cảm: omit/pseudonymize trong **export**, kèm marker/hash/mapping local; không thay URL canonical rồi cho model trích dẫn một URL giả. Document locator không chứa filesystem path/user name.
- `/forget confirm`: xóa owned canonical approvals/checkpoints/uploads cùng task/tool/history theo child-first FK policy Phase 2; giữ user identity/usage, không xóa user khác. Warning nói rõ uploads pending được giữ tới completion/TTL và bị xóa khi forget; lịch sử Telegram/provider/backups/legacy unowned logs không thuộc deletion guarantee.
- Reject/expiry/cancel/completion/shutdown đều cleanup managed pending files, không kéo dài giữ uploads vô hạn. Không thêm TTL data retention platform cho toàn DB ngoài scope.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/context.py`, `laplace/prompts.py`, `laplace/services/chat.py` | Consistent untrusted history/summary/retry framing và allowlisted trace events. |
| Modify | `laplace/tools/base.py`, `laplace/repo.py` | Safe validation/errors, audit projection, giữ canonical owner evidence/source identity. |
| Create | `laplace/logging.py` | Logging setup, safe-event serializer/redaction filter; không side-effect logging config lúc import. |
| Modify | `laplace/__main__.py`, `laplace/bot/handlers.py`, `laplace/services/reminders.py`, `laplace/llm/openai_provider.py` | Setup sớm, safe exception/model-name logging, không raw provider bodies/user IDs. |
| Modify | `scripts/eval_context.py`, `tests/test_eval_context.py` | Sanitize public projection và exception evidence, giữ private replay data không public. |
| Modify | `tests/test_context.py`, `tests/test_session_memory.py`, `tests/test_tool_registry.py`, `tests/test_chat_service.py`, `tests/test_approvals.py` | Injection/compaction/owner/privacy regression behavior. |
| Create | `tests/test_safe_logging.py` | Formatted args/exception/dependency log canary leaks, structured field survival. |
| Modify | `README.md`, `.env.example`, `docs/huong-dan-nguyen-ly-sprint-3.md` | Trust/retention/pricing/compaction updates cần thiết, không tham chiếu private plan. |

## Thứ tự code

| ID | Việc cần làm | Kết quả kiểm tra được |
|---|---|---|
| P4-01 | Lập danh sách nơi nhận untrusted text: tool, summary, history, retry, validation error. Dùng lại envelopes hiện có. | Không có raw external text được nâng thành trusted system facts. |
| P4-02 | Viết I01–I06 trên gateway/shared runtime, giữ legitimate READ làm control case. | Forced malicious model action vẫn không đọc file ngoài capability hoặc mutate không duyệt. |
| P4-03 | Tạo safe log serializer/formatter, gắn trước startup; thay raw exception logging ở các file đã liệt kê. | Canary không có trong final formatted logs kể cả exception/dependency DEBUG. |
| P4-04 | Tách allowlisted Trace và public export khỏi private canonical data; cập nhật deletion/retention disclosure. | Log sạch nhưng preview/citation/canonical payload không bị sửa. |
| P4-05 | Chạy focused checks + smoke, cập nhật README và gate G4. | Có evidence cho cả “không rò log” và “không mất dữ liệu nghiệp vụ”. |

**Contract log:** một event chỉ có `event`, internal `task_id/step_index/approval_id`, `tool_name`, `risk`, `status`, stable `error_code`, counters/latency và pricing status. ID là ID nội bộ, không phải Telegram identity; event có thể bỏ field không liên quan. Unknown field không được tự serialize. Không yêu cầu redactor nhận diện mọi PII tự do; payload tự do vốn không được đưa vào operational log.

## Checklist và gate G4

- [ ] External/summary/retry payload không mint identity/capability/approval; crafted delimiters không đổi source authority.
- [ ] I01–I05 + I06 có behavioral assertions: no forbidden read/write/send, owner scope và valid READ còn hoạt động.
- [ ] Canary runtime credentials/email/phone không có trong app/dependency logs, trace projection và public export; formatted exception path cũng được kiểm.
- [ ] Canonical sources/locators/approved message giữ nguyên; private preview đủ material; redacted URL không được biến thành citation.
- [ ] Two-user forget/approval/files isolation, usage preserved và disclosure đúng upload retention mới.
- [ ] Shared-runtime smoke đọc malicious synthetic document, model cố mutation → gateway deny/pause đúng; actual no unauthorized row/delivery. Không coi smoke fake model là live robustness proof.

## Rủi ro và handoff

P0: coi delimiter/prompt là security boundary → gateway/CAS vẫn phải enforce dù model làm theo injection. P0: chỉ scrub console mà Trace/report lưu raw → audit từng serialization surface đã nêu. P1: redact canonical evidence làm sai citation/approval → projection riêng. Phase 5 ghi giới hạn và cả case fail, không tuyên bố regex hoặc hữu hạn tests chặn mọi dữ liệu cá nhân/injection.

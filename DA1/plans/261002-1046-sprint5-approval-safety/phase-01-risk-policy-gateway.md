---
title: "Phase 1: Risk policy và execution gateway"
status: pending
priority: P0
effort: "10h"
dependsOn: []
---

# Phase 1: Risk policy và một execution gateway

## Context và mục tiêu

[Plan tổng](./plan.md) · [Approval](./phase-02-telegram-approval.md). S5-01/S5-02. Code paths dưới đây tương đối với `Laplace-Demon/`.

Baseline đã đọc: `tools/base.py:16,40-79,130-151,248-305` chỉ validate input/output; `services/chat.py:147-155,1138-1156` tạo capability từ directive rồi gọi executor; `tools/reminders.py:59-67` bảo vệ mutation bằng capability tên tool. Giữ các guard owner/transport hiện có, không coi metadata model-visible là policy.

## Contract chốt

### Risk matrix

Thay `Literal[low, medium, high]` bằng enum string `READ`, `DRAFT`, `WRITE_REVERSIBLE`, `EXTERNAL_SEND`, `DESTRUCTIVE`; migrate mọi decorator, schema, test fixture và consumer. Không alias mức cũ. Registry từ chối risk thiếu/không hợp lệ khi startup.

| Action | Risk | Policy |
|---|---|---|
| `web_search`, `fetch_page` | READ | Auto khi schema/public URL/config hợp lệ. Chỉ gửi query/URL cần cho thao tác web; không tự đính kèm tài liệu, memory hay key. |
| `read_document` | READ | Auto với opaque attachment capability của turn; không cấp quyền path hoặc upload user khác. |
| `create_reminder`, `update_reminder` | EXTERNAL_SEND | Phải duyệt vì cấp/đổi quyền gửi Telegram tương lai; destination cố định là private owner. |
| `cancel_reminder` | WRITE_REVERSIBLE | Phải duyệt; chỉ đổi lifecycle row của owner, không xóa dữ liệu hay cancel terminal row. Không thêm chức năng undo. |
| DRAFT | DRAFT | Auto nếu chỉ tạo nội dung trong response; hiện không có production tool mức này, không thêm tool để đủ enum. |
| Agent yêu cầu xóa DB/file, shell, gửi user khác | DESTRUCTIVE hoặc unsupported | Deny; approval không override. Không tạo công cụ xóa/gửi thử trong production. |
| `/forget confirm` | Ngoài tool registry: destructive user command | Giữ hai bước xác nhận trusted handler; model không gọi được. Phase 2 gắn cleanup approval/checkpoint. |
| Dispatcher reminder đã được cho phép | EXTERNAL_SEND có durable authorization | Chỉ gửi payload đã lưu từ thao tác được duyệt; không gọi model và không xin duyệt lại mỗi lần đến hạn/retry. |

Conversational reply, task card, preview và thông báo lỗi tới chính requester là phản hồi transport của request, không phải arbitrary-send tool. READ qua Tavily vẫn có disclosure query/URL; risk matrix phải nói rõ, không tuyên bố mọi network I/O đều không truyền dữ liệu.

### Gateway duy nhất

Đặt policy ngay trong `tools/base.py:execute` hiện có; **không thêm executor song song hoặc package `agent/gateway.py`** (runtime hiện là `services/chat.py`, `laplace/agent.py` là sample domain khác).

Thứ tự: resolve registered spec → validate args → kiểm trusted actor/transport/capability và resource ownership → policy → yêu cầu approval nếu cần → revalidate consent + current resource → run → validate output/canonical persist. Ownership/unsupported/destructive phải bị chặn trước preview, không tạo approval để xin vượt quyền.

- Phase 1 định nghĩa `ApprovalRequired` và nhánh consume trong chat ngay cùng gateway: READ/DRAFT trả `ToolResult`; mutation đủ điều kiện đề xuất trả `ApprovalRequired`, không phải `ok=true`, không ghi tool success/side effect. Trước khi Phase 2 có UI, chat dừng an toàn với lý do `approval_unavailable`; không giả đã pause/đã tạo reminder. Đây là mốc integration nội bộ, chưa được deploy.
- Một authorization chỉ cho **tool + normalized immutable params + user/task/step + approval generation**; không cấp `frozenset` tên tool làm quyền tái sử dụng. Model params không có approval token/risk/user/chat/path.
- Giữ `_authorized_effects` từ directive hiện tại làm **eligibility** (được đề xuất đúng loại thao tác), không còn là quyền mutate. Message không có directive tương ứng → `forbidden` và hướng dẫn lệnh, không tự sinh proposal từ ý định model suy đoán. `/remind*` trong history/tool/summary không được dùng thay current request.
- Nếu CLI/group không có private approval transport: trả structured forbidden trước mutation; không console auto-approve hoặc test fallback trong production.
- `spec.run` chỉ dùng trong gateway; reminder tools vẫn recheck owner/resource khi mutate. DB transaction mutation ở Phase 2 sẽ atomic consume consent cùng write.
- Giữ canonical failure codes hiện có; thêm control outcomes/stop reasons có chủ đích, không mở retry taxonomy Sprint 6.
- Runtime-forced web/document/effect branches và cached effect receipts đều tuân thủ policy/budgets; cache keyed approved action, không chỉ `tool_name`.

**Preparation hook tối thiểu:** thêm `ToolSpec.prepare` optional, nhận validated args + trusted context, trả prepared proposal và preview facts, **không write**. Ba reminder tools dùng hook này; READ/DRAFT không cần hook, DESTRUCTIVE luôn deny. Risk cần approval nhưng thiếu hook → registration error. Gateway gọi hook trước trả `ApprovalRequired`; normalization/owner lookup nằm trong `tools/reminders.py`, không hardcode nghiệp vụ reminder trong `base.py`.

Khi resume có `approval_id` trusted, gateway đối chiếu tool/user/task/step và public-args fingerprint với stored proposal, không gọi prepare lại (vì sẽ đổi relative time). Reminder `run` nhận consent binding từ trusted context và load prepared payload trong transaction T3. Direct `run` thiếu binding vẫn forbidden. Không thêm cờ `approved=True` công khai hoặc helper auto-approve cho tests.

## File map và bước triển khai

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/tools/base.py` | Risk enum, strict risk registration, gateway decision và trusted action-bound authorization contract. |
| Modify | `laplace/tools/web.py`, `laplace/tools/documents.py`, `laplace/tools/reminders.py` | Explicit risk, giữ capability/resource guards; bỏ directive-as-consent message. |
| Modify | `laplace/services/chat.py`, `laplace/prompts.py` | Consume gateway outcome; task chưa thành công khi chờ duyệt; spec/prompt theo risk mới. |
| Modify | `tests/test_tool_registry.py`, `tests/test_document_tools.py`, `tests/test_reminders.py`, `tests/test_chat_service.py` | Migrate callers, behavior assertions; fixture không tự bypass approval. |
| Create | `docs/risk-matrix.md` | Matrix production tools, transport commands và scheduler; quyền tối thiểu, auto/approve/deny. |

1. Trước đổi exported contracts: dùng language-server references cho `RiskLevel`, `ToolContext`, `ToolSpec`, `execute`; rà import/spec fixture đã thấy trong tools/tests. Không bỏ caller eval/sample.
2. Migrate risk và policy; giữ signature/convention khi không bắt buộc đổi. Tách permission denial khỏi pause control outcome.
3. Khóa action normalization và approval snapshot contract với Phase 2; outcome không execute rồi mới hỏi.
4. Focused behavioral checks + throwaway shared-runtime smoke với DB tạm, isolated registry và synthetic effects; không đưa synthetic tool vào builtin registry.

## Thứ tự code

| ID | Việc cần làm | Kết quả kiểm tra được |
|---|---|---|
| P1-01 | Đổi RiskLevel và mọi decorator/fixture/spec consumer. | Startup reject risk không hợp lệ; toàn bộ READ vẫn chạy được. |
| P1-02 | Trong executor, tách validate/policy khỏi `spec.run`; deny unknown/destructive/transport/eligibility trước run. | Callback chưa tồn tại vẫn không có đường mutation tự chạy. |
| P1-03 | Định nghĩa prepared proposal của reminder và `ApprovalRequired`; kiểm owner + target trước trả proposal. | Snapshot có message/due/target đúng và không tạo/sửa reminder. |
| P1-04 | Chat consume control outcome, không persist success; migrate tests gọi executor trực tiếp. | Shared runtime trả `approval_unavailable`, tool rows không ghi mutation thành công. |
| P1-05 | Hoàn thiện risk matrix và chạy gate G1. | Kết quả READ/deny/proposal quan sát qua DB tạm và actual gateway. |

**Contract bàn giao cho Phase 2:** `execute(name, public_params, trusted_context) → ToolResult | ApprovalRequired`. `ApprovalRequired` chứa prepared payload + risk + preview facts; không chứa callback token/decision. Phase 2 mới cấp token và lưu consent. Không sử dụng `ToolResult(ok=False)` để che một side effect đã xảy ra.

## Checklist và gate G1

- [ ] S5-01: mọi production tool được phân loại; không còn `low/medium/high` contracts/callers.
- [ ] S5-02: crafted owner/destination/path/risk/approval params bị reject, owner mismatch không lộ row.
- [ ] Destructive/unknown action bị deny trước callable; approval không nâng quyền cho action đó.
- [ ] Reminder chưa có consent không tạo/sửa/hủy row; READ vẫn chạy đúng capability, URL/locator còn đúng.
- [ ] Cached receipt/runtime recovery không bypass gateway hoặc tái dùng consent cho params khác.
- [ ] Smoke quan sát state/rows/counter thực, không chỉ so spec text hoặc refusal wording.

## Rủi ro và handoff

P0: tách gateway mà để một caller dùng executor cũ → bypass. Control: cutover ngay boundary đang dùng, migrate mọi caller, không shim. P0: approval được coi là permission toàn cục → action scope bất biến + owner SQL. Phase 2 hoàn thiện persistence/consume; chưa nghiệm thu approval chỉ bằng enum hoặc prompt.

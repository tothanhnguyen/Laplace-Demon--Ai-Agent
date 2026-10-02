---
title: "Phase 1: Tool contract and trusted runtime context"
status: completed
priority: P0
effort: "10h"
dependsOn: [260909-0913-sprint3-context-session-memory]
---

# Phase 1: Contract registry, execution context và schema

Status: Completed | Priority: P0 | Effort: 10h | Depends on: Sprint 3 completed

## Mục tiêu

Khóa một contract tool dùng chung trước khi thêm web/document/reminder. Agent loop, schema retry, task card, compaction và giới hạn 5 tool giữ nguyên; mọi tool mới đi qua đúng một executor có validation, ownership context, source metadata và latency thật.

- [Plan tổng](./plan.md) · [Web](./phase-02-web-research.md) · [Document](./phase-03-document-processing.md) · [Reminder](./phase-04-reminder-scheduler.md)
- Baseline: `laplace/tools/base.py:17-108`, `laplace/services/chat.py:472-716`, `laplace/context.py:169-213`, `laplace/repo.py:301-419`.
- Vấn đề thật: duplicate tool đang silent overwrite; callable không nhận trusted actor; output không có schema; `dict` bị đổi thành Python repr; source chỉ đọc từ param `path`; latency tool luôn bằng 0.

## Contract chốt

### T1 — ToolSpec và registration

`ToolSpec` giữ decorator registry, thêm metadata explicit:

- `name`: stable snake_case, unique.
- `purpose`: tool làm gì; không trộn với policy.
- `when_to_use`, `when_not_to_use`: chuỗi ngắn, LLM-visible.
- `risk_level: low | medium | high`: metadata chuẩn bị cho Sprint 5; Sprint 4 không tự nhận đã có Approval Gate.
- `args_model`, `result_model`: Pydantic models có `extra="forbid"`.
- `run(args, context)`: đúng một callable signature cho tất cả tools.

`tool(...)` phải raise khi tên đã tồn tại; không package scanning/plugin discovery. `load_builtin_tools()` tiếp tục import explicit modules để startup deterministic. `specs_for_llm()` trả danh sách ổn định theo tên, gồm JSON schema input/output và metadata sử dụng.

### T2 — Trusted ToolContext

Context immutable, do runtime tạo sau khi resolve user; tối thiểu chứa:

```text
user_id, telegram_user_id, transport, is_private_chat,
conversation_id, request_message_id, task_id, step_index,
attachments: {opaque_id -> resolved Path}
```

- Identity/destination/filesystem path không xuất hiện trong production tool params. Attachment access chỉ qua opaque ID.
- `attachments` chỉ có file của turn hiện tại; document tool không nhận raw path.
- CLI/eval context có `transport=cli`, không có Telegram delivery capability.
- Context giữ plain IDs/path; không đưa ORM Session/object qua thread hoặc vượt transaction.

Retire `read_file` khỏi production cleanly: xóa explicit import và model-controlled path tool; update README/tests/sample để dùng synthetic test-only tool hoặc opaque attachment contract. Không giữ alias/shim. `.env`, source tree và retained upload không còn tool nào address bằng filesystem path.

### T3 — Output và failure envelope

Executor trả canonical envelope:

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "error_code": null,
  "retryable": false,
  "sources": [],
  "latency_ms": 12
}
```

- Success data được validate bằng `result_model`, dump JSON-compatible, không `str(dict)`.
- Expected tool failures dùng exception/result typed với code tối thiểu: `invalid_input`, `configuration_error`, `forbidden`, `not_found`, `unsupported_content`, `upstream_error`, `timeout`, `runtime_error`.
- Đây là transport taxonomy đủ cho Sprint 4; retry policy tổng quát thuộc Sprint 6.
- Unexpected exceptions bị bắt ở tool boundary; message bounded, không lộ token, local path, stack trace hoặc response headers.
- `sources` tối đa bounded; web dùng canonical URL, document dùng opaque ID + locator. Full envelope lưu `ToolCall.result_json`; `ToolCall.error` giữ text lỗi để query; `latency_ms` đo bằng monotonic clock quanh validation/run.

### T4 — Context, memory và citations

`bounded_tool_observation()` nhận canonical envelope:

- Giữ `tool_call_id`, `ok`, error code và sources trước khi cắt data excerpt.
- Toàn message vẫn nằm trong `TOOL_OBSERVATION_MAX_CHARS`; source quá dài cũng bị bound rõ marker.
- Raw web/document content tiếp tục nằm dưới `UNTRUSTED_TOOL_DATA_JSON`, không được biến thành system instruction.
- `repo.tool_rows_for_request_ids`, summary source extraction, task Step preview và `/memory` đọc `sources` từ stored envelope; không còn suy source từ `params.path`.
- Prompt bắt buộc citation chỉ dùng source/locator đã thấy; web/document content không được yêu cầu reminder hoặc hành động khác. Reminder chỉ theo direct user intent.

### T5 — Runtime facts và configuration

- Thêm trusted runtime facts cho mỗi decision: `now_utc`, configured IANA timezone, transport/private flag và opaque attachment IDs. Không lấy “ngày hiện tại” từ model knowledge.
- Preserve layer/budget contract Sprint 3; dựng lại context trước mỗi provider call như hiện tại.
- Add direct dependencies ở `pyproject.toml`: `httpx>=0.27,<1`, `pypdf>=5,<7`, `python-docx>=1.1,<2`, `apscheduler>=3.11,<4`.
- Add settings: `tavily_api_key`, HTTP timeout, web max results/chunks, document byte/unit/char caps, `timezone`, reminder scan interval và misfire grace. Default bounded; secret default rỗng.
- `.env.example` chỉ có placeholder; không copy key thật.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Modify | `laplace/tools/base.py` | ToolSpec/ToolContext/ToolResult/output validation/duplicate guard/latency |
| Delete/retire | `laplace/tools/read_file.py` + production import | Bỏ model-controlled filesystem access; sample/tests dùng isolated synthetic tool |
| Modify | `laplace/services/chat.py` | Tạo context, canonical persistence, source list, real latency |
| Modify | `laplace/context.py` | Bounded structured observation ưu tiên source/error metadata |
| Modify | `laplace/repo.py` | Parse result envelope cho recall/memory; không infer `params.path` |
| Modify | `laplace/prompts.py` | Usage/citation/direct-intent rules + runtime facts |
| Modify | `laplace/config.py`, `pyproject.toml`, `.env.example` | Direct dependencies và bounded settings |
| Modify | `tests/test_chat_service.py`, `tests/test_context.py`, `tests/test_session_memory.py`, `tests/test_bootstrap.py` | Clean-cutover regressions |
| Create | `tests/test_tool_registry.py` | Registry/output/failure contracts có giá trị lâu dài |

## Đầu việc

| ID | Việc | Effort | Gate |
|---|---|---:|---|
| S4-01A | Chốt ToolSpec/ToolContext/ToolResult và strict models | 3h | Không model-controlled identity/path |
| S4-01B | Migrate executor/chat, retire read_file, canonical persistence | 3h | Không signature/repr/source/path tool cũ |
| S4-01C | Generalize observation/recall/memory sources | 2h | Full DB + bounded model view còn đúng |
| S4-01D | Add deps/settings/docs placeholders và focused regressions | 2h | Offline, không secret/network |

## Nghiệm thu G1

- Duplicate registration fail; ordering/spec JSON deterministic.
- Unknown tool, extra param, invalid field, invalid returned model và runtime exception đều thành structured error; agent loop không crash.
- Production specs không chứa `read_file`; gọi tên đó trả unknown tool. `.env`, repository source và upload user khác không address được qua bất kỳ production schema nào.
- A crafted `user_id`, `telegram_user_id`, `destination`, `path` field bị Pydantic reject ở stateful tools.
- Tool structured data round-trip JSON trong DB; observation/model thấy source list; `/memory` đọc lại đúng source; payload lớn vẫn giữ hard context bound.
- Latency test dùng injectable clock hoặc range có tolerance, không assert exact wall time.
- Existing five-tool limit, task ownership, cancellation và compaction behavior không đổi.

Smoke sau focused tests: một turn MockLLM gọi synthetic tool đăng ký trong isolated test registry qua shared `handle_message`, kiểm reply, owned Task/ToolCall, canonical JSON result và bounded observation. Không dùng test-only tool trong production `load_builtin_tools()` và không viết test chỉ assert source text.

## Không thuộc phase

Không implement web/document/reminder behavior ở đây; không Approval Gate; không native provider tool-calling; không cache/plugin system; không tăng MAX_STEPS để che context design lỗi.

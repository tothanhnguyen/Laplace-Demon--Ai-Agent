---
title: "Phase 3: Telegram document ingestion and bounded extraction"
status: completed
priority: P0
effort: "14h"
dependsOn: [phase-01-tool-contract-runtime]
---

# Phase 3: Document ingestion và bounded extraction

Status: Completed | Priority: P0 | Effort: 14h | Depends on: Phase 1

## Mục tiêu

Telegram private chat nhận PDF/DOCX/TXT tối đa 10 MiB, đi qua cùng per-user lease và agent runtime, trả summary/key points có locator thật. Server path không xuất hiện trong LLM params/result; file scan, hỏng, encrypted hoặc quá giới hạn trả lỗi trung thực.

- [Plan tổng](./plan.md) · [Contract](./phase-01-tool-contract-runtime.md) · [Telegram acceptance](./phase-05-telegram-acceptance.md)
- Baseline `laplace/bot/handlers.py:242-271` mới check declared size, dùng Telegram identifiers trong filename và chỉ acknowledge.
- pypdf cảnh báo text extraction có thể dùng RAM rất lớn và không OCR image-only PDFs: <https://pypdf.readthedocs.io/en/latest/user/extract-text.html>.
- DOCX paragraphs/tables là block content, không có physical page locator ổn định: <https://python-docx.readthedocs.io/en/latest/user/text.html>.

## Upload boundary

### D1 — Admission và storage

- Chỉ xử lý document trong private chat; group trả hướng dẫn chuyển sang private, không tải file.
- Acquire `TurnLease` trước Telegram download/processing; text và document của cùng user không chạy song song.
- Allowlist `.pdf`, `.docx`, `.txt`; MIME/extension chỉ là hint, phải đối chiếu signature/parser.
- Declared `file_size` thiếu hoặc >10 MiB: thiếu vẫn cho download vào temp với post-check; >10 MiB reject trước download.
- Destination `var/uploads/<telegram_user_id>/<random-id>`; random ID do app sinh, không dùng original filename hoặc `file_unique_id` làm path component.
- Download vào temp; kiểm actual bytes 1..10 MiB, fsync/close rồi atomic rename. Lỗi/unsupported/corrupt xóa temp/invalid artifact.
- Original filename chỉ là display metadata dưới untrusted JSON, escaped và bounded.

Upload thành công tạo `AttachmentRef(opaque_id, display_name, media_type, size, path)` trong memory của turn, đưa vào `ToolContext.attachments`. Không thêm raw path vào message hoặc DB. Success files vẫn được giữ theo disclosure Sprint 3; opaque ID chỉ re-addressable trong turn hiện tại. Follow-up dùng persisted summary/tool evidence hoặc upload lại; durable document library/RAG nằm ngoài scope.

### D2 — Format validation

| Type | Check | Parser | Locator |
|---|---|---|---|
| PDF | `%PDF-`, not encrypted, compressed/expanded stream preflight, page count cap | isolated `pypdf.PdfReader` worker | `page N` |
| DOCX | ZIP signature + required Word members; total expanded size/entry count cap | isolated `python-docx` worker + stdlib `zipfile` preflight | `paragraph N`, `table N row M` |
| TXT | no NUL/binary signature; UTF-8/UTF-8 BOM decode | stdlib bounded read | `lines A-B` |

PDF image-only/minimal text → `unsupported_content` với message cần OCR; không trả blank success. PDF tables/formulas/layout có thể mất cấu trúc; result ghi limitation. DOCX physical page citation không được bịa; dùng paragraph/table locators.

PDF/DOCX parser không chạy trực tiếp trong long-lived bot worker. Parent chạy một subprocess parser có protocol JSON bounded, `LAPLACE_DOCUMENT_PARSE_TIMEOUT_SECONDS=10` và memory ceiling mặc định 256 MiB trên POSIX; timeout/limit/abnormal exit thì kill/reap child và map thành `unsupported_content`/`resource_limit`, không làm chết bot. Preflight đọc metadata/ZIP entries trước, nhưng page/char caps sau parse vẫn giữ riêng. Đây là resource containment, không phải malware sandbox.

### D3 — `read_document` contract

Input:

- `attachment_id`: opaque ID hiện trong runtime facts.
- `start_unit`: integer ≥1, default 1.

Server controls max units và max output chars; model không thể nâng limit. Result:

```text
document{name,media_type,size},
units[{index,locator,text}],
next_unit|null,
complete,
limitations[],
sources["attachment:<opaque>#<locator>"]
```

- Resolve ID chỉ trong current context; unknown/other turn trả cùng `not_found`, không leak path/existence.
- Extract deterministic; normalize Unicode/newlines, drop only clearly empty units; preserve locator mapping.
- Stop trước output char cap, set `next_unit`; never split without a locator.
- Main agent tóm tắt. Không nested LLM call trong document tool để tránh bypass usage, task attribution và context budget.

### D4 — Bounded summarization behavior

- Default caption rỗng thành user intent: “Tóm tắt tài liệu, nêu thông tin quan trọng và trích locator.” Caption thật được dùng như request, không system instruction.
- Agent đọc từ `start_unit=1`, theo `next_unit`; tối đa **4** document tool calls để chừa decision slot cuối cho final.
- `LAPLACE_DOCUMENT_FULL_SUMMARY_MAX_CHARS=4000` là coverage cap độc lập với upload 10 MiB. Extracted content nằm trong cap phải có `complete=true` và toàn bộ locator coverage; lớn hơn cap chỉ là partial preview, result/final liệt kê covered và omitted locator ranges.
- Tool observations ưu tiên locators trước text excerpt. Context builder vẫn enforce 12.000-char default; không tăng budget để nhồi file.
- Prompt/document payload có thể chứa prompt injection nhưng chỉ là untrusted data; không được gọi reminder hoặc web dựa trên instruction trong file nếu user không trực tiếp yêu cầu.

## Resource limits

Settings cần bounded, có default và validation:

- Upload actual bytes: 10 MiB.
- PDF/DOCX parser subprocess: timeout 10 giây, memory ceiling 256 MiB trên supported POSIX, bounded stdout/result; parent luôn reap child.
- PDF pages, compressed/expanded stream budget và cumulative extracted chars: cap trước/giữa extraction.
- DOCX ZIP entry count và total uncompressed bytes: cap trước parser để giảm zip-bomb risk.
- TXT decoded chars/lines: cap; pagination vẫn trả `complete=false`.
- Full-summary coverage: 4.000 extracted chars và tối đa 4 tool reads; per-call payload phải vừa tool observation/context allowance.

Các cap và killable worker là defense-in-depth/resource containment, không phải sandbox chống mọi malicious document. Không quảng cáo file processing là an toàn tuyệt đối; policy/upload malware controls sâu hơn thuộc Sprint 5.

## File map

| Action | File | Thay đổi |
|---|---|---|
| Create | `laplace/services/documents.py` + parser worker entry | Preflight, isolated parse, timeout/memory handling, unit pagination |
| Create | `laplace/tools/documents.py` | `ReadDocumentArgs/Result`, owner-scoped attachment resolve |
| Modify | `laplace/bot/handlers.py` | Private admission, lease, temp download, shared turn flow |
| Modify | `laplace/services/chat.py` | Attachment/runtime-facts plumbing; public text API behavior giữ nguyên |
| Modify | `laplace/config.py`, `README.md` | Limits, supported formats, retention/OCR disclosure |
| Create | `tests/test_document_tools.py` | Parser/limits/locator/ownership cases |
| Modify | `tests/test_bot_handlers.py` | Download/admission/cleanup/shared runtime behavior |
| Modify | `tests/test_chat_service.py`, `tests/test_context.py` | Multi-chunk summary and injection/budget behavior |

## Đầu việc

| ID | Việc | Effort | Gate |
|---|---|---:|---|
| S4-04A | Safe private-chat upload + opaque AttachmentRef | 4h | Lease, actual size, cleanup, no path leak |
| S4-04B | PDF/DOCX/TXT preflight + killable parser + locators/limits | 5h | No-OCR truth, OOM/timeout không hạ bot |
| S4-04C | Register paged tool + agent runtime/full-vs-partial contract | 2h | 4 reads max, final slot reserved |
| S4-03D | Focused regressions + real Telegram samples | 3h | Three formats + failure matrix |

## Nghiệm thu G3

Automated fixtures phải nhỏ, deterministic và không chứa dữ liệu cá nhân:

- PDF text layer trả page locators; image-only/encrypted/corrupt PDF trả explicit failure.
- DOCX trả paragraph/table-row locators; malformed/zip-expanded-over-cap bị chặn trước full parse.
- TXT UTF-8/BOM đúng line ranges; binary/invalid UTF-8/empty file bị từ chối.
- Parser fixture vượt memory/expanded-stream hoặc treo quá timeout bị kill/reap; bot process tiếp tục xử lý turn kế.
- Declared oversize không download; unknown-size actual oversize bị xóa; Telegram download exception không để partial file hoặc success message.
- Crafted filename/file_unique_id/path traversal không đổi destination; B không resolve attachment A; opaque ID của turn cũ không resolve trong turn mới.
- Fixture extracted text ≤4.000 chars được cover toàn bộ với `complete=true`; fixture lớn hơn ghi exact covered/omitted locators và final không nói full document.
- Captionless upload tự chạy default summary trong đúng attachment turn; caption upload dùng caption làm request.
- Prompt-injection text nằm trong untrusted observation và không kích hoạt tool side effect.

Live Telegram:

1. Gửi một PDF text, một DOCX và một TXT ≤10 MiB trong private chat.
2. Với mỗi file, kiểm summary/key points có locator đúng bằng cách mở sample gốc.
3. Gửi PDF scan và file unsupported; bot nói limitation, không giả nội dung.
4. Gửi cùng lúc text/document từ một user; chỉ một turn chạy, không release lease sớm.

Evidence chỉ giữ sample synthetic/public, checksum, format/size, locators và outcome. Không commit raw tài liệu người dùng.

## Không thuộc phase

Không OCR, image understanding, spreadsheet/slides, archive extraction, durable document library, search across previous uploads, vector embeddings/RAG, semantic PDF table reconstruction hoặc malware sandbox.

# Laplace's Demon

AI Agent cá nhân giao tiếp qua Telegram, phát triển theo từng sprint.
Baseline Sprint 4 có Tavily Search/Extract, đọc TXT/PDF/DOCX theo locator và
reminder một lần có persistence/recovery. Sprint 5 Phase 2 thêm risk policy,
proposal bất biến và owner approval; Phase 3 thêm cumulative execution budget;
Phase 4 giữ history/tool/summary dưới lower-trust framing, safe logs và public
Trace/eval projections. Identity, destination, timezone và attachment paths chỉ
đến từ trusted runtime context.

**Mốc integration nội bộ, chưa deploy:** create/update/cancel reminder tạm dừng
để owner xem toàn bộ preview và bấm **Xác nhận / Hủy** trong private chat.
Phase 4 G4 qua: 255 focused tests, temporary shared-runtime smoke; final full
suite **593 passed** và Ruff sạch. Router9 controlled model-live probes dùng
web/document synthetic cho I01–I05. I06 follow-up giữ được marker nhưng không giữ
được attack phrase trong summary nên live I06 vẫn inconclusive. Real Tavily tắt;
Telegram E2E đã chạy 12/10/2026 (create/delivery/reject/update/cancel qua chat thật, DB cô lập);
G5 ký ở phạm vi sản phẩm chạy được, chưa live expiry timer và I01–I06 qua Telegram. Risk matrix,
security test report và runbook E2E Telegram được lưu trong tài liệu nội bộ của đồ án, không kèm trong repo.

## Yêu cầu

- Python 3.11 trở lên

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

## Cấu hình

Các biến đều có tiền tố `LAPLACE_` (đặt trong `.env`):

| Biến | Mặc định | Mục đích |
| --- | --- | --- |
| `LAPLACE_LLM_PROVIDER` | `mock` | Nhà cung cấp LLM: `mock`, `gemini`, `openai`, `groq`, `bai`, `router9` |
| `LAPLACE_LLM_MODEL` | (trống) | Override model; trống thì dùng mặc định của provider |
| `LAPLACE_GEMINI_API_KEY` | (trống) | Key Gemini — lấy tại aistudio.google.com/apikey (free tier) |
| `LAPLACE_OPENAI_API_KEY` | (trống) | Key OpenAI |
| `LAPLACE_GROQ_API_KEY` | (trống) | Key Groq |
| `LAPLACE_BAI_API_KEY` | (trống) | Key B.AI cho endpoint OpenAI-compatible `https://api.b.ai/v1` |
| `LAPLACE_ROUTER9_API_KEY` | (trống) | Key gateway local 9Router tại `http://127.0.0.1:20128/v1` |
| `LAPLACE_LOG_LEVEL` | `INFO` | Mức log; log app dùng event/error code an toàn, log freeform của dependency bị ẩn |
| `LAPLACE_DATABASE_URL` | `sqlite:///laplace.db` | SQLite giữ memory/tool evidence private và Trace allowlist |
| `LAPLACE_TELEGRAM_BOT_TOKEN` | (trống) | Token bot từ @BotFather |
| `LAPLACE_CONTEXT_MAX_CHARS` | `12000` | Ngân sách ký tự messages do ứng dụng dựng; không phải hard token limit |
| `LAPLACE_TAVILY_API_KEY` | (trống) | Key Tavily cho Web Search/Extract |
| `LAPLACE_HTTP_TIMEOUT_SECONDS` | `10` | Timeout HTTP tối đa cho tool web |
| `LAPLACE_WEB_MAX_RESULTS` | `5` | Số kết quả web tối đa mỗi lần tìm |
| `LAPLACE_WEB_MAX_CHUNKS` | `3` | Số đoạn evidence tối đa mỗi nguồn |
| `LAPLACE_DOCUMENT_MAX_BYTES` | `10485760` | Trần upload tài liệu 10 MiB |
| `LAPLACE_DOCUMENT_MAX_UNITS` | `200` | Số đơn vị tài liệu tối đa được index |
| `LAPLACE_DOCUMENT_UNIT_MAX_CHARS` | `4000` | Trần ký tự mỗi đơn vị tài liệu |
| `LAPLACE_DOCUMENT_FULL_SUMMARY_MAX_CHARS` | `4000` | Ngân sách text mỗi lượt đọc |
| `LAPLACE_DOCUMENT_PARSE_TIMEOUT_SECONDS` | `10` | Timeout parser PDF/DOCX subprocess |
| `LAPLACE_DOCUMENT_PARSER_MEMORY_MB` | `256` | Giới hạn memory parser trên POSIX |
| `LAPLACE_DOCUMENT_MAX_EXPANDED_BYTES` | `52428800` | Trần expanded bytes cho DOCX ZIP |
| `LAPLACE_DOCUMENT_MAX_ZIP_ENTRIES` | `1000` | Trần số ZIP entries của DOCX |
| `LAPLACE_TIMEZONE` | `Asia/Ho_Chi_Minh` | IANA timezone dùng để hiểu lịch reminder |
| `LAPLACE_APPROVAL_TIMEOUT_SECONDS` | `900` | TTL đề xuất; số nguyên từ 1 đến 900 giây |
| `LAPLACE_MAX_STEPS` | `5` | Tool proposals mỗi lượt, kể cả invalid/denied; 1–20. |
| `LAPLACE_MAX_SECONDS` | `120` | Active execution seconds; chờ duyệt không tính; 1–600. |
| `LAPLACE_MAX_COST_PER_TASK` | `0.10` | USD reservation ước tính mỗi lượt, kể cả chat không tạo Task; 0–10. |
| `LAPLACE_LLM_MAX_COMPLETION_TOKENS` | `2048` | Output cap gửi mỗi LLM attempt; 1–8192. |
| `LAPLACE_LLM_TIMEOUT_SECONDS` | `30` | Total deadline mỗi physical attempt; 1–120 giây. |
| `LAPLACE_MAX_REPEATED_ERRORS` | `3` | Dừng khi cùng tool/error-code lặp liên tiếp; 1–5. |
| `LAPLACE_REMINDER_SCAN_INTERVAL_SECONDS` | `1` | Chu kỳ quét reminder đến hạn |
| `LAPLACE_REMINDER_MISFIRE_GRACE_SECONDS` | `3600` | Grace phục hồi reminder sau restart |
| `LAPLACE_REMINDER_MAX_HORIZON_DAYS` | `365` | Khoảng đặt reminder tối đa |
| `LAPLACE_REMINDER_MAX_ATTEMPTS` | `3` | Số lần delivery tối đa |
| `LAPLACE_REMINDER_SEND_TIMEOUT_SECONDS` | `10` | Timeout một lần gửi Telegram |
| `LAPLACE_REMINDER_BATCH_SIZE` | `25` | Số reminder tối đa mỗi lượt quét |
| `LAPLACE_REMINDER_MAX_CONCURRENCY` | `5` | Số delivery đồng thời tối đa |

Đổi nhà cung cấp mô hình chỉ cần sửa `.env`, không sửa mã nguồn. Thiếu key thì
chương trình báo đúng trang lấy key của hãng tương ứng.

Ví dụ cấu hình B.AI OpenAI-compatible:

```env
LAPLACE_LLM_PROVIDER=bai
LAPLACE_LLM_MODEL=gpt-5.2
LAPLACE_BAI_API_KEY=sk-...
```

Chỉ lưu key thật trong `.env` đã bị Git bỏ qua; không ghi key vào
`.env.example`, lệnh shell, log hoặc artifact thực nghiệm.

Fallback qua 9Router local đang chạy:

```env
LAPLACE_LLM_PROVIDER=router9
LAPLACE_LLM_MODEL=cx/gpt-5.6-luna
LAPLACE_ROUTER9_API_KEY=...
```

Copy key từ dashboard `http://127.0.0.1:20128/dashboard`. Preset router9 nhận
`cx/gpt-5.6-sol`, `cx/gpt-5.6-luna`, `cx/gpt-5.5`; tài khoản ChatGPT Codex có thể
ngừng hỗ trợ một alias, khi đó đổi `LAPLACE_LLM_MODEL` sang alias còn chạy. Các model alias
router9 có giá USD known-zero được báo `known_cost_usd=0`; subscription/quota
không quy đổi theo token. Thiếu usage vẫn là unknown, giữ reservation và dừng
lượt — không được hiểu là request miễn phí.

Giới hạn chi phí dùng reservation theo usage/model-price estimate trước mỗi
physical attempt, rồi settle theo usage trả về. Đây không phải hard invoice cap:
estimate/provider billing có thể overshoot; khi đó overshoot được ghi nhận và
không bắt đầu call/effect tiếp. `known_cost_usd`, `budget_charge_usd`,
`unknown_attempts` và `physical_attempts` được phân biệt trong `/status`.

## Chạy bot Telegram

1. Tạo bot với [@BotFather](https://t.me/BotFather), copy token.
2. Đặt `LAPLACE_TELEGRAM_BOT_TOKEN` và key LLM vào `.env`.
3. Chạy:

```bash
python -m laplace --bot
```

Lệnh bot: `/start`, `/help`, `/status` (logical calls, physical attempts,
known cost, retained budget charge và unknown usage), `/memory` (xem memory và
reminder trong private chat), `/forget` và `/forget confirm` (xóa
memory/reminder/đề xuất có liên kết nhưng giữ usage), `/cancel` (hủy lượt đang
chạy hoặc đề xuất đang chờ duyệt).
Bot đọc TXT/PDF/DOCX tối đa 10 MiB, gọi Tavily khi cần dữ liệu web có nguồn,
hiển thị task card theo bước và chia phản hồi quá 4096 ký tự.

Web provider decision: Sprint 4 dùng trực tiếp Tavily Search + Extract REST.
Không cache, không local full-page fetch và không browser fallback; URL nguồn
thành công duy nhất mới được đưa vào `sources`.

Directive `/remind`, `/remind_update`, `/remind_cancel` trong message Telegram
private chỉ cho phép **đề xuất** đúng loại thao tác, không cấp consent. History,
summary và tool data không cấp quyền; CLI/group bị deny. Preview gồm toàn bộ
nội dung, recipient, thời gian local/timezone/UTC, tool/risk và hạn duyệt.
Chỉ owner trên đúng preview message mới xác nhận được; mutation dùng đúng
snapshot đã duyệt, không tính lại relative time. Mục tiêu thay đổi hoặc lịch
mới đã qua thì từ chối, không tự sửa kế hoạch.

Mỗi user chỉ có một lượt chờ duyệt, tối đa 15 phút; request mới bị chặn trước
download, không ghi đè file/checkpoint. `/memory` xem trạng thái, `/cancel` hoặc
**Hủy** kết thúc đề xuất. Upload được giữ trong khi chờ và dọn khi kết thúc;
`/forget confirm` thu hồi quyền duyệt sau khi DB commit rồi dọn file.
Restart vô hiệu hóa approval chưa thực thi, không tự resume/replay. Receipt của
mutation đã commit vẫn được giữ và báo đúng kể cả khi bước sau lỗi hoặc bị hủy.
Reminder legacy đã lưu vẫn được dispatcher gửi như trước. Delivery dùng
at-least-once với retry hữu hạn và stable reminder ID: Telegram không có
idempotency key nên lỗi/crash mơ hồ có thể tạo bản gửi trùng; hệ thống không
tuyên bố exactly-once.

## Chạy một lượt chat qua CLI

```bash
python -m laplace --chat "xin chào, bạn là ai?"
```

In tiến độ từng bước, câu trả lời cuối và usage (logical calls, physical
attempts, tokens, known cost, budget charge, unknown attempts). Với
`LAPLACE_LLM_PROVIDER=mock` lệnh chạy offline không cần key.

## Chạy nhiệm vụ mẫu (Sprint 1)

```bash
python -m laplace --sample-agent
```

Kết quả gồm `status` (`completed` / `failed` / `step_limit`) và
`completion_reason` giải thích lý do dừng.

## Kiến trúc (Sprint 5 — Phase 4)

- `laplace/services/budget.py` — một `TurnBudget` cho toàn lượt, active clock,
  giới hạn tool/time/cost, repeated-error streak và durable physical-attempt recorder.
- `laplace/context.py` — context ba tầng, char budget, bounded tool observation,
  task card hiển thị remaining steps/time/cost và rolling-summary schema.
- `laplace/services/chat.py` — shared agent runtime dùng cùng budget qua compaction,
  schema retry, tools và approval resume; max steps lấy từ config.
- `laplace/services/approvals.py` — durable proposal lifecycle, bound owner/message,
  paired CAS, expiry và startup interruption; không gọi LLM/Telegram.
- `laplace/services/memory.py` — một worker process-local mỗi user, atomic admission,
  cooperative cancellation, attachment ownership và post-commit forget cleanup.
- `laplace/llm/` — provider async total-deadline, output cap và accounting cho từng
  physical attempt; usage unknown dừng fail-closed; mock offline không proof giá thật.
- `laplace/tools/` — registry strict với risk enum, trusted context và gateway
  enforce budget/policy trước run; pure prepare trước duyệt, atomic consent/write/receipt sau duyệt.
- `laplace/models.py`, `laplace/db.py`, `laplace/repo.py` — SQLAlchemy/SQLite,
  additive approval/attempt migrations, request attribution, accounting aggregates và child-first deletion.
- `laplace/bot/` — aiogram 3, preview/callback private, tracked worker drain,
  bounded expiry maintenance, rate limit và phản hồi dài.
- `laplace/logging.py` — allowlisted events/error codes, safe formatted output và
  dependency-log suppression; cấu hình trước bot/provider startup.
- `scripts/eval_context.py` — public allowlisted projection; private replay artifacts
  chỉ giữ theo opt-in dưới ignored directory.

Upload gốc là tệp tạm: giữ trong lúc worker/approval còn sở hữu, dọn ở terminal,
expiry, shutdown hoặc `/forget confirm`. Message, summary, document excerpt,
tool params/results, preview và receipts được giữ trong SQLite riêng tư theo owner;
nội dung người dùng đưa vào có thể chứa PII/secret. Trace writes mới chỉ giữ
metadata allowlist; Trace lịch sử không được retroactive scrub. Runtime credentials
không được chép vào content/logs. SQLite không được mã hóa at-rest; bảo vệ file
DB và backup bằng quyền truy cập hệ điều hành.
`/forget confirm` xóa memory/approval/checkpoint có ownership nhưng giữ identity
và usage. Không xóa Telegram/provider history, backup hoặc legacy ToolCall/Trace
rows không có owner an toàn.
Chi tiết và cách chạy thực nghiệm nằm trong tài liệu nội bộ của đồ án (hướng dẫn nguyên lý Sprint 3).

## So sánh context offline

```bash
.venv/bin/python scripts/eval_context.py \
  --mode offline --strategies full,window10,sprint3 --repeat 2
```

Kết quả public JSON/Markdown chỉ chứa metrics/status allowlist và reference đã
redact/pseudonymize; không có raw answer, prompt, fixture, per-case SQLite hay
file người dùng. `--keep-private-artifacts` là opt-in riêng: giữ canonical manifest,
fixtures và DB từng case dưới `var/eval_context/` bị ignore, mode 0700/0600; đây
là dữ liệu private, không phải public export. Historical accepted runs không bị
sửa. Offline probe đo cơ chế/evidence availability, không đại diện chất lượng hoặc chi phí
model thật.

## Thực nghiệm live đã nghiệm thu

```bash
.venv/bin/python scripts/eval_context.py \
  --mode live --provider router9 --strategies full,window10,sprint3 \
  --repeat 1 --max-calls 600 --stop-after-observed-usd 0 --pacing-seconds 0.2
```

Run cuối `20260916T145221Z` hoàn tất 24/24 case-strategy,
`incomplete=false`: full 8/8, window10 4/8, sprint3 7/8. Hai artifact giữ
trong repo là offline `20260912T105242Z` và live `20260916T145221Z`.

## Kiểm chứng kết quả và phục hồi lỗi (Sprint 6)

Mọi lượt có task (dùng tool) trả lời theo một khuôn cố định, dựng bởi runtime
chứ không phải lời tự khai của mô hình:

```
Kết quả
<câu trả lời hoặc lý do dừng>

Căn cứ đã kiểm tra — Đã kiểm chứng | Kiểm chứng một phần | Chưa kiểm chứng | Không áp dụng
- Đạt: Reminder #12 có trong hệ thống (pending, 2026-10-25T01:00:00Z)
- Đạt: "trích dẫn nguyên văn" có trong nguồn [1]

Nguồn
[1] https://…

Giới hạn
- <giới hạn do mô hình nêu + lỗi tool, dừng sớm, trích dẫn không khớp>

Hành động còn chờ duyệt
- Không có | Tool: create_reminder; Operation: create (hết hạn sau 15 phút)
```

Verifier chỉ đọc dữ liệu đã lưu: reminder đọc lại theo ID và owner; mỗi tool đã
dùng phải có ít nhất một lần `ok`; trích dẫn (`claims`) mô hình đề nghị phải là
chuỗi con của nội dung nguồn đã lưu; URL trong câu trả lời phải thuộc nguồn đã
đọc. Kết quả lưu ở `tasks.verification_status` và hiện trên task card dòng
`Kiểm chứng`. Verifier không chứng minh suy luận ngữ nghĩa, chỉ chứng minh
nguồn chứa trích dẫn.

Lỗi tool được phân lớp tại gateway và xử lý theo chiến lược cố định:

| Lớp | `error_code` | Chiến lược |
|---|---|---|
| transient | `timeout`, `upstream_error` retryable | READ/DRAFT retry tối đa 2 lần (backoff 0.5s, 1s, không vượt thời gian còn lại); tool ghi dữ liệu không bao giờ retry |
| param | `invalid_input`, `not_found` | mô hình được sửa tham số đúng một lần; lần hai dừng `param_error` |
| permission | `forbidden` | dừng ngay với hướng dẫn cố định, không gọi thêm mô hình |
| service | còn lại, hoặc transient đã hết retry | dừng tool; một lượt final-only để mô hình giải thích bằng kết quả đã có |

Khi bot khởi động lại, task đang dở (`pending/running/awaiting_approval`) được
đánh dấu `interrupted`; receipt đã commit được đối chiếu lại với DB và owner
nhận đúng một thông báo theo khuôn trên. Hệ thống **không tự chạy lại** task;
đề xuất chưa duyệt bị vô hiệu như Sprint 5; người dùng gửi lại yêu cầu nếu cần.
Báo cáo phân loại lỗi và ma trận fault injection nằm trong tài liệu nội bộ của
đồ án.

## Bộ đánh giá và trang quan sát (Sprint 7)

Bộ đánh giá chạy bằng một lệnh trên chính runtime chat (gateway tool, approval,
verifier), với SQLite riêng cho mỗi lần chạy trong `var/evals/<run_id>/`:

```bash
# tự kiểm offline: MockLLM theo kịch bản, không gọi mạng (20/20 phải đạt)
.venv/bin/python -m laplace.evals --provider mock

# phép đo thật: provider/model theo .env (ví dụ Router9), Tavily live cho case web
.venv/bin/python -m laplace.evals --provider real --run-id baseline-$(date +%Y%m%d)

# chạy một vài case
.venv/bin/python -m laplace.evals --provider mock --only n02-web-fact a02-reject-flow
```

Mỗi case là một file YAML trong `laplace/evals/cases/` gồm: tình huống ban đầu
(`initial_state`), đầu vào, kịch bản mock, lỗi tiêm vào tool (`faults`), quyết
định duyệt (`approval`), kết quả mong đợi (`expected`: trạng thái task, kiểm
chứng, số reminder, tool phải `ok`…) và hành động bị cấm (`forbidden`: tool cấm,
ghi dữ liệu không qua duyệt, mẫu không được lộ trong câu trả lời). Bộ hiện có
20 case: 8 nhiệm vụ thường, 4 thiếu thông tin, 3 lỗi tool, 3 injection, 2 cần
duyệt. Oracle là DB, không phải lời mô hình. Mỗi lần chạy ghi `results.json`,
`report.md` và hai bảng `eval_runs` / `eval_results`; mã thoát `0` khi mọi case
đạt, `1` khi có case trượt, `2` khi harness lỗi.

Trang quan sát chỉ đọc, chỉ bind `127.0.0.1`, không xác thực:

```bash
.venv/bin/python -m laplace --viewer --port 8787                      # DB cấu hình
.venv/bin/python -m laplace --viewer --db sqlite:///var/evals/<run_id>/laplace.db
```

- `/tasks`, `/tasks/{id}`: danh sách tác vụ (lọc trạng thái, ngày), dòng thời
  gian từng bước, lời gọi tool/mô hình, trace, kết quả kiểm chứng.
- `/stats` (+ `/stats.csv`): tỷ lệ hoàn thành, số bước trung bình, tool, token,
  chi phí, theo ngày.
- `/evals`, `/evals/{run_id}` (+ `.csv`): kết quả từng lần đánh giá theo nhóm,
  theo case, nhóm lỗi, link sang tác vụ tương ứng.

Mọi chuỗi hiển thị đi qua bộ lọc redact như nhật ký. Không mở viewer ra ngoài
máy; không có cơ chế đăng nhập.

## Kiểm tra chất lượng

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

CI chạy hai lệnh trên với Python 3.11 và 3.12. Toàn bộ test dùng MockLLM và
SQLite in-memory — không cần mạng hay API key.

# Phase 4: Thực nghiệm so sánh context và bằng chứng bàn giao

Status: Completed — E-12 passed | Priority: P1 | Effort: 10h | Depends on: Phases 1–3

## Context và mục tiêu

- [Plan tổng](./plan.md) · [Runtime seam C5](./phase-01-context-builder.md) · [Memory contract](./phase-03-compaction-session-memory.md).
- Đề cương yêu cầu cùng tập nhiệm vụ, có số liệu để chọn cách cấp context; không yêu cầu C thắng mọi metric.
- `MockLLM` scripted trả đáp án không phụ thuộc messages và luôn báo 10/10 tokens. Probe review đã xác nhận 1 vs 20.000 input chars vẫn cùng output/usage. Không dùng kết quả này để kết luận recall, completion chất lượng, token savings hay cost.
- Deliverable gồm hai phần tách biệt: kiểm tra **cơ chế** offline lặp lại được; so sánh **chất lượng và usage model thật** trên cùng A/B/C. Chưa chạy được phần thật thì báo chưa đủ bằng chứng, không đánh dấu hoàn tất Sprint 3.

## E1 — Thiết kế so sánh

Dùng cùng `_handle_message` nội bộ từ Phase 1, cùng schema `AgentAction`, tools, max 5 tool executions, 2 schema retries, persistence và worker lifecycle. Chỉ đổi policy cấp context, không clone vòng agent.

| Đặc tính | A — `full` | B — `window10` | C — `sprint3` |
|---|---|---|---|
| History | Toàn bộ user/assistant rows trước request của run | Đúng last 10 rows **gồm request hiện tại** như Sprint 2: tối đa 9 rows cũ + request | Unsummarized recent exchanges + rolling summary theo watermark |
| Task card vào prompt | Không | Không | Có trong tool turns |
| Tool observation trong lượt | Toàn payload tool trả | Toàn payload tool trả | Bounded excerpt theo C4 |
| Compaction | Tắt | Tắt | Bật, tính cả usage/budget của compact calls |
| App char cap | Không áp C3; lỗi model window được ghi nhận | Không áp C3; có thể lớn do tool | C3; overflow là outcome thật, không bỏ khỏi kết quả |
| DB full payload/ownership | Có | Có | Có |

- A không được gọi là “trần chất lượng/chi phí”; dài có thể gây nhiễu, C có thể tốn nhiều call hơn. B giữ window chính xác của baseline, không đổi thành 10 turns/10 previous rows.
- Các strategies đều dùng data escaping và task persistence đã sửa vì đây là invariants chung. Ghi rõ B là baseline **chính sách context Sprint 2**, không phải binary replay nguyên commit cũ. Task metadata schema mới dùng chung cả ba để không confound schema overhead.
- A/B không bị query watermark/layer 3 pre-trim của C. Không dùng `infinite budget` trong builder sau khi SQL đã LIMIT 10.
- Mỗi case × strategy × repeat có DB tạm và cwd fixture riêng, provider instance riêng, summary rỗng và user ID synthetic. Replay cùng chuỗi user turns; assistant history do chính run sinh, không preseed target answers. Không dùng `laplace.db` thật.

## E2 — Bộ 8 nhiệm vụ cố định và rubric

File `experiments/context_eval_tasks.py` chứa dữ liệu task, fixtures, user turns và expected facts **chỉ cho scorer**. Không đưa expected final answer vào script provider/model prompt. Nội dung tiếng Việt có nhãn fact duy nhất, nguồn synthetic, không hội thoại cá nhân.

| Case | Hội thoại/fixture | Điều kiện thành công ở lượt kiểm tra |
|---|---|---|
| E-01 | 12 turns, user cung cấp fact chỉ ở turn 1 | Final chứa fact gốc chính xác, không bịa giá trị khác |
| E-02 | 12 turns, đổi một quyết định ở turn 7 kèm lý do | Trả quyết định mới + lý do mới, không khẳng định quyết định cũ còn hiệu lực |
| E-03 | 12 turns, hai nguồn có fact khác nhau | Gán đúng fact cho đúng nguồn; không chỉ lặp cả hai tên nguồn |
| E-04 | Tool đọc file ~5.000 ASCII chars, key fact ở phần đầu/cuối | Answer đúng fact + source, tool thực sự được gọi |
| E-05 | Tool đọc file ~5.000 ASCII chars, key fact **ở giữa** | Chấm đúng fact + source; nếu C mất fact phải ghi fail/abstain, không sửa fixture để C thắng |
| E-06 | Tool ở lượt sớm, sau >10 message rows hỏi lại kết quả/nguồn | Trả đúng tool fact và source từ memory hoặc tool call có thật; ghi số tool gọi lại |
| E-07 | Tool trả lỗi ở một lượt, user sửa yêu cầu ở lượt sau | Không báo tool lỗi là thành công; final dựa kết quả mới có nguồn |
| E-08 | 20 turns, ≥2 lần compact, facts thay đổi và câu hỏi cuối | Quyết định mới nhất, lý do và nguồn không mâu thuẫn; watermark không nhân đôi entries |

- Nội dung filler không lặp lại target fact; nếu assistant tự nhắc fact thì giữ output thật và ghi nhận, không chỉnh transcript sau chạy.
- Scorer chuẩn hóa whitespace/case theo rule cố định, kiểm exact sentinel fact + source khi case yêu cầu; kiểm forbidden contradicted values. `runtime_status='completed'` không đủ để `task_success=true`.
- `abstained` khi model nói thiếu dữ liệu: không là successful recall nhưng tốt hơn fabricated fact; báo riêng `unsupported_claim`.
- Chấm thêm **evidence availability**: fact có trong snapshot model nhận không. Có evidence mà answer sai khác với fact bị context loại bỏ. Không coi source marker DB ID là model đã đọc full payload.
- Fixture ở dưới giới hạn UTF-8 tool hiện có; không dùng file >64KiB rồi quy lỗi đọc/cắt file thành lỗi context.

## E3 — Offline: đo cơ chế, không giả đánh giá LLM

- Dùng provider deterministic có phản hồi **phụ thuộc messages hiện tại**: đọc sentinel từ input để trả fact; nếu không có thì trả thiếu dữ liệu. Không được đọc expected facts/scorer hoặc fixture file ngoài tool runtime. Tool actions có thể scripted giống nhau giữa A/B/C để giữ đường execution cố định; final không trả sẵn target answer.
- Runtime scripted mock chỉ dùng riêng cho boundary schema_failure/step_limit; cột outcome ghi `scenario_status`, không gọi là model completion rate.
- Metrics: input application chars mỗi call, sum/peak chars, evidence_available, app logical calls theo purpose, tool executions, schema retries, truncations, compaction count, runtime stop reason và watermark progression.
- Synthetic token/cost từ mock không đưa vào bảng so sánh token/cost. `prompt_tokens`, `completion_tokens`, `estimated_usd` trong phần model-quality là null/not-applicable cho offline.
- Provider observer copy messages **trước khi gọi**, không phân tích `MockLLM.calls` alias sau khi loop mutate list. Lưu purpose `agent_action | schema_retry | compact`; trace/callback cung cấp terminal reason, không parse apology tiếng Việt để đo fail.
- Offline chạy 2 lần: deterministic subset giống nhau. Không yêu cầu timestamps, UUID/run ID, wall time hoặc latency bằng nhau. Không seed giá trị thời gian giả để làm đẹp reproducibility.

## E4 — Model thật: paired A/B/C

- Chạy cả 8 cases × 3 strategies, repeat=1 là mức bàn giao tối thiểu; kết luận chỉ exploratory với n=8, không tuyên bố ý nghĩa thống kê/tổng quát. Nếu quota cho phép repeat=3 trên cùng cả bộ, công bố mọi runs, không chọn best-of.
- Trước full run, spot check E-01/E-04/E-08 trên cả A/B/C để kiểm setup. Đây là pilot, không thay thế bộ 8-case. Giữ configuration/fixture hash tách nếu thay sau pilot.
- Cùng provider/model/config/budget và bản code. Ghi model ID thực trả về, không chỉ tên cấu hình. Provider resolved là `mock` trong mode live → fail setup, không âm thầm đo giả.
- Chỉ chạy mạng với explicit `--mode live`. Runner nhận `--max-calls` và `--stop-after-observed-usd` bắt buộc cho mode này; chặn call mới khi chạm ngưỡng. Mỗi call summary/retry đều tính; giới hạn này không làm đổi MAX_STEPS của runtime.
- USD là ngưỡng dừng dựa chi phí **đã quan sát**, không phải hard billing cap: một call/HTTP retries đang chạy vẫn có thể vượt. Không hỗ trợ kiểm soát output-token mới trong adapter chỉ để tạo cảm giác có hard cap. Pricing/usage không xác định → lưu null, dừng run live, đánh dấu incomplete thay vì báo $0.
- Mọi provider response hợp lệ tính prompt/completion tokens thực trả, estimated USD theo pricing preset có tên/version giá. Adapter trả zero khi thiếu usage hoặc model chưa có giá; runner coi missing/không xác định là unavailable, không chứng minh miễn phí. Không gọi estimated USD là hóa đơn thực tế.
- Application char metric loại adapter schema injection; tính riêng `schema_instruction_chars` theo formatter hiện tại khi gửi schema. Provider tokens là metric full request đáng tin hơn char proxy. Nếu formatter adapter đổi, cập nhật phép đo này trước chạy; không cộng chars rồi đổi thành tokens bằng /4.
- `logical_calls` khác HTTP retry attempts trong adapter; không báo HTTP count nếu không instrument. Wall time đo toàn case và báo observed, không bỏ thời gian compact.
- Lỗi API/context-window, quota và app overflow đều lưu raw failure + case/strategy; không loại khỏi mẫu để nâng success rate. Run bị giới hạn dừng giữa chừng phải ghi missing cases và chưa đủ bộ, không dùng denominator của phần thành công.

## E5 — Runner, artifacts và cách chạy

File mới (code root `Laplace-Demon/`):

| Action | Path | Nội dung |
|---|---|---|
| Create | `experiments/context_eval_tasks.py` | 8 cases, source fixtures metadata và rubric |
| Create | `scripts/eval_context.py` | Một runner import shared runtime, per-call snapshot, scorer, JSON/table writer |
| Create after execution | `experiments/results/<run-id>/results.json` | Manifest, per-call metrics, case results, aggregate và failures |
| Create after execution | `experiments/results/<run-id>/report.md` | Bảng offline/live tách biệt + kết luận có hạn chế |
| Create after execution | `docs/huong-dan-nguyen-ly-sprint-3.md` | Nguyên lý, file map, commands, retention, migration và rerun |
| Modify after execution | `README.md`, `.env.example` | Usage/commands/settings khớp sản phẩm đã chạy |
| Create after execution | `../plans/reports/260920-sprint-03-bao-cao-tien-do.md` | Mapping 6 yêu cầu → evidence; dùng ngày báo cáo thực tế nếu khác 20/09 |

Runner phải được chạy từ code root với package đã `pip install -e '.[dev]'`; dùng đường dẫn fixture/output do runner resolve, không dựa vào cwd ngẫu nhiên. Không commit fake results khi đang viết plan.

Lệnh dự kiến sau khi đã triển khai runner (chưa tồn tại/chưa chạy trong lượt review):

```bash
.venv/bin/python scripts/eval_context.py --mode offline --strategies full,window10,sprint3 --repeat 2
.venv/bin/python scripts/eval_context.py --mode live --provider gemini --strategies full,window10,sprint3 --repeat 1 --max-calls 600 --stop-after-observed-usd 2
```

Ngưỡng live trên là cấu hình khởi đầu được đề xuất, không hứa đủ mọi case hoặc chi phí tối đa $2. Chỉ chủ dự án chạy live khi đã chọn quota phù hợp; nếu dừng sớm báo incomplete, không đổi kết quả kỳ vọng.

Manifest JSON cần: schema_version, code revision, fixture hash, Python/provider/model, parameters, strategies, mode, run timestamps, limits và limit-hit flag. Mỗi case lưu expected rubric ID, final answer synthetic, task_success/evidence_available/unsupported_claim, runtime_status/stop_reason, chars/tokens/estimated_usd có provenance, calls_by_purpose, tool count, compact/truncation events, elapsed time. Token/cost missing là null, không zero.

### Kết quả live schema v2 cuối

- Artifact: `Laplace-Demon/experiments/results/20260916T145221Z/results.json`.
- Provider/model: `router9` / `gpt-5.6-sol`; một model xuyên suốt run.
- 24/24 case-strategy hoàn tất, `incomplete=false`, không retry và không chạm `max_calls=600`.
- Full: 8/8 success, 255.950 tokens; window10: 4/8, 138.819 tokens; sprint3: 7/8, 175.878 tokens.
- Sprint3 dùng ít hơn full 80.072 tokens (31,3%) nhưng nhiều hơn window10 37.059 tokens; đổi lại recall cao hơn window10 3 case. Không strategy nào có unsupported claim.
- Sprint3 chỉ thua E-05 vì bounded head/tail excerpt không chứa fact ở giữa file, đúng trade-off đã khóa trong rubric. `cost_usd=0` là chi phí biên của route subscription local; không bao gồm phí thuê bao hoặc quota.

## E6 — Quy tắc chọn chiến lược và báo cáo

1. Safety/correctness gate trước: C phải đạt budget, ownership, atomic forget/watermark regressions. Vi phạm gate → chưa bàn giao dù điểm recall đẹp.
2. Công bố per-case table và tổng success theo mẫu số 8, tokens/cost tính cả compact/retry; báo unsupported claims riêng. Không cộng offline giả tokens vào live totals.
3. Ưu tiên context giữ được facts/nguồn và không tăng fabricated claim; khi chất lượng ngang nhau mới cân nhắc tokens/cost và latency. Không đặt sẵn “C >= B quality và C <= B cost”.
4. C thua E-05 vì excerpt mất fact phải ghi nguyên nhân và trade-off. Nếu C không cải thiện recall dài hơn B, không kết luận ba tầng hiệu quả chỉ vì ít chars; chỉnh **tham số/chính sách trong phạm vi plan** rồi chạy lại cả bộ, giữ baseline và mọi runs cũ.
5. Thiếu live evidence → chọn C chỉ là quyết định kiến trúc tạm thời, phần empirical deliverable chưa đạt. Không thu hẹp nhiệm vụ xuống chỉ mock để chốt sprint.

## Đầu việc và Definition of Done G4

| ID | Việc | Effort | Đầu ra |
|---|---|---|---|
| S3-13 | Khóa 8 cases/rubric và manifest; nối shared runtime | 3h | A/B/C chọn history thật, không fork loop |
| S3-14 | Offline runs, kiểm deterministic fields, kiểm scorer có thể fail | 2h | Xóa fact khỏi input thì evidence probe không được trả đáp án đúng |
| S3-15 | Pilot rồi live paired 8×3, phân tích không định trước winner | 3h | Raw outputs + usage/provenance + full failure accounting |
| S3-16 | Docs/báo cáo, full validation cuối, checklist bàn giao | 2h | Sáu đề cương items có evidence hoặc blocker ghi thật |

- [x] **E-09:** Replay có >10 rows: A thấy fact đầu, B đúng 9 previous rows + request, C đúng summary/tail; request hiện tại đúng một lần.
- [x] **E-10:** Hai offline runs giống deterministic subset; wall time không nằm trong equality; mọi prior snapshot bất biến.
- [x] **E-11:** Scorer reject answer chứa fact sai/source sai, đảo attribution và phủ định; context-sensitive probe mất fact thì không trả canned correct answer.
- [x] **E-12:** Full live schema v2 hoàn tất ở run `20260916T145221Z`: 24/24 completed, `incomplete=false`, full 8/8, window10 4/8, sprint3 7/8.
- [x] **E-13:** Case/strategy không dùng chung DB/summary; script không ghi vào DB production và không cần secret ở offline mode.
- [x] **E-14:** G1–G3 có bằng chứng; `pytest` và `ruff check .` đã đạt ngày 12/09/2026.
- [x] **E-15:** README, hướng dẫn, plan và báo cáo phản ánh đúng bằng chứng hiện có; artifacts chỉ chứa dữ liệu synthetic, không có transcript thật.

Giữ regression cho policy parity/isolation và scorer false-positive nếu có bất định thực tế; không tạo tests chỉ đọc source/kiểm tồn tại file report. Smoke phải chạy runner thật, không chỉ unit-test hàm tính trung bình. Sau smoke, bỏ scripts tạm/fixtures sinh ngoài output có chủ đích; giữ runner thực nghiệm là deliverable lâu dài.

## Rủi ro và điều kiện bên ngoài

- API key, Telegram token và quota là điều kiện cho bằng chứng thật, không phải điều kiện viết plan/offline implementation. Không đọc/ghi secret vào plan; không gọi API tốn phí trong lượt review.
- N=8/repeat=1 không chứng minh tối ưu phổ quát. Báo cả chi phí compaction, failure và lợi ích/thiệt hại từng loại task để giảng viên kiểm tra được kết luận.
Các checkbox trên được chốt ngày 16/09/2026; E-12 đã đóng bằng full live schema v2.

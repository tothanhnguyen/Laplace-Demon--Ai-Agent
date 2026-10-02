# Sơ đồ nguyên lý hoạt động — Laplace-Demon (bản triển khai, Sprint 2)

> Nguồn: mã thật của `Laplace-Demon/laplace/` — `__main__.py`, `agent.py`, `config.py`, `tasks.py`.
> Sprint 2: **Agent loop thuần Python, deterministic** — không DB, không LLM, không tool, không Telegram, không trace.

## Nguyên lý tóm tắt

1. `__main__.py` là CLI (`python -m laplace`). Không có cờ → khởi tạo `Settings()` và in dòng sẵn sàng. Có `--sample-agent` → chạy nhiệm vụ mẫu.
2. `tasks.py` chỉ chứa **dữ liệu**: factory tạo `AgentTask` (tên + tuple `TaskStep` theo thứ tự). Không thực thi gì. Cả `TaskStep` và `AgentTask` đều validate trong `__post_init__`: tên rỗng → `ValueError`, sai kiểu → `TypeError`, bước thành công kèm `failure_reason` → `ValueError`.
3. `agent.py` chứa vòng lặp: `Agent(max_steps).run(task)` xử lý **tối đa một bước mỗi vòng**, dừng ngay khi bước thất bại (`failed`) hoặc chạm giới hạn (`step_limit`), hết bước → `completed`. Mọi kết quả đều có `completion_reason` khác rỗng. `AgentResult` cũng validate: `processed_steps` không được vượt `total_steps`, `completion_reason` không rỗng.
4. `config.py` nạp `.env` (tiền tố `LAPLACE_`) qua pydantic-settings — chỉ phục vụ đường in dòng sẵn sàng.

## Sơ đồ luồng hoạt động

![Sơ đồ nguyên lý Laplace-Demon](so-do-laplace-demon.png)

```mermaid
flowchart TD
    U(["Người dùng<br/>python -m laplace"]) --> CLI["__main__.py<br/>argparse"]

    CLI -->|"--sample-agent"| F["tasks.py<br/>successful_sample()"]
    CLI -->|"không có cờ"| S["config.py<br/>Settings()"]
    S --> ENV[".env<br/>tiền tố LAPLACE_"]
    ENV --> RD["in dòng sẵn sàng<br/>app_name · environment · log_level"]

    F --> ST["AgentTask<br/>name + steps: tuple[TaskStep]"]
    ST --> V1{"dataclass<br/>__post_init__"}
    V1 -->|"tên rỗng / sai kiểu /<br/>steps không phải tuple"| ERR["ValueError / TypeError"]
    V1 -->|"hợp lệ"| VST{"TaskStep validation<br/>__post_init__"}
    VST -->|"succeeds=True +<br/>failure_reason không rỗng"| ERR
    VST -->|"tên rỗng / sai kiểu bool/str"| ERR
    VST -->|"hợp lệ"| RUN["Agent(max_steps).run(task)<br/>agent.py"]

    RUN --> V2{"max_steps ≥ 0 ?"}
    V2 -->|"không"| ERR2["ValueError"]
    V2 -->|"có"| LOOP["for step in task.steps[:limit]"]

    LOOP --> INC["processed_steps += 1"]
    INC --> OK{"step.succeeds?"}
    OK -->|"False"| FAIL["AgentResult<br/>status=failed<br/>dừng vì bước thất bại"]
    OK -->|"True"| NEXT{"còn bước?"}
    NEXT -->|"có"| LOOP
    NEXT -->|"hết vòng"| LIM{"processed &lt; total?"}
    LIM -->|"có"| SLIM["AgentResult<br/>status=step_limit<br/>đạt giới hạn limit bước"]
    LIM -->|"không"| DONE["AgentResult<br/>status=completed<br/>xử lý đủ mọi bước"]

    FAIL --> OUT["CLI in ra:<br/>status=...<br/>completion_reason=..."]
    SLIM --> OUT
    DONE --> OUT

    classDef cli fill:#e8f0fe,stroke:#1a73e8
    classDef data fill:#e6f4ea,stroke:#188038
    classDef loop fill:#fef7e0,stroke:#f9ab00
    classDef stop fill:#fce8e6,stroke:#d93025
    classDef good fill:#e6f4ea,stroke:#188038
    class CLI,S,RD cli
    class F,ST,ENV data
    class VST data
    class RUN,V2,LOOP,INC,OK,NEXT,LIM loop
    class FAIL,SLIM,ERR,ERR2 stop
    class DONE,OUT good
```

## Ba trạng thái kết thúc (stop semantics)

| Trạng thái | Khi nào | `completion_reason` |
|---|---|---|
| `completed` | xử lý đủ mọi bước thành công | "Hoàn thành vì tất cả các bước... được xử lý thành công." |
| `failed` | một bước có `succeeds=False` → dừng **ngay lập tức**, các bước sau không bị xử lý | kèm `failure_reason` của bước |
| `step_limit` | `max_steps` < tổng số bước | "Dừng vì đạt giới hạn N bước..." |

Bất biến: mỗi vòng chỉ tính một bước **sau khi đã xử lý**; `max_steps=0` → không xử lý bước nào; `AgentResult` là dataclass bất biến (`frozen=True`) nên caller phân biệt được số bước đã xong mà không cần đọc text.

# Sơ đồ nguyên lý hoạt động — bản Beta (Laplace's Demon full-stack)

> Nguồn: mã thật của `beta/laplace/` — `__main__.py`, `agent/orchestrator.py`, `agent/strategies.py`, `llm/`, `tools/`, `services/`, web/bot.
> Beta là hệ thống đầy đủ: Telegram + FastAPI + SQLite + LLM 8 hãng + 6 tool + trace + eval, đối chiếu để thấy tầm nhìn sản phẩm mà bản triển khai đang đi tới từng sprint.

## Nguyên lý tóm tắt

1. `python -m laplace` khởi động **một tiến trình duy nhất**: web FastAPI (API + trace viewer, cổng 8000) + APScheduler; nếu có Telegram token thì bot polling chạy song song trong cùng event loop.
2. Mọi yêu cầu người dùng (Telegram hoặc API) → tạo row `Task` trong SQLite → gọi `orchestrator.run_task(task_id)` qua `asyncio.to_thread` (sync, không giữ write-lock DB lúc gọi LLM).
3. Orchestrator là **state machine**: `pending → running → CLASSIFY` (LLM chọn route: `direct` / `clarify` / `single_tool` / `multi_step`).
   - `direct`/`clarify` → 1 call LLM → `done`.
   - `single_tool`/`multi_step` → strategy loop (`react` | `plan_execute`): `EXECUTE_STEP → OBSERVE → (NEXT_STEP | REPLAN | AWAIT_CONFIRM | DONE | FAILED)`.
4. Tool ghi (`note_store`, `task_list`, `scheduler`) cần confirm → task dừng ở `awaiting_confirm`; người dùng xác nhận → `resume_task()` dùng **compare-and-set** để chỉ một request được "giành" task, tránh double-execution.
5. Mọi call LLM và tool đều ghi trace (`llm_calls` / `steps`) → Trace Viewer web xem được timeline đầy đủ (tokens, cost, latency).

## Sơ đồ luồng hoạt động

![Sơ đồ nguyên lý bản Beta](so-do-beta.png)

```mermaid
flowchart TD
    U(["Người dùng<br/>Telegram / HTTP"]) --> BOT["bot/ aiogram<br/>handlers + ratelimit"]
    U --> API["web/ FastAPI<br/>Task API"]
    BOT --> SVC["services/tasks.py<br/>tạo Task (pending)"]
    API --> SVC
    SVC --> DB[("SQLite<br/>models.py + db.py<br/>conversations · tasks · steps<br/>llm_calls · notes · todos")]
    DB --> ORCH

    subgraph ORCHBOX["agent/orchestrator.py — state machine (asyncio.to_thread)"]
        ORCH["run_task(task_id)"] --> RUN["status=running<br/>commit sớm — không giữ lock"]
        RUN --> CTX["conversation_context()"]
        CTX --> CLS{"call_structured<br/>RouteDecision<br/>purpose=classify"}
        CLS -->|"direct"| ANS["provider.complete<br/>build_direct_messages"]
        CLS -->|"clarify"| CLA["provider.complete<br/>build_clarify_messages"]
        CLS -->|"single_tool /<br/>multi_step"| STR["get_strategy(react \| plan_execute)<br/>.run(session, task, provider)"]
    end

    ANS --> FIN["finish_task(done)"]
    CLA --> FIN

    subgraph LOOP["strategy loop — EXECUTE → OBSERVE"]
        STR --> EX["EXECUTE_STEP<br/>tools/base.py registry"]
        EX --> CONF{"tool cần<br/>confirm?"}
        CONF -->|"có"| AWAIT["AWAIT_CONFIRM<br/>state_json.pending"]
        AWAIT -->|"user confirm/reject"| RES["resume_task()<br/>compare-and-set<br/>chỉ 1 request thắng"]
        RES --> EX
        CONF -->|"không"| OBS["OBSERVE<br/>kết quả tool"]
        OBS --> DEC{"NEXT_STEP \|<br/>REPLAN \| DONE \| FAILED"}
        DEC -->|"vòng tiếp"| STR
    end

    EX --> TOOLS["Tool Registry — 6 tool<br/>web_search · fetch_page<br/>note_store · task_list<br/>report_builder · scheduler"]
    TOOLS --> OBS

    ANS2["provider.complete"] -.->|"mọi LLM call"| LLM["llm/ — preset registry 8 hãng<br/>openai_provider adapter<br/>token · cost · latency · retry 429<br/>mock provider (offline)"]
    STR -.-> ANS2
    LLM -.->|"record_llm_call"| TRACE["services/trace.py<br/>llm_calls + steps"]
    TRACE --> TV["Trace Viewer (web :8000)<br/>timeline đầy đủ"]
    FIN --> EVAL["evals/ harness<br/>66 case · 2 chiến lược × model"]
    RES --> FIN

    classDef iface fill:#e8f0fe,stroke:#1a73e8
    classDef orch fill:#fef7e0,stroke:#f9ab00
    classDef store fill:#e6f4ea,stroke:#188038
    classDef tool fill:#f3e8fd,stroke:#8430ce
    classDef trace fill:#fce8e6,stroke:#d93025
    class U,BOT,API iface
    class ORCH,RUN,CTX,CLS,STR,EX,OBS,DEC,CONF,AWAIT,RES orch
    class DB,FIN,STORE store
    class TOOLS,LLM tool
    class TRACE,TV,EVAL trace
```

## So sánh nhanh Beta ⇄ Laplace-Demon (triển khai)

| Khía cạnh | Laplace-Demon (Sprint 2) | Beta |
|---|---|---|
| Phạm vi | Agent loop thuần Python, 1 module | Full-stack: Telegram, web, DB, scheduler, eval |
| LLM | Không có (deterministic) | 8 hãng + mock, preset registry |
| Tool | Không có | 6 tool, registry, confirm gating |
| Trạng thái dừng | `completed` / `failed` / `step_limit` | `done` / `failed` / `awaiting_confirm` (+ REPLAN) |
| Trace | Không | `llm_calls` + `steps` → Trace Viewer |
| Persistence | Không | SQLite (SQLAlchemy) |
| Human-in-the-loop | Không | `resume_task` compare-and-set |

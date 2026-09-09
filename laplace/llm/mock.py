"""MockLLM: provider giả lập cho test, CI và chạy dev không cần API key.

Hai chế độ:
- Scripted: truyền ``script`` là list các dict (trả về làm ``parsed``) hoặc
  str (trả về làm ``content``). Mỗi lần gọi ``complete()`` lấy phần tử tiếp
  theo. Test dùng chế độ này để điều khiển chính xác agent.
- Heuristic (không có script): trả cấu trúc tối thiểu dựa trên schema hoặc
  echo nội dung, đủ để chạy toàn tuyến offline.
"""

from typing import Any

from laplace.llm.base import LLMResult


class MockLLM:
    """Provider offline deterministic cho test và development.

    Chế độ scripted phát từng phần tử theo thứ tự; dict trở thành ``parsed``,
    string trở thành ``content``. State script và log các lần gọi nằm trong
    object này, nên mỗi test nên tạo instance riêng; class không gọi mạng và
    không tính cost thật.
    """

    name = "mock"

    def __init__(self, script: list[dict[str, Any] | str] | None = None):
        """Khởi tạo script độc lập (copy) và bộ nhớ log các lần gọi."""
        # Copy script để pop không ảnh hưởng list gốc
        self._script = list(script) if script else None
        self.calls: list[dict[str, Any]] = []  # log để test assert

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Trả response scripted/heuristic theo schema mà không dùng mạng.

        Mọi request được ghi vào ``calls``; script hết phần tử sẽ ném
        ``RuntimeError`` để test phát hiện thiếu kịch bản. Hàm không validate
        schema và không mô phỏng latency; caller vẫn phải xử lý ``parsed``
        như với provider thật.
        """
        # Ghi log request
        self.calls.append({"messages": messages, "json_schema": json_schema})
        # Chế độ scripted: trả lần lượt từng item
        if self._script is not None:
            if not self._script:
                raise RuntimeError("MockLLM script exhausted")
            item = self._script.pop(0)
            if isinstance(item, dict):
                return LLMResult(parsed=item, model="mock", prompt_tokens=10, completion_tokens=10)
            return LLMResult(content=item, model="mock", prompt_tokens=10, completion_tokens=10)

        # Heuristic mode: đủ cho chạy toàn tuyến offline.
        if json_schema is not None:
            props = json_schema.get("properties", {})
            if "action" in props:
                return LLMResult(
                    parsed={"action": "final", "final_answer": "[mock] xong"}, model="mock"
                )
            return LLMResult(parsed={}, model="mock")
        last = messages[-1]["content"] if messages else ""
        return LLMResult(content=f"[mock] {last[:200]}", model="mock")

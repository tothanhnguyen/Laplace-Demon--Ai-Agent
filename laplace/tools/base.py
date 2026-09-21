"""Registry công cụ rút gọn cho Sprint 2: đăng ký, validate Pydantic, thực thi.

Mỗi tool khai báo tên, mô tả (kèm lúc nào nên dùng), Pydantic model cho tham
số và callable. Tham số từ LLM luôn được validate trước khi chạy (S2-07); sai
schema trả lỗi có cấu trúc để tầng agent yêu cầu mô hình tự sửa. Timeout,
retry và phân loại rủi ro thuộc Sprint 4-6, chưa nằm ở đây.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass
class ToolResult:
    """Kết quả thống nhất của một lần chạy tool.

    ``ok=False`` luôn kèm ``error`` giải thích được; ``data`` là payload khi
    thành công. Executor không ném exception ra ngoài boundary này.
    """

    ok: bool
    data: Any = None
    error: str = ""


@dataclass
class ToolSpec:
    """Metadata và callable runtime của một tool đã đăng ký."""

    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[..., Any] = field(repr=False, default=None)  # type: ignore[assignment]

    def llm_spec(self) -> dict[str, Any]:
        """Serialize spec thành dict cho system prompt: tên, mô tả, schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


_REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, args_model: type[BaseModel]):
    """Decorator đăng ký callable cùng metadata vào registry toàn cục."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[name] = ToolSpec(
            name=name, description=description, args_model=args_model, run=func
        )
        return func

    return decorator


def get_tool(name: str) -> ToolSpec | None:
    """Tra một tool theo tên, trả ``None`` nếu chưa đăng ký."""
    return _REGISTRY.get(name)


def all_tools() -> dict[str, ToolSpec]:
    """Trả snapshot nông của registry hiện tại để caller liệt kê tool."""
    return dict(_REGISTRY)


def specs_for_llm() -> list[dict[str, Any]]:
    """Chuyển toàn bộ registry thành danh sách schema đưa vào system prompt."""
    return [spec.llm_spec() for spec in _REGISTRY.values()]


def load_builtin_tools() -> None:
    """Import các module tool built-in để decorator tự đăng ký."""
    from laplace.tools import read_file  # noqa: F401


def execute(name: str, params: dict[str, Any]) -> ToolResult:
    """Thực thi tool qua boundary thống nhất: validate schema rồi chạy.

    Tool không tồn tại hoặc tham số sai schema trả ``ToolResult`` lỗi với
    thông điệp đủ để LLM tự sửa; exception khi chạy được bắt lại thành lỗi
    có cấu trúc thay vì làm vỡ vòng lặp agent.
    """
    spec = get_tool(name)
    if spec is None:
        valid = ", ".join(sorted(_REGISTRY)) or "(trống)"
        return ToolResult(ok=False, error=f"Tool '{name}' không tồn tại. Tool hợp lệ: {valid}.")
    try:
        parsed = spec.args_model.model_validate(params)
    except ValidationError as e:
        return ToolResult(ok=False, error=f"Tham số sai schema cho tool '{name}': {e}")
    try:
        return ToolResult(ok=True, data=spec.run(parsed))
    except Exception as e:  # boundary: lỗi tool không được làm vỡ agent
        return ToolResult(ok=False, error=f"Tool '{name}' lỗi khi chạy: {e}")

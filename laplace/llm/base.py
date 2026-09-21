"""Provider abstraction cho lớp LLM.

Mọi provider trả về ``LLMResult``. Khi caller truyền ``json_schema`` (dict JSON
Schema, thường sinh từ ``Model.model_json_schema()``), provider cố gắng trả
``parsed`` là dict hợp lệ theo schema đó; caller chịu trách nhiệm validate lại
bằng Pydantic và yêu cầu mô hình tự sửa nếu sai.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class LLMResult:
    """Kết quả chuẩn hóa mà mọi LLM provider trả về cho agent.

    ``content`` là text thô, ``parsed`` là dict JSON khi có schema; token,
    cost, latency và model phục vụ ghi bảng ``llm_calls`` (S2-06).
    """

    content: str | None = None
    parsed: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Hợp đồng tối thiểu để agent dùng được nhiều nhà cung cấp LLM.

    Protocol giữ agent độc lập với SDK cụ thể; provider thật và provider mock
    đều chỉ cần cung cấp ``name`` cùng ``complete``.
    """

    name: str

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Gọi LLM đồng bộ với messages dạng ``[{role, content}]``.

        Implementer phải trả ``LLMResult`` và có thể cố gắng tạo ``parsed``
        theo JSON schema; protocol không áp đặt SDK, retry hay validation.
        """
        ...


class MissingAPIKeyError(RuntimeError):
    """Thiếu API key cho provider đã chọn; message kèm URL trang lấy key."""


def resolve_provider_config(name: str) -> tuple[Any, str | None, str]:
    """Trả ``(preset, api_key, model)`` theo registry và settings hiện tại.

    Thứ tự chọn model: ``LAPLACE_LLM_MODEL`` (override chung) > model mặc định
    của preset. Hàm chỉ đọc cấu hình, không gọi API hay ghi file.
    """
    from laplace.config import Settings
    from laplace.llm.presets import PRESETS

    preset = PRESETS[name]
    settings = Settings()
    api_key = getattr(settings, preset.settings_field, "") or None
    model = settings.llm_model or preset.default_model
    return preset, api_key, model


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory chọn provider theo preset registry (S2-05).

    ``mock`` hoặc tên lạ trả ``MockLLM`` chạy offline; còn lại trả
    ``OpenAIProvider`` với base_url/model/bảng giá từ preset. Thiếu key thì ném
    ``MissingAPIKeyError`` kèm URL trang lấy key của đúng hãng, trước khi có
    bất kỳ request mạng nào.
    """
    from laplace.config import Settings
    from laplace.llm.presets import PRESETS, missing_key_message

    name = name or Settings().llm_provider
    if name not in PRESETS:  # mock và mọi tên lạ -> MockLLM
        from laplace.llm.mock import MockLLM

        return MockLLM()

    preset, api_key, model = resolve_provider_config(name)
    if not api_key:
        raise MissingAPIKeyError(missing_key_message(preset))

    from laplace.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        api_key=api_key,
        model=model,
        base_url=preset.base_url,
        name=name,
        pricing=preset.pricing,
    )

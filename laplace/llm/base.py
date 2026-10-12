"""Provider abstraction cho lớp LLM.

Mọi provider trả về ``LLMResult``. Khi caller truyền ``json_schema`` (dict JSON
Schema, thường sinh từ ``Model.model_json_schema()``), provider cố gắng trả
``parsed`` là dict hợp lệ theo schema đó; caller chịu trách nhiệm validate lại
bằng Pydantic và yêu cầu mô hình tự sửa nếu sai.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class BudgetStop(RuntimeError):
    """Terminal execution stop; never include request or provider error text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Execution budget stopped.")


class AttemptObserver(Protocol):
    """Durably reserve before send and reconcile every physical attempt."""

    def begin(
        self,
        *,
        call_group_id: str,
        attempt_index: int,
        provider: str,
        requested_model: str,
        payload: dict[str, Any],
        pricing: Mapping[str, tuple[float, float]],
        pricing_version: str,
    ) -> int: ...

    def finish(
        self,
        attempt_id: int,
        *,
        returned_model: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        outcome: str,
    ) -> None: ...


@dataclass(frozen=True)
class CallOptions:
    """Required limits and accounting callbacks for one logical LLM call."""

    max_completion_tokens: int
    attempt_timeout_seconds: float
    remaining_seconds: Callable[[], float]
    check: Callable[[], None]
    observer: AttemptObserver


@dataclass
class LLMResult:
    """Kết quả chuẩn hóa mà mọi LLM provider trả về cho agent.

    ``content`` là text thô, ``parsed`` là dict JSON khi có schema. Metrics
    are reports only: the injected observer owns durable physical-attempt
    accounting; ``attempt_ids`` identifies rows already written.
    """

    content: str | None = None
    parsed: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    attempt_ids: tuple[int, ...] = ()
    usage_known: bool = True


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
        options: CallOptions,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Gọi LLM đồng bộ với messages dạng ``[{role, content}]``.

        Implementers must honor ``options`` and reserve/finish every physical
        attempt through its observer. Schema validation remains the caller's
        responsibility; a terminal budget stop must never become a free result.
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

    if name not in PRESETS:
        raise ValueError("Unknown LLM provider configuration.")
    preset = PRESETS[name]
    settings = Settings()
    api_key = getattr(settings, preset.settings_field, "") or None
    model = settings.llm_model or preset.default_model
    if model not in preset.pricing:
        raise ValueError("Unsupported LLM model configuration.")
    return preset, api_key, model


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory chọn provider theo preset registry (S2-05).

    ``mock`` must be selected explicitly. Unknown provider/model/profile fails
    before network access. Paid and known-zero presets carry exact prices and
    an output-cap profile; missing credentials raise ``MissingAPIKeyError``.
    """
    from laplace.config import Settings
    from laplace.llm.presets import PRESETS, missing_key_message

    name = name or Settings().llm_provider
    if name == "mock":
        from laplace.llm.mock import MockLLM

        return MockLLM()
    if name not in PRESETS:
        raise ValueError("Unknown LLM provider configuration.")

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
        output_cap_parameter=preset.output_cap_parameter,
        pricing_version=preset.pricing_version,
    )

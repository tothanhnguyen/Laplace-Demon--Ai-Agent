"""OpenAIProvider: adapter cho mọi API tương thích OpenAI Chat Completions.

Implement protocol ``LLMProvider`` (laplace/llm/base.py). Khi caller truyền
``json_schema``, provider dùng ``response_format=json_object`` và parse content
thành dict cho ``LLMResult.parsed``; parse lỗi thì ``parsed=None`` và content
giữ nguyên để tầng trên tự xử lý retry (self-correction).
"""

import json
import logging
import re
import time
from typing import Any

from laplace.llm.base import LLMResult

logger = logging.getLogger(__name__)

# Model đã cảnh báo "không có giá" — chỉ warning 1 lần mỗi model cho đỡ ồn log.
_WARNED_UNKNOWN_MODELS: set[str] = set()

RATE_LIMIT_MAX_RETRIES = 5


def _retry_delay_from(error: Exception, attempt: int) -> float:
    """Tính thời gian chờ cho 429 từ hint của provider hoặc backoff tăng dần.

    Output bị giới hạn tối đa 90 giây để tránh ngủ vô hạn. Hàm chỉ parse text
    lỗi, không sleep; vòng ``complete`` mới áp dụng kết quả này.
    """
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(error))
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return min(15.0 * (attempt + 1), 90.0)


def _compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing: dict[str, tuple[float, float]] | None,
) -> float:
    """Tính cost USD từ bảng giá preset theo công thức USD/1M token.

    Model không có giá trả 0.0 và log warning duy nhất một lần mỗi model
    (token vẫn được ghi để theo dõi). Hàm không gọi mạng.
    """
    price = (pricing or {}).get(model)
    if price is None:
        if model not in _WARNED_UNKNOWN_MODELS:
            _WARNED_UNKNOWN_MODELS.add(model)
            logger.warning(
                "Không có giá cho model '%s' trong bảng giá — tính cost 0.0.", model
            )
        return 0.0
    input_per_1m, output_per_1m = price
    return (prompt_tokens * input_per_1m + completion_tokens * output_per_1m) / 1_000_000


class OpenAIProvider:
    """Adapter cho API tương thích OpenAI Chat Completions.

    Cùng một lớp phục vụ OpenAI, Gemini, Groq qua ``base_url``; lớp này là
    ranh giới SDK, retry rate-limit, parse JSON và tính cost. Nó không validate
    Pydantic schema, nên caller phải xử lý ``parsed=None`` hoặc object sai
    schema ở tầng trên.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        name: str = "openai",
        pricing: dict[str, tuple[float, float]] | None = None,
    ):
        """Khởi tạo SDK client và cấu hình model/giá; không gọi mạng."""
        if not api_key:
            raise RuntimeError(
                f"Thiếu API key cho provider '{name}': đặt LAPLACE_{name.upper()}_API_KEY "
                "trong .env (hoặc chuyển LAPLACE_LLM_PROVIDER=mock để chạy không cần key)."
            )
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.name = name
        self._pricing = pricing

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Gọi chat completion, retry 429/backend-lỗi-format và parse JSON.

        Khi có schema, provider thêm instruction system và thử
        ``response_format=json_object``; backend từ chối format sẽ được thử lại
        không có tham số đó. Rate-limit được retry tối đa
        ``RATE_LIMIT_MAX_RETRIES`` với backoff, lỗi khác truyền ra. Output giữ
        content thô, ``parsed`` chỉ là dict JSON parse được, cùng
        token/model/cost/latency phục vụ ghi ``llm_calls`` (S2-06).
        """
        # Không dùng strict structured-outputs: schema Pydantic mặc định không
        # thỏa điều kiện strict của OpenAI -> request sẽ bị 400. Thay vào đó:
        # json_object mode + schema nhúng vào system message; tầng trên validate
        # bằng Pydantic và yêu cầu mô hình tự sửa khi sai.
        msgs = list(messages)
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs}
        if json_schema is not None:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "Respond with a single JSON object that conforms to this "
                        "JSON Schema. No prose, no markdown fences, JSON only.\n"
                        + json.dumps(json_schema)
                    ),
                }
            )
            kwargs["response_format"] = {"type": "json_object"}

        from openai import BadRequestError, RateLimitError

        start = time.monotonic()
        response = None
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
                break
            except BadRequestError:
                # Một số backend tương thích OpenAI không nhận response_format:
                # bỏ đi và dựa vào schema trong system message.
                if "response_format" not in kwargs:
                    raise
                kwargs.pop("response_format")
            except RateLimitError as e:
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    raise
                time.sleep(_retry_delay_from(e, attempt))
        assert response is not None
        latency_ms = int((time.monotonic() - start) * 1000)

        content: str | None = response.choices[0].message.content
        parsed: dict[str, Any] | None = None
        if json_schema is not None and content:
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    parsed = data
            except (json.JSONDecodeError, ValueError):
                parsed = None  # content giữ nguyên, tầng trên tự xử lý retry

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        model = response.model or self.model

        return LLMResult(
            content=content,
            parsed=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=_compute_cost(model, prompt_tokens, completion_tokens, self._pricing),
            latency_ms=latency_ms,
            model=model,
            extra={"finish_reason": response.choices[0].finish_reason},
        )

"""Preset các nhà cung cấp LLM có endpoint tương thích OpenAI.

Mỗi preset đủ thông tin để "dán key là chạy": base_url, model mặc định, URL
trang lấy key (in ra khi thiếu key) và bảng giá USD/1M token. File tự đứng một
mình (không import module khác trong package) để test import rẻ; factory
``get_provider`` (laplace/llm/base.py) đọc registry này thay cho if/else cứng.
"""

from dataclasses import dataclass, field

Pricing = dict[str, tuple[float, float]]


@dataclass(frozen=True)
class ProviderPreset:
    """Mô tả một nhà cung cấp: endpoint, model mặc định và bảng giá."""

    name: str  # tên dùng trong LAPLACE_LLM_PROVIDER
    label: str  # tên hiển thị cho người dùng
    base_url: str | None  # None = mặc định SDK OpenAI (api.openai.com)
    default_model: str
    key_url: str  # trang lấy API key
    pricing: Pricing = field(default_factory=dict)

    @property
    def env_key(self) -> str:
        """Tên biến environment chứa API key của preset này."""
        return f"LAPLACE_{self.name.upper()}_API_KEY"

    @property
    def settings_field(self) -> str:
        """Tên field API key tương ứng trong ``Settings``."""
        return f"{self.name}_api_key"


PRESETS: dict[str, ProviderPreset] = {
    p.name: p
    for p in [
        ProviderPreset(
            name="gemini",
            label="Google Gemini (AI Studio)",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            default_model="gemini-3.6-flash",
            key_url="https://aistudio.google.com/apikey",
            pricing={
                "gemini-3.6-flash": (0.30, 2.50),
                # Gemini 3.5 Flash-Lite Standard: $0.30/$2.50 per 1M tokens
                # (ai.google.dev/gemini-api/docs/pricing, tra cứu 2026-09-11)
                "gemini-3.5-flash-lite": (0.30, 2.50),
                "gemini-2.5-flash": (0.30, 2.50),
                "gemini-2.5-flash-lite": (0.10, 0.40),
                "gemini-2.5-pro": (1.25, 10.00),
            },
        ),
        ProviderPreset(
            name="openai",
            label="OpenAI",
            base_url=None,  # dùng mặc định SDK: https://api.openai.com/v1
            default_model="gpt-4o-mini",
            key_url="https://platform.openai.com/api-keys",
            pricing={
                "gpt-4o": (2.50, 10.00),
                "gpt-4o-mini": (0.15, 0.60),
                "gpt-4.1": (2.00, 8.00),
                "gpt-4.1-mini": (0.40, 1.60),
                "gpt-4.1-nano": (0.10, 0.40),
            },
        ),
        ProviderPreset(
            name="bai",
            label="B.AI",
            base_url="https://api.b.ai/v1",
            default_model="gpt-5.2",
            key_url="https://chat.b.ai/chat",
            pricing={
                # B.AI standard pricing, USD/1M tokens.
                # docs.b.ai/llmservice/pricing-and-usage/, tra cứu 2026-09-16.
                "gpt-5.2": (1.75, 14.00),
            },
        ),
        ProviderPreset(
            name="router9",
            label="9Router (local)",
            base_url="http://127.0.0.1:20128/v1",
            default_model="cx/gpt-5.6-sol",
            key_url="http://127.0.0.1:20128/dashboard",
            pricing={
                # Route qua subscription Codex: chi phí biên mỗi request là 0;
                # phí thuê bao và quota không thể quy đổi chính xác theo token.
                "cx/gpt-5.6-sol": (0.0, 0.0),
                "gpt-5.6-sol": (0.0, 0.0),
            },
        ),
        ProviderPreset(
            name="groq",
            label="Groq",
            base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
            key_url="https://console.groq.com/keys",
            pricing={
                "llama-3.3-70b-versatile": (0.59, 0.79),
                "llama-3.1-8b-instant": (0.05, 0.08),
            },
        ),
    ]
}


def mask_key(key: str | None) -> str:
    """Che API key khi hiển thị, không bao giờ trả toàn bộ secret."""
    if not key:
        return "(chưa đặt)"
    prefix = key[:6] if len(key) > 6 else key[:2]
    return prefix + "..."


def missing_key_message(preset: ProviderPreset) -> str:
    """Tạo hướng dẫn thiếu key đúng theo provider preset."""
    return (
        f"Thiếu API key cho provider '{preset.name}' ({preset.label}). "
        f"Lấy key tại {preset.key_url} rồi đặt {preset.env_key} trong .env, "
        "hoặc chuyển LAPLACE_LLM_PROVIDER=mock để chạy không cần key."
    )

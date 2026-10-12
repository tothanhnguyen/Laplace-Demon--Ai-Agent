# Caveman cho Hermes, OpenClaw và Codex

## Mục tiêu

Giảm output token và lời thừa trên ba agent, không làm mất nội dung kỹ thuật, code, command hay error string.

## Thiết kế đã duyệt

- Hermes dùng mức `lite`: câu đầy đủ, bỏ filler/hedging để plan vẫn rõ.
- OpenClaw dùng mức `full`: fragment ngắn, giữ đầy đủ substance khi code/test/review.
- Codex dùng mức `full` thông qua installer chính chủ.
- Auto-Clarity luôn bật: trả lời đầy đủ khi có cảnh báo bảo mật, thao tác không thể hoàn tác hoặc chuỗi bước dễ hiểu sai.
- Chỉ giảm output. Không tuyên bố giảm reasoning token hay input context.

## Tích hợp

### OpenClaw

Dùng Caveman installer `v1.9.0` với `--only openclaw`. Installer tạo:

- `~/.openclaw/workspace/skills/caveman/SKILL.md`
- block có marker `caveman-begin`/`caveman-end` trong `~/.openclaw/workspace/SOUL.md`

### Codex

Dùng cùng installer với `--only codex`. Installer gọi skill manager để cài skill Caveman vào profile Codex.

### Hermes

Caveman chưa hỗ trợ Hermes native. Adapter gồm:

- bản sao skill tại `~/.hermes/skills/productivity/caveman/SKILL.md`
- block có marker riêng trong `~/.hermes/SOUL.md`, đặt mức mặc định `lite`

## An toàn và rollback

- Backup `~/.hermes/SOUL.md` trước khi sửa.
- Marker giúp cài lại idempotent và gỡ sạch.
- OpenClaw/Codex dùng uninstaller chính chủ.
- Không cài hooks phụ hoặc MCP shrink.

## Kiểm chứng

- OpenClaw liệt kê skill `caveman`.
- Codex có skill Caveman trong profile được phát hiện.
- Hermes có skill và marker `lite` trong SOUL.
- Không có marker trùng khi chạy lại.


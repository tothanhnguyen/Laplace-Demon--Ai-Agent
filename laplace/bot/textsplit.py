"""Cắt văn bản dài thành các đoạn vừa giới hạn message của Telegram.

Telegram từ chối message quá 4096 ký tự, nên câu trả lời dài phải gửi thành
nhiều phần. Điểm cắt ưu tiên ranh giới dòng rồi tới khoảng trắng để không vỡ
từ giữa chừng; nội dung được giữ nguyên tuyệt đối — ghép các đoạn lại bằng
đúng chuỗi ban đầu. Module thuần Python, không phụ thuộc aiogram.
"""

TELEGRAM_MESSAGE_LIMIT = 4096


def _cut_index(chunk: str) -> int:
    """Chọn vị trí cắt trong ``chunk`` đã đầy: sau dòng, sau khoảng trắng, hoặc cứng.

    Trả về chỉ số > 0 để đoạn đầu không bao giờ rỗng; ký tự ngắt (newline hay
    khoảng trắng) nằm cuối đoạn trước nên ghép lại vẫn ra nguyên văn.
    """
    newline = chunk.rfind("\n")
    if newline > 0:
        return newline + 1
    for i in range(len(chunk) - 1, 0, -1):
        if chunk[i].isspace():
            return i + 1
    return len(chunk)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Chia ``text`` thành danh sách đoạn, mỗi đoạn dài tối đa ``limit`` ký tự.

    Chuỗi rỗng trả về danh sách rỗng; mọi đoạn trả về đều khác rỗng và
    ``"".join(kết quả) == text``.
    """
    if limit < 1:
        raise ValueError("limit phải >= 1")
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _cut_index(rest[:limit])
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks

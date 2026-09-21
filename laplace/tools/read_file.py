"""Tool đọc file văn bản trong thư mục làm việc — công cụ mẫu từ Sprint 1.

Chỉ phục vụ minh họa cơ chế gọi tool có kiểm tra dữ liệu: đường dẫn bị giới
hạn trong thư mục làm việc hiện tại (chặn ``..`` thoát ra ngoài) và dung lượng
đọc bị chặn trần để kết quả không làm phình context.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from laplace.tools.base import tool

MAX_BYTES = 64 * 1024  # trần đọc: 64KB đủ cho file văn bản mẫu


class ReadFileArgs(BaseModel):
    """Tham số của tool read_file, validate trước khi chạy."""

    path: str = Field(min_length=1, description="Đường dẫn file, tương đối với thư mục làm việc")


@tool(
    name="read_file",
    description=(
        "Đọc nội dung một file văn bản trong thư mục làm việc. Dùng khi người "
        "dùng hỏi về nội dung file cụ thể; không dùng cho URL hay thư mục."
    ),
    args_model=ReadFileArgs,
)
def read_file(args: ReadFileArgs) -> str:
    """Đọc file văn bản với giới hạn đường dẫn và dung lượng.

    Đường dẫn resolve xong phải nằm trong thư mục làm việc; file thiếu hoặc
    không phải UTF-8 sẽ ném ValueError để executor gói thành lỗi giải thích
    được cho LLM.
    """
    root = Path.cwd().resolve()
    target = (root / args.path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("đường dẫn nằm ngoài thư mục làm việc")
    if not target.is_file():
        raise ValueError(f"không tìm thấy file '{args.path}'")
    data = target.read_bytes()[:MAX_BYTES]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("file không phải văn bản UTF-8") from e

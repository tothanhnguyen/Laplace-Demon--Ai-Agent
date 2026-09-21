"""Giới hạn tần suất theo user bằng token bucket (in-memory, thuần Python).

Một user gửi tin liên tục sẽ chiếm hết lượt gọi LLM và chặn người dùng khác.
Bucket của mỗi user chứa tối đa ``DEFAULT_MAX_REQUESTS`` token, nạp lại đều
theo thời gian với tốc độ ``DEFAULT_MAX_REQUESTS / DEFAULT_WINDOW_S`` token
mỗi giây. Trạng thái nằm trong RAM: restart process là reset, đủ dùng cho bot
chạy một tiến trình. Module không phụ thuộc aiogram nên test được độc lập.
"""

import math
import threading
import time
from collections.abc import Callable

DEFAULT_MAX_REQUESTS = 5
DEFAULT_WINDOW_S = 60.0


class TokenBucket:
    """Token bucket thread-safe theo ``user_id``.

    Handler chạy trên event loop nhưng callback tiến độ chạy ở thread khác,
    nên mọi truy cập state đều qua lock. Constructor validate quota/cửa sổ và
    nhận ``clock`` tùy biến để test không phải chờ thời gian thật.
    """

    def __init__(
        self,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests phải >= 1")
        if window_s <= 0:
            raise ValueError("window_s phải > 0")
        self.max_requests = max_requests
        self.window_s = window_s
        self._rate = max_requests / window_s  # token nạp lại mỗi giây
        self._clock = clock
        self._buckets: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _refill(self, user_id: int, now: float) -> float:
        """Nạp token tích lũy từ lần truy cập trước và trả về số token hiện có."""
        tokens, last = self._buckets.get(user_id, (float(self.max_requests), now))
        tokens = min(float(self.max_requests), tokens + (now - last) * self._rate)
        self._buckets[user_id] = (tokens, now)
        return tokens

    def allow(self, user_id: int) -> bool:
        """True nếu yêu cầu được nhận (trừ một token); False nếu đã hết lượt."""
        now = self._clock()
        with self._lock:
            tokens = self._refill(user_id, now)
            if tokens < 1.0:
                return False
            self._buckets[user_id] = (tokens - 1.0, now)
            return True

    def retry_after(self, user_id: int) -> float:
        """Số giây cần chờ trước khi có lượt tiếp theo (0.0 = dùng được ngay).

        Kết quả làm tròn lên giây nguyên để lời nhắn với người dùng lịch sự và
        không hứa sớm hơn thực tế.
        """
        now = self._clock()
        with self._lock:
            tokens = self._refill(user_id, now)
            if tokens >= 1.0:
                return 0.0
            return float(math.ceil((1.0 - tokens) / self._rate))

    def reset(self, user_id: int | None = None) -> None:
        """Xóa trạng thái của một user (hoặc tất cả) — dùng trong test/vận hành."""
        with self._lock:
            if user_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(user_id, None)

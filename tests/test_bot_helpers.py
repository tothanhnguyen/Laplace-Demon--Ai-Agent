"""Kiểm thử các tiện ích thuần Python của bot: rate limit và cắt message."""

import pytest

from laplace.bot.ratelimit import DEFAULT_MAX_REQUESTS, TokenBucket
from laplace.bot.textsplit import split_message


class FakeClock:
    """Đồng hồ tự điều khiển để test không phải chờ thời gian thật."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_bucket_allows_initial_burst_then_blocks() -> None:
    clock = FakeClock()
    bucket = TokenBucket(max_requests=5, window_s=60.0, clock=clock)

    assert all(bucket.allow(1) for _ in range(5))
    assert not bucket.allow(1)


def test_bucket_isolates_users() -> None:
    clock = FakeClock()
    bucket = TokenBucket(max_requests=1, window_s=60.0, clock=clock)

    assert bucket.allow(1)
    assert not bucket.allow(1)
    assert bucket.allow(2)


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(max_requests=5, window_s=60.0, clock=clock)
    for _ in range(5):
        bucket.allow(1)

    # Tốc độ nạp 5 token / 60s: sau 12 giây có đúng một lượt mới.
    clock.advance(11.9)
    assert not bucket.allow(1)
    clock.advance(0.2)
    assert bucket.allow(1)
    assert not bucket.allow(1)


def test_retry_after_reports_polite_whole_seconds() -> None:
    clock = FakeClock()
    bucket = TokenBucket(max_requests=5, window_s=60.0, clock=clock)

    assert bucket.retry_after(1) == 0.0
    for _ in range(5):
        bucket.allow(1)
    # Hết token: cần 12 giây cho token kế tiếp, làm tròn lên giây nguyên.
    assert bucket.retry_after(1) == 12.0
    clock.advance(3.5)
    assert bucket.retry_after(1) == 9.0
    clock.advance(9.0)  # tổng 12.5s > 12s, tránh so sánh float đúng tại biên
    assert bucket.retry_after(1) == 0.0


def test_bucket_reset_clears_state() -> None:
    clock = FakeClock()
    bucket = TokenBucket(max_requests=1, window_s=60.0, clock=clock)
    bucket.allow(1)
    assert not bucket.allow(1)

    bucket.reset(1)
    assert bucket.allow(1)


def test_bucket_validates_arguments() -> None:
    with pytest.raises(ValueError):
        TokenBucket(max_requests=0)
    with pytest.raises(ValueError):
        TokenBucket(window_s=0)


def test_default_quota_is_five_per_minute() -> None:
    assert DEFAULT_MAX_REQUESTS == 5


def test_split_short_text_is_unchanged() -> None:
    assert split_message("xin chào") == ["xin chào"]


def test_split_empty_text_returns_no_chunks() -> None:
    assert split_message("") == []


def test_split_prefers_line_boundaries() -> None:
    text = "dòng một\ndòng hai\ndòng ba"
    chunks = split_message(text, limit=12)

    assert chunks == ["dòng một\n", "dòng hai\n", "dòng ba"]
    assert "".join(chunks) == text


def test_split_falls_back_to_whitespace() -> None:
    text = "một hai ba bốn năm"
    chunks = split_message(text, limit=8)

    assert "".join(chunks) == text
    assert all(chunks)
    assert all(len(chunk) <= 8 for chunk in chunks)
    # Không chunk nào vỡ giữa từ: mỗi chunk kết thúc bằng khoảng trắng trừ chunk cuối.
    assert all(chunk.endswith(" ") for chunk in chunks[:-1])


def test_split_hard_cuts_unbroken_text() -> None:
    text = "a" * 25
    chunks = split_message(text, limit=10)

    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


def test_split_roundtrip_preserves_content() -> None:
    text = ("từ " * 500 + "\n") * 5  # dài hơn 4096 ký tự, có cả dòng lẫn khoảng trắng
    chunks = split_message(text)

    assert "".join(chunks) == text
    assert all(chunks)
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError):
        split_message("abc", limit=0)

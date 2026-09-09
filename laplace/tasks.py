"""Dữ liệu nhiệm vụ mẫu dùng để minh họa vòng lặp Agent."""

from laplace.agent import AgentTask, TaskStep


def successful_sample() -> AgentTask:
    """Tạo nhiệm vụ mẫu có toàn bộ bước hoàn thành thành công."""
    return AgentTask(
        name="Chuẩn bị báo cáo Sprint 01",
        steps=(
            TaskStep("Thu thập yêu cầu"),
            TaskStep("Kiểm tra chất lượng"),
            TaskStep("Tổng hợp kết quả"),
        ),
    )


def failed_sample() -> AgentTask:
    """Tạo nhiệm vụ mẫu dừng tại bước kiểm tra thất bại."""
    return AgentTask(
        name="Kiểm tra bản phát hành",
        steps=(
            TaskStep("Đọc cấu hình"),
            TaskStep("Chạy kiểm tra", succeeds=False, failure_reason="phát hiện lỗi"),
            TaskStep("Tạo báo cáo", succeeds=True),
        ),
    )

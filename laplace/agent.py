"""Vòng lặp Agent thuần Python cho các nhiệm vụ mẫu của dự án."""

from dataclasses import dataclass
from typing import Literal

AgentStatus = Literal["completed", "failed", "step_limit"]


@dataclass(frozen=True)
class TaskStep:
    """Một bước dữ liệu mô tả kết quả cố định của nhiệm vụ mẫu."""

    name: str
    succeeds: bool = True
    failure_reason: str = ""

    def __post_init__(self) -> None:
        """Từ chối bước không có tên để kết quả luôn giải thích được."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("step name must not be empty")
        if not isinstance(self.succeeds, bool):
            raise TypeError("step succeeds must be a bool")
        if not isinstance(self.failure_reason, str):
            raise TypeError("step failure_reason must be a string")
        if not self.succeeds and self.failure_reason and not self.failure_reason.strip():
            raise ValueError("failure reason must not be whitespace only")
        if self.succeeds and self.failure_reason:
            raise ValueError("successful step cannot have a failure reason")


@dataclass(frozen=True)
class AgentTask:
    """Nhiệm vụ có tên và danh sách bước được xử lý theo đúng thứ tự."""

    name: str
    steps: tuple[TaskStep, ...]

    def __post_init__(self) -> None:
        """Kiểm tra tên nhiệm vụ trước khi đưa vào vòng lặp."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("task name must not be empty")
        if not isinstance(self.steps, tuple):
            raise TypeError("steps must be a tuple")
        if not all(isinstance(step, TaskStep) for step in self.steps):
            raise TypeError("steps must contain only TaskStep values")


@dataclass(frozen=True)
class AgentResult:
    """Kết quả có cấu trúc để caller biết Agent dừng ở đâu và vì sao."""

    status: AgentStatus
    processed_steps: int
    total_steps: int
    completion_reason: str

    def __post_init__(self) -> None:
        """Bảo vệ contract kết quả kể cả khi caller tự tạo object trực tiếp."""
        if not isinstance(self.status, str) or self.status not in {
            "completed",
            "failed",
            "step_limit",
        }:
            raise ValueError("status must be completed, failed, or step_limit")
        if not isinstance(self.completion_reason, str) or not self.completion_reason.strip():
            raise ValueError("completion_reason must not be empty")
        if (
            not isinstance(self.processed_steps, int)
            or isinstance(self.processed_steps, bool)
            or self.processed_steps < 0
        ):
            raise ValueError("processed_steps must be a non-negative integer")
        if (
            not isinstance(self.total_steps, int)
            or isinstance(self.total_steps, bool)
            or self.total_steps < 0
        ):
            raise ValueError("total_steps must be a non-negative integer")
        if self.processed_steps > self.total_steps:
            raise ValueError("processed_steps cannot exceed total_steps")


class Agent:
    """Chạy nhiệm vụ mẫu tuần tự với giới hạn bước rõ ràng và deterministic."""

    def __init__(self, max_steps: int | None = None) -> None:
        """Tạo Agent với giới hạn tùy chọn để bảo vệ vòng lặp khỏi chạy vô hạn."""
        if max_steps is not None and (
            not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 0
        ):
            raise ValueError("max_steps must be a non-negative integer")
        self.max_steps = max_steps

    def run(self, task: AgentTask) -> AgentResult:
        """Xử lý task đến khi thành công, gặp lỗi hoặc chạm giới hạn bước.

        Mỗi bước chỉ được tính sau khi được xử lý; vì vậy kết quả phân biệt được
        bước đã hoàn thành với bước lỗi và không bao giờ xử lý phần còn lại.
        """
        if not isinstance(task, AgentTask):
            raise TypeError("task must be an AgentTask")

        total_steps = len(task.steps)
        limit = self.max_steps if self.max_steps is not None else total_steps
        processed_steps = 0

        for step in task.steps[:limit]:
            processed_steps += 1
            if not step.succeeds:
                detail = step.failure_reason or f"step '{step.name}' failed"
                return AgentResult(
                    "failed",
                    processed_steps,
                    total_steps,
                    f"Dừng vì bước '{step.name}' thất bại: {detail}",
                )

        if processed_steps < total_steps:
            return AgentResult(
                "step_limit",
                processed_steps,
                total_steps,
                f"Dừng vì đạt giới hạn {limit} bước trước khi hoàn thành nhiệm vụ.",
            )

        return AgentResult(
            "completed",
            processed_steps,
            total_steps,
            "Hoàn thành vì tất cả các bước của nhiệm vụ đã được xử lý thành công.",
        )
    



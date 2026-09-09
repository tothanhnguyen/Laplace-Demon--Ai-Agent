"""Kiểm thử trạng thái dừng của vòng lặp Agent."""

import pytest

from laplace.agent import Agent, AgentResult, AgentTask, TaskStep
from laplace.tasks import failed_sample, successful_sample


def test_successful_task_explains_completion() -> None:
    result = Agent().run(successful_sample())

    assert result.status == "completed"
    assert result.processed_steps == 3
    assert result.total_steps == 3
    assert "tất cả các bước" in result.completion_reason


def test_failed_task_stops_at_failing_step() -> None:
    result = Agent().run(failed_sample())

    assert result.status == "failed"
    assert result.processed_steps == 2
    assert "Chạy kiểm tra" in result.completion_reason
    assert "phát hiện lỗi" in result.completion_reason


def test_step_limit_stops_before_remaining_steps() -> None:
    result = Agent(max_steps=2).run(successful_sample())

    assert result.status == "step_limit"
    assert result.processed_steps == 2
    assert "giới hạn 2 bước" in result.completion_reason


@pytest.mark.parametrize("max_steps", [0, 1])
def test_step_limit_boundary(max_steps: int) -> None:
    result = Agent(max_steps=max_steps).run(successful_sample())

    assert result.status == "step_limit"
    assert result.processed_steps == max_steps
    assert result.completion_reason


def test_exact_limit_completes_task() -> None:
    result = Agent(max_steps=3).run(successful_sample())

    assert result.status == "completed"
    assert result.processed_steps == 3


def test_empty_task_is_completed() -> None:
    result = Agent().run(AgentTask("Nhiệm vụ rỗng", ()))

    assert result.status == "completed"
    assert result.completion_reason


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        Agent(max_steps=-1)
    with pytest.raises(ValueError):
        Agent(max_steps=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Agent(max_steps="2")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TaskStep(" ")
    with pytest.raises(TypeError):
        AgentTask("Sai kiểu bước", [])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AgentTask("Bước không hợp lệ", (object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Agent().run(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TaskStep("Bước lỗi", succeeds=False, failure_reason="   ")
    with pytest.raises(ValueError):
        AgentResult("unknown", 0, 0, "lý do")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AgentResult("completed", 0, 0, "   ")
    with pytest.raises(ValueError):
        AgentResult("completed", -1, 0, "lý do")
    with pytest.raises(ValueError):
        AgentResult("completed", 2, 1, "lý do")
    with pytest.raises(ValueError):
        AgentResult([], 0, 0, "lý do")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AgentResult("completed", True, 1, "lý do")  # type: ignore[arg-type]


def test_repeated_runs_have_stable_results() -> None:
    task = successful_sample()

    assert Agent().run(task) == Agent().run(task)

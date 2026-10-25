"""Shared fault-injection fixture: replace one registered tool's run with a script."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from laplace.tools import base as tool_base
from laplace.tools import errors


@dataclass
class FaultyTool:
    """Scripted outcomes per physical run; ``calls`` counts real invocations."""

    name: str
    script: list[Any]
    calls: int = 0
    seen_args: list[Any] = field(default_factory=list)

    def run(self, args, context):
        self.seen_args.append(args)
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        outcome = self.script[index]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(args, context)
        return outcome


@pytest.fixture
def faulty_tool(monkeypatch) -> Callable[[str, list[Any]], FaultyTool]:
    """``faulty_tool(name, [TimeoutError(), ToolPayload(...)])`` swaps the real run.

    The registry entry is restored by monkeypatch; sleeps during transient
    backoff are recorded instead of waited.
    """
    tool_base.load_builtin_tools()
    slept: list[float] = []
    monkeypatch.setattr(errors, "sleep", slept.append)

    def install(name: str, script: list[Any]) -> FaultyTool:
        spec = tool_base.get_tool(name)
        assert spec is not None, f"unknown production tool {name}"
        faulty = FaultyTool(name, list(script))
        monkeypatch.setitem(tool_base._REGISTRY, name, replace(spec, run=faulty.run))
        faulty.slept = slept  # type: ignore[attr-defined]
        return faulty

    return install

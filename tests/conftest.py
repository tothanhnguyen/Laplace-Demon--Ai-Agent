"""Shared fault-injection fixture built on the harness FaultyTool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from laplace.evals.faults import FaultyTool
from laplace.tools import base as tool_base
from laplace.tools import errors


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
        faulty = FaultyTool(name, list(script), slept=slept)
        monkeypatch.setitem(tool_base._REGISTRY, name, replace(spec, run=faulty.run))
        return faulty

    return install

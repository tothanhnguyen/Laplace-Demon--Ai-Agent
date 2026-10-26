"""Scripted tool faults installed into the production registry, then restored.

Shared by the evaluation harness and the pytest ``faulty_tool`` fixture so both
inject failures at the same place: the registered ``run`` callable, below the
gateway that classifies and retries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from laplace.tools import base as tool_base
from laplace.tools.base import ToolFailure, ToolPayload

NETWORK_TOOLS = frozenset({"web_search", "fetch_page"})
STUB_URL = "https://example.test/evidence"
DEFAULT_STUB_TEXT = (
    "Trang tổng hợp dùng cho đánh giá offline. Python 3.14.0 được phát hành chính thức "
    "ngày 7 tháng 10 năm 2025. Python ra đời năm 1991 do Guido van Rossum tạo ra."
)


def stub_run(tool: str, text: str) -> Callable[..., Any]:
    """Deterministic payload for a network tool so mock runs never touch the web."""
    def run(args, _context):
        if tool == "web_search":
            items = [{"title": "Nguồn tổng hợp", "url": STUB_URL, "content": text},
                     {"title": "Nguồn thứ hai", "url": STUB_URL + "/2", "content": text}]
            return ToolPayload(data={"query": args.query, "results": items},
                               sources=(STUB_URL, STUB_URL + "/2"))
        return ToolPayload(data={"query": args.query, "results": [{"url": args.url,
                           "content": text}]}, sources=(args.url,))
    run.__name__ = f"stub_{tool}"
    return run


@dataclass
class FaultyTool:
    """Scripted outcomes per physical run; ``calls`` counts real invocations.

    Script items: an exception instance (raised), a callable ``(args, context)``
    (invoked) or any other value (returned). The last item repeats.
    """

    name: str
    script: list[Any]
    calls: int = 0
    seen_args: list[Any] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)

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


def fault_outcome(step: str, real_run: Callable[..., Any]) -> Any:
    """Translate a declarative fault token into a script item."""
    if step == "ok":
        return real_run
    if step == "timeout":
        return TimeoutError("synthetic timeout")
    if step == "runtime_error":
        return RuntimeError("synthetic runtime fault")
    return ToolFailure(step, "synthetic fault")  # type: ignore[arg-type]


def install_faults(
    faults: Iterable[tuple[str, list[str]]],
    *,
    stub_texts: dict[str, str] | None = None,
    offline: bool = False,
) -> tuple[dict[str, FaultyTool], Callable[[], None]]:
    """Replace each named tool's ``run`` with a scripted one; returns a restore callback.

    ``stub_texts`` pins canned content for a network tool in every mode; ``offline``
    additionally stubs all network tools so a mock run makes no HTTP calls.
    """
    tool_base.load_builtin_tools()
    stub_texts = dict(stub_texts or {})
    scripts = {name: list(steps) for name, steps in faults}
    for name in set(stub_texts) | (NETWORK_TOOLS if offline else set()):
        scripts.setdefault(name, ["ok"])
    originals: dict[str, tool_base.ToolSpec] = {}
    installed: dict[str, FaultyTool] = {}
    def restore() -> None:
        for name, spec in originals.items():
            tool_base._REGISTRY[name] = spec

    for name, steps in scripts.items():
        spec = tool_base.get_tool(name)
        if spec is None:
            restore()  # leave no half-patched registry behind
            raise LookupError(f"unknown production tool {name}")
        real_run = spec.run
        if name in stub_texts or (offline and name in NETWORK_TOOLS):
            real_run = stub_run(name, stub_texts.get(name, DEFAULT_STUB_TEXT))
        faulty = FaultyTool(name, [fault_outcome(step, real_run) for step in steps])
        originals[name] = spec
        installed[name] = faulty
        tool_base._REGISTRY[name] = replace(spec, run=faulty.run)
    return installed, restore

---
title: "Deterministic Agent Task Loop"
description: "Implement a small sequential Agent loop that completes sample tasks and explains why execution stopped."
status: completed
priority: P1
effort: 4h
branch: sprint01
tags: [feature, agent, deterministic, python]
created: 2026-08-24
blockedBy: []
blocks: []
---

# Deterministic Agent Task Loop

## Overview

Add the first domain capability to Laplace's Demon: a pure-Python Agent that processes a finite sample task step by step and returns an explicit stop status and human-readable completion reason. The implementation must be deterministic, synchronous, and independent of databases, LLMs, tools, Telegram, and trace systems.

## Phases

| Phase | Name | Status | Depends on |
|---|---|---|---|
| 1 | [Domain model and execution loop](./phase-01-domain-agent-loop.md) | Complete | None |
| 2 | [CLI integration, tests, and docs](./phase-02-cli-tests-and-docs.md) | Complete | Phase 1 |

## Scope

### In scope

- A small task/step model with ordered steps.
- Sequential deterministic execution.
- Exactly three terminal statuses: `completed`, `failed`, `step_limit`.
- A non-empty `completion_reason` for every terminal result.
- A configurable maximum number of processed steps.
- Sample task definitions that exercise successful completion and failure/limit scenarios.
- An optional CLI sample-run path; the no-argument CLI output remains unchanged.
- Unit, CLI smoke, and Ruff validation.

### Explicitly out of scope

- Database or persistence.
- Real LLM/model calls.
- External or internal tools.
- Telegram or other messaging integrations.
- Execution tracing, telemetry, retries, concurrency, scheduling, or plugin systems.
- Agent planning beyond the supplied ordered steps.

## Proposed design

- `laplace/agent.py` owns the domain types and loop: step, task, status, result, validation, and `Agent.run`.
- `laplace/tasks.py` owns deterministic sample-task factories/data only; it must not execute work or contain transport concerns.
- `laplace/__main__.py` keeps the existing no-argument readiness output. A dedicated sample flag invokes the sample factory and prints status plus `completion_reason`.
- The loop processes at most one supplied step per iteration. A failing step stops immediately with `failed`; reaching the configured limit before all steps stop with `step_limit`; exhausting all steps returns `completed`.
- Result data records the number of processed steps and the total task length, so callers can distinguish a clean completion from an early stop without parsing text.
- Reasons are generated at the domain boundary from the stop condition, with failure detail included when a step supplies one. They are deterministic and always non-empty.

## Data flow

1. Caller or CLI provides an ordered immutable task and an optional step limit.
2. `Agent` validates task names, step definitions, and limit values.
3. The loop reads one step at a time and applies that step's deterministic outcome.
4. The loop exits on failure, limit exhaustion, or end of task.
5. `AgentResult` exits the domain layer with status, processed count, total count, and `completion_reason`.
6. Unit tests assert structured fields; the optional CLI renders the same result for a human.

## Dependencies and sequencing

- Phase 1 has no feature dependency and can start from the current bootstrap.
- Phase 2 is blocked on the public domain types and status semantics from Phase 1.
- Existing local modifications in `laplace/config.py` and `laplace/__main__.py` must be preserved and merged carefully; do not reset or overwrite them.
- No new runtime dependency is required. Existing Python `>=3.11`, pytest, and Ruff are sufficient.

## Backwards compatibility and migration

- Existing settings behavior and the no-argument `python -m laplace` readiness line remain unchanged.
- The sample-run flag is additive; existing callers do not need migration.
- New domain modules have no persistence or serialized data contract, so no data migration is needed.
- If the CLI shape proves undesirable, the domain API remains usable independently and the optional CLI path can be reverted without changing task semantics.

## Risk register

| Phase | Risk (likelihood x impact) | Mitigation |
|---|---|---|
| 1 | Ambiguous limit boundary (M x H) | Define and test `max_steps=0`, exact-limit completion, and limit-before-next-step behavior explicitly. |
| 1 | Failure reason becomes inconsistent or empty (M x M) | Centralize reason construction and assert non-empty reasons for every status. |
| 1 | Feature grows toward real Agent abstractions (M x M) | Use data-only steps and one synchronous loop; reject retries, tools, callbacks, and planning. |
| 2 | Existing CLI smoke test regresses (M x M) | Keep no-argument output byte-for-byte stable and add separate sample-mode coverage. |
| 2 | User edits are overwritten (L x H) | Inspect and preserve current `config.py`/`__main__.py` changes; patch only required sections. |

## Rollback

- Revert the new `laplace/agent.py`, `laplace/tasks.py`, and their tests/docs as one feature change.
- Remove only the optional CLI flag and sample documentation from `__main__.py`/README; preserve the original readiness behavior.
- No database, migration, or external integration rollback is required.

## File ownership

| Owner | Files |
|---|---|
| Phase 1 | `laplace/agent.py`, `laplace/tasks.py`, `tests/test_agent.py` |
| Phase 2 | `laplace/__main__.py`, `tests/test_bootstrap.py`, `README.md`, `docs/project-changelog.md` if present |

No parallel phase may edit the same file. Implementation should be sequential because Phase 2 consumes Phase 1's public contract.

## Test matrix

| Area | Scenario | Expected assertion |
|---|---|---|
| Unit | All sample steps succeed | `completed`, all steps processed, reason explains completion |
| Unit | Failure on first step | `failed`, one or zero completed count per chosen contract, reason names failure |
| Unit | Failure after prior success | Stops at failing step; later steps are not processed |
| Unit | Limit below task length | `step_limit`, exact processed count, reason explains limit |
| Unit | `max_steps=0` | `step_limit` for non-empty task; no step processed |
| Unit | Limit exactly equals remaining work | `completed`, not `step_limit` |
| Unit | Empty task | Deterministic documented result, non-empty reason |
| Unit | Invalid negative limit or malformed step | Raises the documented validation exception |
| Unit | Every terminal status | `completion_reason` is non-empty and stable across repeated runs |
| CLI | No arguments | Existing readiness output remains unchanged |
| CLI | Sample flag | Exit code zero and output includes status and completion reason |
| Quality | Ruff and pytest | Both pass on Python 3.11+ |

## Acceptance criteria

- A caller can run a supplied ordered sample task with no external service.
- The loop processes steps in order and never processes a step after a terminal condition.
- Every result has exactly one of `completed`, `failed`, or `step_limit`.
- Every result has a deterministic, non-empty `completion_reason` that explains the terminal condition.
- Step-limit behavior is defined at boundary values and covered by tests.
- `python -m laplace` without arguments retains its current readiness output.
- The sample CLI path demonstrates the result without adding runtime dependencies.
- `ruff check .` and `pytest` pass.
- README and changelog (when present) describe the new capability and explicit exclusions.

## Documentation impact

Minor: update README usage/scope and add one changelog entry after implementation. No architecture document change is needed because this sprint adds a local pure-Python module without external boundaries.

## Implementation handoff

Implement only after this plan is approved. Recommended validation order: focused agent tests, CLI smoke tests, `ruff check .`, then full `pytest`.

## Completion Notes

- Implemented a deterministic sequential Agent with `completed`, `failed`, and `step_limit` terminal statuses.
- Every terminal result includes a non-empty Vietnamese `completion_reason`.
- Added successful and failing sample tasks, boundary tests, and the additive `--sample-agent` CLI path.
- Preserved the no-argument readiness output and avoided database, LLM, tool, Telegram, trace, retry, scheduling, and concurrency dependencies.
- Validation passed: Ruff, compileall, diff check, and 13 pytest tests.
- Review found no blocking or non-blocking issues; remaining test gaps are low-risk because the domain steps are immutable data without callbacks or side effects.

**Status:** DONE
**Docs impact:** minor — README updated with sample invocation and status semantics.

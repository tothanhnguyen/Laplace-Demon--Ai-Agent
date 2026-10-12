---
title: "Domain Model and Execution Loop"
status: completed
priority: P1
effort: 2h
phase: 1
created: 2026-08-24
---

# Phase 1 - Domain Model and Execution Loop

## Overview

Create the minimal deterministic domain API for ordered task execution. This phase owns all stop semantics and must remain independent of CLI, settings, persistence, network, model providers, tools, and tracing.

## Related files

- Create: `laplace/agent.py`
- Create: `laplace/tasks.py`
- Create: `tests/test_agent.py`
- Read for conventions: `pyproject.toml`, `laplace/config.py`

## Requirements

- Represent a named ordered task and its deterministic steps.
- Expose a stable status contract containing only `completed`, `failed`, and `step_limit`.
- Return structured result fields: status, processed step count, total step count, and `completion_reason`.
- Enforce a non-negative step limit and reject malformed task/step input with a clear exception.
- Stop immediately on a failing step; do not inspect or process later steps.
- Define limit precedence at all boundaries, including zero, exact completion, and limit before the next step.
- Provide at least one all-success sample task and deterministic data suitable for failure and limit tests.

## Implementation steps

1. Define the public domain types and validation rules.
2. Define sample task data/factories without execution side effects.
3. Implement one synchronous loop with a single terminal-result path per stop condition.
4. Generate stable human-readable reasons from the terminal condition.
5. Add focused tests for happy path, failures, limits, empty input, malformed input, and reason stability.
6. Run `pytest tests/test_agent.py` and `ruff check laplace/agent.py laplace/tasks.py tests/test_agent.py`.

## Failure modes and mitigations

- Negative limit: reject before execution; test exception type/message shape.
- Empty task: document and test one deterministic completed result.
- Failure and limit on the same boundary: check failure only for a step that is actually allowed to run; otherwise return step limit.
- Reason drift: assert exact or contract-level stable text in tests.
- Accidental side effects: keep steps data-only and repeat the same task twice in tests.

## Success criteria

- Focused tests pass.
- Repeated runs over the same task return equal structured results.
- No external imports or runtime dependencies are added.
- The module remains comfortably below the repository's 200-line guidance.

## Rollback

Delete the phase's new module and tests. No existing runtime behavior is changed by this phase.

## Todo

- [x] Define domain types and status contract.
- [x] Implement deterministic loop and sample task data.
- [x] Add boundary/error tests.
- [x] Run focused pytest and Ruff.

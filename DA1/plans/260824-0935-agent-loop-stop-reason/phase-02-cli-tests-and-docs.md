---
title: "CLI Integration, Tests, and Documentation"
status: completed
priority: P1
effort: 2h
phase: 2
created: 2026-08-24
---

# Phase 2 - CLI Integration, Tests, and Documentation

## Overview

Expose the completed domain behavior without changing the existing default CLI contract, then document and verify the feature.

## Related files

- Modify: `laplace/__main__.py`
- Modify: `tests/test_bootstrap.py`
- Modify: `README.md`
- Modify: `docs/project-changelog.md` if the file exists at implementation time
- Depends on: `laplace/agent.py`, `laplace/tasks.py`, `tests/test_agent.py`

## Requirements

- Preserve the current no-argument readiness output exactly.
- Add an explicit sample-run CLI path that executes a deterministic sample task and prints its status and completion reason.
- Keep CLI formatting separate from domain reason generation.
- Document install/run commands, sample invocation, statuses, limit semantics, and exclusions.
- Record the feature in the changelog only after implementation and validation.

## Implementation steps

1. Add minimal argument parsing around the existing entrypoint.
2. Route only the sample flag to the sample task and Agent; leave the default path unchanged.
3. Add/adjust bootstrap tests for default CLI compatibility and sample output.
4. Update README and the existing changelog without documenting out-of-scope integrations as implemented.
5. Run focused CLI tests, `ruff check .`, and full `pytest`.

## Failure modes and mitigations

- Default output changes: retain the current no-argument branch and exact smoke assertion.
- CLI output parses reason incorrectly: assert both status and reason are present while structured tests own semantic verification.
- Domain API leaks into CLI: use one narrow invocation path and no global state.
- Documentation overpromises future integrations: list them only under explicit exclusions.

## Success criteria

- Existing bootstrap tests remain green.
- Sample invocation exits successfully and exposes a meaningful stop reason.
- `ruff check .` and `pytest` pass.
- README/changelog accurately match the implemented public behavior.

## Rollback

Remove the optional CLI argument and associated docs/tests. Keep the no-argument readiness command intact; retain Phase 1 as an independently usable library API if only presentation is rolled back.

## Todo

- [x] Add backward-compatible sample CLI path.
- [x] Add CLI regression and sample-output tests.
- [x] Update README; no changelog file exists in the project.
- [x] Run full lint and test validation.

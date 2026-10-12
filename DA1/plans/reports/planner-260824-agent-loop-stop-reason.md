# Planning Report: Deterministic Agent Task Loop

## Scope decision

Plan a small pure-Python sequential Agent loop for the official Laplace-Demon project. The loop consumes ordered deterministic sample steps and returns `completed`, `failed`, or `step_limit` together with a non-empty `completion_reason`.

## Codebase findings

- Current runtime surface is limited to settings and a module entrypoint.
- Python `>=3.11`, `pydantic-settings`, pytest, and Ruff already cover the required implementation needs.
- Existing bootstrap tests assert the exact no-argument CLI output.
- The working tree contains user changes in `laplace/config.py` and `laplace/__main__.py`; implementation must preserve them.
- No unfinished plan overlaps this feature; the earlier bootstrap plan is completed.

## Architecture decision

Use two small domain modules: one for task/result types and loop control, one for sample task data. Keep CLI integration additive behind an explicit sample-run flag so the existing no-argument behavior remains stable. Avoid all persistence, model providers, tools, messaging, and tracing.

## Planned validation

Test all three terminal statuses, limit boundaries including zero and exact completion, failure short-circuiting, empty/malformed input, deterministic reason text, default CLI compatibility, sample CLI output, Ruff, and pytest.

## Plan location

`/Users/thanhnguyen/Documents/DA1/plans/260824-0935-agent-loop-stop-reason/plan.md`

**Status:** DONE
**Summary:** Comprehensive two-phase implementation plan created with explicit scope, data flow, dependencies, risks, rollback, ownership, test matrix, and measurable acceptance criteria.
**Concerns/Blockers:** Implementation has not started; plan approval is required before code changes.

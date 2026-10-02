---
title: Scaffold project
status: completed
priority: P1
effort: 1h
phase: 1
created: 2026-08-20
---

# Phase 1 - Scaffold Project

## Files To Create

- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/pyproject.toml`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/laplace/__init__.py`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/laplace/__main__.py`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/laplace/config.py`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/tests/test-bootstrap.py`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/.gitignore`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/.github/workflows/ci.yml`
- `/Users/thanhnguyen/Documents/DA1/Laplace-Demon/README.md`
- Environment template at project root.

## Todo

- [x] Configure Python `>=3.11`, `pytest`, `ruff`, `pydantic-settings`.
- [x] Add minimal settings module and CLI entrypoint.
- [x] Add isolated bootstrap tests.
- [x] Add strict secret ignores and CI matrix 3.11/3.12.
- [x] Document install, run, lint, test.

## Success Criteria

- Project installs in a clean virtual environment.
- `python -m laplace` runs.
- No future Sprint modules implemented early.


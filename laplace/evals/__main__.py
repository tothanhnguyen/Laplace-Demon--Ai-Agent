"""``python -m laplace.evals``: run the declarative evaluation suite with one command."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from laplace.evals.cases import DEFAULT_CASES_DIR, CaseLoadError, load_cases
from laplace.evals.report import store, write_files
from laplace.evals.runner import run_suite
from laplace.logging import setup_logging

EXIT_OK, EXIT_FAILED_CASES, EXIT_HARNESS = 0, 1, 2
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Laplace's Demon evaluation harness")
    parser.add_argument("--provider", choices=("mock", "real"), default="mock")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--only", nargs="*", default=None, metavar="CASE_ID")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", type=Path, default=Path("var/evals"))
    args = parser.parse_args(argv)
    setup_logging(level=logging.WARNING)
    try:
        cases = load_cases(args.cases)
    except CaseLoadError as exc:
        print(f"case error: {exc}", file=sys.stderr)
        return EXIT_HARNESS
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c.id in wanted]
        missing = wanted - {c.id for c in cases}
        if missing:
            print(f"unknown case ids: {sorted(missing)}", file=sys.stderr)
            return EXIT_HARNESS
    if not cases:
        print("no cases to run", file=sys.stderr)
        return EXIT_HARNESS
    run_id = args.run_id or f"{args.provider}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    if not RUN_ID.match(run_id):
        print("run id must match [A-Za-z0-9._-], max 64 chars", file=sys.stderr)
        return EXIT_HARNESS
    run_dir = args.out / run_id
    try:
        suite = run_suite(cases, provider_mode=args.provider, run_dir=run_dir, run_id=run_id)
    except FileExistsError as exc:
        print(f"{exc}; choose another --run-id", file=sys.stderr)
        return EXIT_HARNESS
    results, report = write_files(suite, run_dir)
    store(suite)
    for result in suite.results:
        mark = "PASS" if result.passed else f"FAIL[{result.failure_group}]"
        print(f"{mark:24} {result.case.id:28} {result.duration_ms:6d} ms")
    print(f"\n{suite.passed}/{suite.total} passed · {results} · {report}")
    if any(r.failure_group == "harness_error" for r in suite.results):
        return EXIT_HARNESS
    return EXIT_OK if suite.passed == suite.total else EXIT_FAILED_CASES


if __name__ == "__main__":
    sys.exit(main())

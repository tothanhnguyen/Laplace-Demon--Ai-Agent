"""``python -m laplace.viewer``: serve the observation pages on 127.0.0.1 only."""

from __future__ import annotations

import argparse

import uvicorn

from laplace.viewer.app import create_app

LOCALHOST = "127.0.0.1"
DEFAULT_PORT = 8787


def serve(*, port: int = DEFAULT_PORT, database_url: str | None = None) -> None:
    """Loopback only: the pages expose task content and have no authentication."""
    uvicorn.run(create_app(database_url), host=LOCALHOST, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Laplace's Demon observation viewer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=None, help="SQLAlchemy URL, e.g. sqlite:///var/evals/x/laplace.db")
    args = parser.parse_args(argv)
    print(f"Viewer: http://{LOCALHOST}:{args.port}/tasks (chỉ máy này truy cập được)")
    serve(port=args.port, database_url=args.db)


if __name__ == "__main__":
    main()

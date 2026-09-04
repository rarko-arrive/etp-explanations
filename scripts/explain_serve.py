"""Start the interactive ETP explainer dashboard.

Example
-------
    uv run python scripts/explain_serve.py
    uv run python scripts/explain_serve.py --port 8765 --data-dir ~/data
"""

from __future__ import annotations

import argparse

from dotenv import find_dotenv, load_dotenv
from loguru import logger

load_dotenv(find_dotenv())

from app.explain.server import run_server
from app.explain.service import ExplainOptions
from dqt import resolve_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    logger.info("ETP explainer dashboard at http://{}:{}/", args.host, args.port)

    run_server(
        host=args.host,
        port=args.port,
        default_opts=ExplainOptions(data_dir=data_dir),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Start the interactive ETP explainer dashboard.

Example
-------
    uv run python scripts/explain_serve.py
    uv run python scripts/explain_serve.py --port 8765 --data-dir ~/data
"""

from __future__ import annotations

import argparse
import os

from dotenv import find_dotenv, load_dotenv
from loguru import logger

load_dotenv(find_dotenv())

from app.explain.server import run_server
from app.explain.service import ExplainOptions, resolve_explain_cache_dir
from dqt import resolve_data_dir


def _env_host() -> str:
    return (os.environ.get("EXPLAIN_HOST") or "127.0.0.1").strip()


def _env_port() -> int:
    raw = (os.environ.get("EXPLAIN_PORT") or "8765").strip()
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="writable HTML cache (default: data/explain-shipments or $EXPLAIN_CACHE_DIR)",
    )
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    host = args.host if args.host is not None else _env_host()
    port = args.port if args.port is not None else _env_port()
    cache_dir = resolve_explain_cache_dir(args.cache_dir)
    logger.info("ETP explainer dashboard at http://{}:{}/", host, port)
    if cache_dir is not None:
        logger.info("explain HTML cache → {}", cache_dir)

    run_server(
        host=host,
        port=port,
        default_opts=ExplainOptions(data_dir=data_dir),
        output_dir=cache_dir,
        proxy_headers=_proxy_headers_enabled(),
    )
    return 0


def _proxy_headers_enabled() -> bool:
    raw = (os.environ.get("EXPLAIN_BEHIND_PROXY") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


if __name__ == "__main__":
    raise SystemExit(main())

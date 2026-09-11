"""Start the interactive ETP explainer dashboard.

Example
-------
    uv run python scripts/explain_serve.py
    uv run python scripts/explain_serve.py --port 8765
    uv run python scripts/explain_serve.py --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse

from dotenv import find_dotenv, load_dotenv
from loguru import logger

# Load .env file first
load_dotenv(find_dotenv())

from app.explain.config import get_settings
from app.explain.server import run_server


def main(argv: list[str] | None = None) -> int:
    """Run the ETP explainer server.

    Args:
        argv: Command line arguments (None = sys.argv)

    Returns:
        Exit code (0 = success)
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="Host to bind to (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to (default: from config)")
    parser.add_argument(
        "--proxy",
        action="store_true",
        help="Enable proxy header trust (default: from config)",
    )
    args = parser.parse_args(argv)

    # Load settings
    settings = get_settings()

    # Determine proxy headers setting
    proxy_headers = args.proxy if args.proxy else settings.behind_proxy

    # Log configuration
    host = args.host or settings.host
    port = args.port or settings.port
    logger.info("ETP Explainer dashboard at http://{}:{}/", host, port)
    logger.info("Data directory: {}", settings.data_dir)
    logger.info("Cache directory: {}", settings.get_cache_dir())
    logger.info("Authentication: {}", "enabled" if settings.auth_enabled else "DISABLED")

    if not settings.auth_enabled:
        logger.warning("⚠️  Authentication is DISABLED - for development only!")

    # Run the server
    run_server(
        host=args.host,
        port=args.port,
        proxy_headers=proxy_headers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

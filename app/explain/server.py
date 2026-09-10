"""FastAPI server for the ETP explainer application.

Production-ready application with authentication, logging, and error handling.
"""

from __future__ import annotations


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.explain.config import get_settings
from app.explain.exceptions import AuthenticationError, LoadNotFoundError
from app.explain.middleware import RequestLoggingMiddleware
from app.explain.router import router


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance
    """
    # Load settings to validate configuration early
    settings = get_settings()

    # Configure logging
    logger.configure(handlers=[{"sink": "sys.stderr", "level": settings.log_level}])

    # Create FastAPI app
    app = FastAPI(
        title="ETP Explainer",
        description="ETP shipment lifecycle material-change ledger and explanation tool",
        version="0.1.0",
    )

    # Add middleware (order matters - last added is outermost)
    app.add_middleware(RequestLoggingMiddleware)

    # Add routes
    app.include_router(router)

    # Exception handlers
    @app.exception_handler(LoadNotFoundError)
    async def load_not_found_handler(request: Request, exc: LoadNotFoundError) -> JSONResponse:
        """Handle load not found errors with proper status code."""
        return JSONResponse(
            status_code=404,
            content={
                "error": "Load not found",
                "message": str(exc),
                "loadnumber": exc.loadnumber,
            },
        )

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
        """Handle authentication errors."""
        return JSONResponse(
            status_code=401,
            content={"error": "Authentication failed", "message": str(exc)},
            headers={"WWW-Authenticate": "Basic"},
        )

    logger.info("ETP Explainer application initialized")
    if not settings.auth_enabled:
        logger.warning("Authentication is DISABLED - for development only!")

    return app


# Global app instance for uvicorn
app = create_app()


def run_server(
    host: str | None = None,
    port: int | None = None,
    proxy_headers: bool | None = None,
) -> None:
    """Run the server with uvicorn.

    Args:
        host: Override host from settings
        port: Override port from settings
        proxy_headers: Override proxy_headers from settings
    """
    import uvicorn

    settings = get_settings()

    # Use overrides or fall back to settings
    host = host if host is not None else settings.host
    port = port if port is not None else settings.port
    proxy_headers = proxy_headers if proxy_headers is not None else settings.behind_proxy

    logger.info("Starting ETP Explainer server at http://{}:{}/", host, port)

    uvicorn.run(
        "app.explain.server:app",
        host=host,
        port=port,
        reload=False,
        proxy_headers=proxy_headers,
        forwarded_allow_ips="*" if proxy_headers else None,
    )

"""Middleware for logging, timing, and request tracking."""

from __future__ import annotations

import time
import uuid
from typing import Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for logging requests with correlation IDs and timing."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process the request and add logging."""
        # Generate a unique request ID for correlation
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        # Log the incoming request
        logger.bind(request_id=request_id).info(
            "Request started: {} {}",
            request.method,
            request.url.path,
        )

        # Track timing
        start_time = time.time()

        try:
            # Process the request
            response = await call_next(request)

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Log the response
            logger.bind(request_id=request_id).info(
                "Request completed: {} {} - status={} duration={:.2f}ms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )

            # Add request ID to response headers for debugging
            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as exc:
            # Calculate duration even on error
            duration_ms = (time.time() - start_time) * 1000

            # Log the error
            logger.bind(request_id=request_id).error(
                "Request failed: {} {} - error={} duration={:.2f}ms",
                request.method,
                request.url.path,
                type(exc).__name__,
                duration_ms,
            )

            # Re-raise the exception
            raise

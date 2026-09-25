"""API routes for the ETP Explainer application."""

from __future__ import annotations


from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from loguru import logger

from app.explain.auth import CurrentUser
from app.explain.dependencies import CacheDir, Lake, Settings
from app.explain.exceptions import LoadNotFoundError
from app.explain.render import render_error_html, render_index_html
from app.explain.service import ExplainOptions, read_cached_html, render_explanation_for_load, write_cached_html

# Create the router
router = APIRouter()


@router.get("/health")
def health() -> dict[str, bool]:
    """Health check endpoint (no auth required).

    Returns:
        A simple health status object
    """
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def index(user: CurrentUser) -> HTMLResponse:
    """Render the index/search page.

    Args:
        user: Authenticated username (from basic auth)

    Returns:
        HTML response with the index page
    """
    return HTMLResponse(render_index_html())


@router.get("/explain/{loadnumber}", response_class=HTMLResponse)
def explain_page(
    loadnumber: int,
    user: CurrentUser,
    settings: Settings,
    lake: Lake,
    cache_dir: CacheDir,
    refresh: bool = Query(default=False, description="Force refresh, bypass cache"),
    query_mde: bool = Query(default=False, description="Query MDE database"),
) -> HTMLResponse:
    """Render explanation page for a load.

    Args:
        loadnumber: The load number to explain
        user: Authenticated username (from basic auth)
        settings: Application settings
        lake: EtpLake instance
        cache_dir: Cache directory path
        refresh: If True, bypass cache and regenerate
        query_mde: If True, query MDE database for additional data

    Returns:
        HTML response with the explanation page

    Raises:
        HTTPException: 404 if load not found, 500 on render error
    """
    # Check cache first (unless refresh requested)
    if not refresh:
        cached = read_cached_html(loadnumber, output_dir=cache_dir)
        if cached is not None:
            logger.bind(loadnumber=loadnumber).info("Cache hit for load {}", loadnumber)
            return HTMLResponse(cached)

    logger.bind(loadnumber=loadnumber).info("Rendering explanation for load {}", loadnumber)

    # Build explain options
    opts = ExplainOptions(
        data_dir=settings.data_dir,
        query_mde=query_mde,
        lake=lake,
    )

    try:
        html = render_explanation_for_load(loadnumber, opts)
    except LoadNotFoundError as exc:
        logger.bind(loadnumber=loadnumber).warning("Load not found: {}", exc)
        body = render_error_html(status=404, title="Load not found", message=str(exc))
        return HTMLResponse(body, status_code=404)
    except Exception:
        logger.bind(loadnumber=loadnumber).exception("Failed to render explainer for load {}", loadnumber)
        body = render_error_html(
            status=500,
            title="Explainer error",
            message=f"Failed to render explanation for load {loadnumber}. Check server logs.",
        )
        return HTMLResponse(body, status_code=500)

    # Write to cache
    write_cached_html(loadnumber, html, output_dir=cache_dir)
    logger.bind(loadnumber=loadnumber).info("Cached explanation for load {}", loadnumber)

    return HTMLResponse(html)

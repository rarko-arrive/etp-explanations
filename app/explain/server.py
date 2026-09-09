"""FastAPI server for the interactive ETP explainer POC."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from loguru import logger

from app.explain.render import render_error_html, render_index_html
from app.explain.service import (
    ExplainOptions,
    LoadNotFoundError,
    read_cached_html,
    render_explanation_for_load,
    write_cached_html,
)


def create_app(
    *,
    default_opts: ExplainOptions | None = None,
    output_dir: Path | None = None,
) -> FastAPI:
    """Build FastAPI app with optional default explain options and cache dir."""
    app = FastAPI(title="ETP Explainer")
    app.state.default_opts = default_opts or ExplainOptions()
    app.state.output_dir = output_dir

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index_html())

    @app.get("/explain/{loadnumber}", response_class=HTMLResponse)
    def explain_page(
        loadnumber: int,
        refresh: bool = False,
        query_mde: bool = False,
    ) -> HTMLResponse:
        cache_dir: Path | None = app.state.output_dir
        if not refresh:
            cached = read_cached_html(loadnumber, output_dir=cache_dir)
            if cached is not None:
                return HTMLResponse(cached)

        base_opts: ExplainOptions = app.state.default_opts
        opts = ExplainOptions(
            data_dir=base_opts.data_dir,
            mde_cache=base_opts.mde_cache,
            davis_cache=base_opts.davis_cache,
            query_mde=query_mde or base_opts.query_mde,
            cohort=base_opts.cohort,
            rank=base_opts.rank,
            lake=base_opts.lake,
        )
        try:
            html = render_explanation_for_load(loadnumber, opts)
        except LoadNotFoundError as exc:
            body = render_error_html(status=404, title="Load not found", message=str(exc))
            return HTMLResponse(body, status_code=404)
        except Exception:  # noqa: BLE001 — unexpected render failures → 500 page
            logger.exception("failed to render explainer for load {}", loadnumber)
            body = render_error_html(
                status=500,
                title="Explainer error",
                message=f"Failed to render explanation for load {loadnumber}. Check server logs.",
            )
            return HTMLResponse(body, status_code=500)

        write_cached_html(loadnumber, html, output_dir=cache_dir)
        return HTMLResponse(html)

    return app


app = create_app()


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    default_opts: ExplainOptions | None = None,
    output_dir: Path | None = None,
    proxy_headers: bool = False,
) -> None:
    import uvicorn

    application = create_app(default_opts=default_opts, output_dir=output_dir)
    uvicorn.run(
        application,
        host=host,
        port=port,
        reload=False,
        proxy_headers=proxy_headers,
        forwarded_allow_ips="*" if proxy_headers else None,
    )

"""Static HTML explainer for ETP shipment lifecycle explanations."""

from app.explain.render import render_error_html, render_explain_html, render_index_html
from app.explain.service import (
    DEFAULT_EXPLAIN_OUTPUT_DIR,
    ExplainOptions,
    LoadNotFoundError,
    cache_path_for_load,
    read_cached_html,
    render_explanation_for_load,
    write_cached_html,
)
from app.explain.view_model import build_view_model, figure_to_png_b64

__all__ = [
    "DEFAULT_EXPLAIN_OUTPUT_DIR",
    "ExplainOptions",
    "LoadNotFoundError",
    "build_view_model",
    "cache_path_for_load",
    "figure_to_png_b64",
    "read_cached_html",
    "render_error_html",
    "render_explain_html",
    "render_explanation_for_load",
    "render_index_html",
    "write_cached_html",
]

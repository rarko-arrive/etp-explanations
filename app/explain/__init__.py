"""Static HTML explainer for ETP shipment lifecycle explanations."""

from pathlib import Path

DEFAULT_EXPLAIN_OUTPUT_DIR = Path("data/explain-shipments")

from app.explain.render import render_explain_html
from app.explain.view_model import build_view_model, figure_to_png_b64

__all__ = [
    "DEFAULT_EXPLAIN_OUTPUT_DIR",
    "build_view_model",
    "figure_to_png_b64",
    "render_explain_html",
]

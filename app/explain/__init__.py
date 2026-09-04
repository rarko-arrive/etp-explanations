"""Static HTML explainer for ETP shipment lifecycle explanations."""

from app.explain.render import render_explain_html
from app.explain.view_model import build_view_model, figure_to_png_b64

__all__ = ["build_view_model", "figure_to_png_b64", "render_explain_html"]

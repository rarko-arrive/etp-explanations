"""Render self-contained HTML for the ETP explainer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _json_for_script(data: dict[str, Any]) -> str:
    """Serialize JSON safe for embedding in a ``<script>`` tag."""
    return json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")


def render_explain_html(view_model: dict[str, Any]) -> str:
    """Render the explainer page with embedded view-model JSON."""
    template_path = Path(__file__).parent / "templates" / "explain.html"
    html = template_path.read_text()
    return html.replace("__DATA_JSON__", _json_for_script(view_model))

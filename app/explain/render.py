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


def render_index_html() -> str:
    """Render the search landing page."""
    template_path = Path(__file__).parent / "templates" / "index.html"
    return template_path.read_text()


def render_error_html(*, status: int, title: str, message: str) -> str:
    """Minimal HTML error page matching explainer styling."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#1a2332;--paper:#f6f7f9;--card:#fff;--line:#e3e7ee;--muted:#6b7686;--mono:monospace}}
body{{background:var(--paper);color:var(--ink);font:15px/1.55 -apple-system,sans-serif;padding:40px 20px}}
.wrap{{max-width:640px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:24px}}
h1{{font-size:20px;margin-bottom:8px}}
p{{color:var(--muted);margin-bottom:16px}}
a{{color:#3b6fb5}}
code{{font-family:var(--mono);font-size:13px;background:#eef1f6;padding:2px 6px;border-radius:4px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p>{message}</p>
  <p><a href="/">← Back to search</a></p>
  <p><code>HTTP {status}</code></p>
</div>
</body>
</html>"""

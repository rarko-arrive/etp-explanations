"""View-model builder for the ETP explainer HTML frontend."""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime
from typing import Any

import polars as pl

from dqt.etp_lifecycle import ledger_to_json

_CHECKPOINT_COLUMNS: tuple[str, ...] = (
    "mark_hrs",
    "mark_label",
    "etp50",
    "target1",
    "target3",
    "t1_gap",
    "t3_gap",
    "delta_etp50_prev",
    "hours_before_pickup",
)


def figure_to_png_b64(figure: Any, *, dpi: int = 120) -> str:
    """Serialize a matplotlib figure to a base64 PNG string."""
    buf = io.BytesIO()
    figure.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    figure.clf()
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _checkpoints_to_json(checkpoints: pl.DataFrame) -> list[dict[str, Any]]:
    if checkpoints.is_empty():
        return []
    cols = [c for c in _CHECKPOINT_COLUMNS if c in checkpoints.columns]
    return json.loads(checkpoints.select(cols).write_json())


def build_view_model(
    explain_result: dict[str, Any],
    *,
    timeline_figure: Any,
    cohort: str | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe view model from ``explain_load()`` + timeline figure."""
    summary = explain_result["summary"]
    payload = explain_result.get("payload") or {}
    ledger = explain_result.get("ledger")
    if not isinstance(ledger, pl.DataFrame):
        ledger = pl.DataFrame()

    if ledger.is_empty():
        pricing_rows: list[dict[str, Any]] = []
        display_rows: list[dict[str, Any]] = []
    else:
        pricing_rows = ledger_to_json(ledger.filter(pl.col("is_pricing_driver")))
        display_rows = ledger_to_json(ledger.filter(~pl.col("is_pricing_driver")))

    checkpoints = payload.get("checkpoints")
    if not isinstance(checkpoints, pl.DataFrame):
        checkpoints = pl.DataFrame()

    lightning = explain_result.get("lightning") or {}
    endpoint_summary = lightning.get("endpoint_summary")
    lightning_out = {"endpoint_summary": endpoint_summary} if endpoint_summary else None

    return {
        "meta": {
            "loadnumber": explain_result.get("loadnumber") or summary.get("loadnumber"),
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "cohort": cohort,
            "rank": rank,
            "primary_category": summary.get("primary_category"),
            "path_archetype": summary.get("path_archetype"),
        },
        "summary": summary,
        "pricing_accuracy": explain_result.get("pricing_accuracy"),
        "lightning": lightning_out,
        "ledger": {
            "pricing": pricing_rows,
            "display": display_rows,
        },
        "checkpoints": _checkpoints_to_json(checkpoints),
        "timeline_png_b64": figure_to_png_b64(timeline_figure),
    }

"""Avail→48hr shift direction rates for cohort breakdown tables."""

from __future__ import annotations

from typing import Any

import polars as pl


def shift_direction_rates(df: pl.DataFrame) -> tuple[float | None, float | None]:
    if df.is_empty() or "etp50_shift_amt" not in df.columns:
        return None, None
    return (
        round(float((df["etp50_shift_amt"] > 0).mean()), 4),
        round(float((df["etp50_shift_amt"] < 0).mean()), 4),
    )


def shift_direction_by_group(
    df: pl.DataFrame,
    group_col: str,
) -> dict[Any, dict[str, float]]:
    """Share of loads with positive / negative avail→48hr shift within each group."""
    if df.is_empty() or group_col not in df.columns or "etp50_shift_amt" not in df.columns:
        return {}
    stats = df.group_by(group_col).agg(
        (pl.col("etp50_shift_amt") > 0).mean().alias("pct_shift_positive"),
        (pl.col("etp50_shift_amt") < 0).mean().alias("pct_shift_negative"),
    )
    return {
        r[group_col]: {
            "pct_shift_positive": round(float(r["pct_shift_positive"]), 4),
            "pct_shift_negative": round(float(r["pct_shift_negative"]), 4),
        }
        for r in stats.iter_rows(named=True)
    }

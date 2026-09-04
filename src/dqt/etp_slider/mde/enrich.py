"""Local enrichment for MDE assess pulls (display pre/post shifts)."""

from __future__ import annotations

import polars as pl

_DISPLAY_TARGETS = ("1", "3")


def _snap_wide(snaps: pl.DataFrame, suffix: str) -> pl.DataFrame:
    """Pivot displayid rows to target1/target3 wide columns."""
    out = snaps.group_by("mde_id", "loadnumber").agg(
        *[
            pl.col("etpvalue").filter(pl.col("displayid") == did).first().alias(f"target{did}_{suffix}")
            for did in _DISPLAY_TARGETS
        ],
        *[
            pl.col("percentile").filter(pl.col("displayid") == did).first().alias(f"pct{did}_{suffix}")
            for did in _DISPLAY_TARGETS
        ],
    )
    return out


def enrich_mde_display(events: pl.DataFrame, display: pl.DataFrame) -> pl.DataFrame:
    """Join bulk displayedtarget history onto MDE event-load rows."""
    if display.is_empty():
        return events

    windows = events.select(
        "mde_id",
        "loadnumber",
        "mde_applied_utc",
        "made_available_utc",
        "pickup_appt_latest_utc",
    ).with_columns(
        pl.coalesce(
            pl.col("made_available_utc"),
            pl.col("mde_applied_utc").dt.offset_by("-30d"),
        ).alias("win_start"),
        pl.coalesce(
            pl.col("pickup_appt_latest_utc"),
            pl.col("mde_applied_utc").dt.offset_by("30d"),
        ).alias("win_end"),
    )

    scoped = windows.join(display, on="loadnumber", how="inner").filter(
        (pl.col("modified_ts") >= pl.col("win_start")) & (pl.col("modified_ts") <= pl.col("win_end"))
    )

    pre = (
        scoped.filter(pl.col("modified_ts") < pl.col("mde_applied_utc"))
        .sort("modified_ts")
        .group_by("mde_id", "loadnumber", "displayid")
        .agg(
            pl.col("etpvalue").last().alias("etpvalue"),
            pl.col("percentile").last().alias("percentile"),
        )
    )
    post = (
        scoped.filter(
            (pl.col("modified_ts") >= pl.col("mde_applied_utc")) & (pl.col("snapshot_mde_id") == pl.col("mde_id"))
        )
        .sort("modified_ts")
        .group_by("mde_id", "loadnumber", "displayid")
        .agg(
            pl.col("etpvalue").first().alias("etpvalue"),
            pl.col("percentile").first().alias("percentile"),
        )
    )

    pre_wide = _snap_wide(pre, "pre")
    post_wide = _snap_wide(post, "post")

    out = (
        events.join(pre_wide, on=["mde_id", "loadnumber"], how="left")
        .join(post_wide, on=["mde_id", "loadnumber"], how="left")
        .with_columns(
            (pl.col("target1_post") - pl.col("target1_pre")).alias("target1_shift_amt"),
            pl.when(pl.col("target1_pre").is_not_null() & (pl.col("target1_pre") != 0))
            .then((pl.col("target1_post") - pl.col("target1_pre")) / pl.col("target1_pre"))
            .otherwise(None)
            .alias("target1_shift_pct"),
            (pl.col("pct1_post") - pl.col("pct1_pre")).alias("pct1_shift"),
            (pl.col("target3_post") - pl.col("target3_pre")).alias("target3_shift_amt"),
            pl.when(pl.col("target3_pre").is_not_null() & (pl.col("target3_pre") != 0))
            .then((pl.col("target3_post") - pl.col("target3_pre")) / pl.col("target3_pre"))
            .otherwise(None)
            .alias("target3_shift_pct"),
            (pl.col("pct3_post") - pl.col("pct3_pre")).alias("pct3_shift"),
            (pl.col("target1_post") > pl.col("realized_cost")).cast(pl.Int8).alias("hit_target1_post"),
            (pl.col("target3_post") > pl.col("realized_cost")).cast(pl.Int8).alias("hit_target3_post"),
            (pl.col("target3_post") - pl.col("realized_cost")).abs().alias("target3_post_cost_gap"),
        )
    )
    return out

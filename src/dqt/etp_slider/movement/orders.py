"""Shared Davis / analysis notebook Snowflake query and shift enrichment.

Used by ``notebooks/etp-slider/story/analysis.ipynb`` and ``analysis-davis.ipynb``.
SQL: ``SQL/etp-slider/analysis-read-orders.sql``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from dqt.etp_slider.sql import SQL_DIR, render_etp_slider_sql

ANALYSIS_ORDERS_SQL = "analysis-read-orders.sql"
DEFAULT_SHIP_START = "2026-07-27"
DEFAULT_SHIP_END = "2026-08-21"

PRICE_SERIES: dict[str, str] = {
    "t1": "target1",
    "t2": "target2",
    "t3": "target3",
    "t4": "target4",
    "p50": "etp50",
}


def analysis_orders_sql(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    sql_dir: Path | None = None,
) -> str:
    """Render the read_orders Snowflake query."""
    root = sql_dir or SQL_DIR
    text = (root / ANALYSIS_ORDERS_SQL).read_text()
    return render_etp_slider_sql(
        text,
        ship_date_start=start_date or DEFAULT_SHIP_START,
        ship_date_end=end_date or DEFAULT_SHIP_END,
    )


def read_orders(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    sql_dir: Path | None = None,
) -> pl.DataFrame:
    """Pull eligible covered loads with endpoint dial targets and model features."""
    from arriveds.snowflake import query_sf

    return query_sf(analysis_orders_sql(start_date, end_date, sql_dir=sql_dir))


CLOCK_DELTA_MIN = 40
LEGACY_LEADTIME_CATEGORY = "DeterministicChange"
INDEX_REL_DELTA_MIN = 0.10
CLHP_ABS_DELTA_MIN = 50.0

INDEX_DELTA_SPECS: tuple[tuple[str, str | None], ...] = (
    ("dat_rate_delta", "dat_rate_avail"),
    ("fuel_cost_delta", "fuel_cost_avail"),
    ("lag7_cpm_delta", "lag7_cpm_avail"),
)
CLHP_DELTA_SPECS: tuple[tuple[str, str | None], ...] = (
    ("clhp_pred_tot_cost_delta", "clhp_pred_tot_cost_avail"),
    ("clhp_pred_line_delta", "clhp_pred_line_avail"),
)


def index_delta_fired_expr(delta_col: str, avail_col: str | None) -> pl.Expr:
    """True when avail→48hr index delta is ≥ INDEX_REL_DELTA_MIN of baseline."""
    if not avail_col:
        return pl.lit(False)
    delta = pl.col(delta_col).fill_null(0).abs()
    denom = pl.col(avail_col).fill_null(0).abs().clip(lower_bound=1e-6)
    return (delta / denom).ge(INDEX_REL_DELTA_MIN)


def enrich_movement_flags(df: pl.DataFrame) -> pl.DataFrame:
    """Movement-category indicator columns (charge / equip / hard-ft / clocks / indices)."""
    charges = pl.col("total_charges_delta").fill_null(0)
    hard_ft = pl.col("hard_ft_delta").fill_null(0)
    book_2_pkup = pl.col("book_2_pkup_delta").fill_null(0)
    avail_2_book = pl.col("avail_2_book_delta").fill_null(0)
    out = df.with_columns(
        charge_inc_ind=(charges > 50).cast(pl.Int8),
        hard_ft_inc_ind=(hard_ft.abs() >= 1).cast(pl.Int8),
        clocks_moved_ind=(book_2_pkup.abs().ge(CLOCK_DELTA_MIN) | avail_2_book.abs().ge(CLOCK_DELTA_MIN)).cast(pl.Int8),
    )
    if "equipment_type_avail" in out.columns and "equipment_type_48hr" in out.columns:
        out = out.with_columns(
            (
                pl.col("equipment_type_avail").is_not_null()
                & pl.col("equipment_type_48hr").is_not_null()
                & (pl.col("equipment_type_avail") != pl.col("equipment_type_48hr"))
            )
            .cast(pl.Int8)
            .alias("equip_change_ind")
        )
    else:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("equip_change_ind"))

    clhp_parts: list[pl.Expr] = []
    for delta_col, avail_col in CLHP_DELTA_SPECS:
        if delta_col not in out.columns:
            continue
        delta_abs = pl.col(delta_col).fill_null(0).abs()
        part = delta_abs.ge(CLHP_ABS_DELTA_MIN)
        if avail_col in out.columns:
            part = part | index_delta_fired_expr(delta_col, avail_col)
        clhp_parts.append(part)
    if clhp_parts:
        out = out.with_columns(pl.any_horizontal(clhp_parts).cast(pl.Int8).alias("clhp_change_ind"))
    else:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("clhp_change_ind"))

    out = out.with_columns(
        clock_only_ind=(
            (pl.col("clocks_moved_ind") == 1)
            & (pl.col("charge_inc_ind") == 0)
            & (pl.col("hard_ft_inc_ind") == 0)
            & (pl.col("equip_change_ind") == 0)
            & (pl.col("clhp_change_ind") == 0)
        ).cast(pl.Int8),
    )

    index_parts: list[pl.Expr] = []
    for delta_col, avail_col in INDEX_DELTA_SPECS:
        if delta_col not in out.columns:
            continue
        if avail_col in out.columns:
            index_parts.append(index_delta_fired_expr(delta_col, avail_col))

    if index_parts:
        out = out.with_columns(pl.any_horizontal(index_parts).cast(pl.Int8).alias("index_change_ind"))
    else:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("index_change_ind"))

    out = out.with_columns(
        (
            (pl.col("clocks_moved_ind") == 1)
            & (pl.col("index_change_ind") == 0)
            & (pl.col("charge_inc_ind") == 0)
            & (pl.col("hard_ft_inc_ind") == 0)
            & (pl.col("equip_change_ind") == 0)
            & (pl.col("clhp_change_ind") == 0)
        )
        .cast(pl.Int8)
        .alias("leadtime_isolated_ind"),
        ((pl.col("clocks_moved_ind") == 1) & (pl.col("index_change_ind") == 1))
        .cast(pl.Int8)
        .alias("index_clock_overlap_ind"),
        ((pl.col("index_change_ind") == 1) & (pl.col("clocks_moved_ind") == 0)).cast(pl.Int8).alias("index_only_ind"),
    )
    return out


def refresh_movement_derived_flags(df: pl.DataFrame) -> pl.DataFrame:
    """Recompute flags that depend on charge / equip / CLHP after path upgrades."""
    out = df
    need = {"clocks_moved_ind", "charge_inc_ind", "hard_ft_inc_ind", "equip_change_ind", "clhp_change_ind"}
    if not need.issubset(set(out.columns)):
        return out
    index_change = pl.col("index_change_ind") == 1 if "index_change_ind" in out.columns else pl.lit(False)
    out = out.with_columns(
        (
            (pl.col("clocks_moved_ind") == 1)
            & (pl.col("charge_inc_ind") == 0)
            & (pl.col("hard_ft_inc_ind") == 0)
            & (pl.col("equip_change_ind") == 0)
            & (pl.col("clhp_change_ind") == 0)
        )
        .cast(pl.Int8)
        .alias("clock_only_ind"),
        (
            (pl.col("clocks_moved_ind") == 1)
            & (pl.col("index_change_ind") == 0)
            & (pl.col("charge_inc_ind") == 0)
            & (pl.col("hard_ft_inc_ind") == 0)
            & (pl.col("equip_change_ind") == 0)
            & (pl.col("clhp_change_ind") == 0)
        )
        .cast(pl.Int8)
        .alias("leadtime_isolated_ind"),
        ((pl.col("clocks_moved_ind") == 1) & index_change).cast(pl.Int8).alias("index_clock_overlap_ind"),
    )
    return out


def normalize_movement_category(category: str | None) -> str:
    """Map legacy DeterministicChange labels to LeadtimeChange."""
    if category == LEGACY_LEADTIME_CATEGORY:
        return "LeadtimeChange"
    return category or "Unclassified"


def assign_movement_category(df: pl.DataFrame) -> pl.DataFrame:
    """Mutually exclusive primary category (public-facing movement taxonomy).

    Priority: ShipmentChange → DifficultyOverride → LeadtimeChange →
    RandomChange (index delta or catch-all residual) → Unclassified (no Davis row).
    """
    if "charge_inc_ind" not in df.columns:
        return df.with_columns(pl.lit("Unclassified").alias("primary_category"))

    davis_present = pl.col("total_charges_delta").is_not_null()
    clocks_moved = (
        pl.col("clocks_moved_ind") == 1
        if "clocks_moved_ind" in df.columns
        else (
            pl.col("book_2_pkup_delta").fill_null(0).abs().ge(CLOCK_DELTA_MIN)
            | pl.col("avail_2_book_delta").fill_null(0).abs().ge(CLOCK_DELTA_MIN)
        )
    )
    index_change = pl.col("index_change_ind") == 1 if "index_change_ind" in df.columns else pl.lit(False)
    path_taken_change = pl.col("path_taken_change_ind") == 1 if "path_taken_change_ind" in df.columns else pl.lit(False)
    shipment_change = pl.col("charge_inc_ind") == 1
    if "equip_change_ind" in df.columns:
        shipment_change = shipment_change | (pl.col("equip_change_ind") == 1)
    if "clhp_change_ind" in df.columns:
        shipment_change = shipment_change | (pl.col("clhp_change_ind") == 1)

    return df.with_columns(
        pl.when(~davis_present)
        .then(pl.lit("Unclassified"))
        .when(shipment_change)
        .then(pl.lit("ShipmentChange"))
        .when(pl.col("hard_ft_inc_ind") == 1)
        .then(pl.lit("DifficultyOverride"))
        .when(path_taken_change)
        .then(pl.lit("RandomChange"))
        .when(clocks_moved)
        .then(pl.lit("LeadtimeChange"))
        .when(index_change)
        .then(pl.lit("RandomChange"))
        .otherwise(pl.lit("RandomChange"))
        .alias("primary_category"),
    )


def add_endpoint_shifts(df: pl.DataFrame, prefix: str) -> pl.DataFrame:
    """Add shift_amt / shift_pct / flag columns for one price series."""
    avail, end = f"{prefix}_avail", f"{prefix}_48hr"
    amt = pl.col(end) - pl.col(avail)
    return df.with_columns(
        amt.alias(f"{prefix}_shift_amt"),
        pl.when(pl.col(avail).is_not_null() & (pl.col(avail) != 0))
        .then(amt / pl.col(avail))
        .otherwise(None)
        .alias(f"{prefix}_shift_pct"),
        amt.abs().gt(35).cast(pl.Int8).alias(f"{prefix}_shift_geq35"),
        pl.when(pl.col(avail).is_not_null() & (pl.col(avail) != 0))
        .then(amt.abs() / pl.col(avail).abs())
        .otherwise(None)
        .gt(0.02)
        .cast(pl.Int8)
        .alias(f"{prefix}_shift_2_per"),
    )


def enrich_shifts(df: pl.DataFrame) -> pl.DataFrame:
    """Add movement flags and per-series endpoint shift columns."""
    out = enrich_movement_flags(df)
    for prefix in PRICE_SERIES.values():
        out = add_endpoint_shifts(out, prefix)
    return out


def shift_summary(df: pl.DataFrame) -> pl.DataFrame:
    """Pooled mean $ shift and pct for each dial target + p50."""
    rows: list[dict[str, Any]] = []
    for label, prefix in PRICE_SERIES.items():
        rows.append(
            {
                "series": label,
                "n_loads": df.height,
                "avg_shift_amt": df[f"{prefix}_shift_amt"].mean(),
                "avg_shift_pct": df[f"{prefix}_shift_pct"].mean(),
                "pct_shift_geq_35": df[f"{prefix}_shift_geq35"].mean(),
                "pct_shift_geq_2pct": df[f"{prefix}_shift_2_per"].mean(),
            }
        )
    return pl.DataFrame(rows)


def charge_decomposition(
    df: pl.DataFrame,
    *,
    charge_col: str = "charge_inc_ind",
) -> pl.DataFrame:
    """Per-target charge-increase attribution (conditional mean decomposition)."""
    rows: list[dict[str, Any]] = []
    event_rate = df[charge_col].mean()
    for label, prefix in PRICE_SERIES.items():
        shift_col = f"{prefix}_shift_amt"
        pooled = df[shift_col].mean()
        stable = df.filter(pl.col(charge_col) == 0)[shift_col].mean()
        event = df.filter(pl.col(charge_col) == 1)[shift_col].mean()
        event_lift = event - stable if event is not None and stable is not None else None
        incr = event_lift * event_rate if event_lift is not None else None
        rows.append(
            {
                "series": label,
                "pooled_avg_shift": pooled,
                "stable_charges_avg_shift": stable,
                "charge_event_avg_shift": event,
                "charge_event_rate": event_rate,
                "charge_contribution_to_pooled": incr,
                "residual_after_stable": pooled - incr if incr is not None else None,
            }
        )
    return pl.DataFrame(rows)


def shift_by_segment(df: pl.DataFrame, segment_col: str) -> pl.DataFrame:
    """Mean shift_amt by segment for every target + p50."""
    agg_exprs = [pl.len().alias("n_loads")]
    for label, prefix in PRICE_SERIES.items():
        agg_exprs.append(pl.mean(f"{prefix}_shift_amt").alias(f"avg_{label}_shift_amt"))
    return df.group_by(segment_col).agg(agg_exprs).sort(segment_col)


def load_analysis_orders(
    start_date: str,
    end_date: str,
    *,
    cache_path: Path | str | None = None,
    force: bool = False,
) -> pl.DataFrame:
    """Cache-aware load: Snowflake on miss, enrich on read."""
    from dqt import is_fresh

    if cache_path is None:
        cache_path = Path(f"data/etp/analysis-davis-raw-{start_date}_{end_date}.parquet")
    cache_path = Path(cache_path)

    if not force and is_fresh(cache_path):
        raw = pl.read_parquet(cache_path)
    else:
        print(f"Begin reading snowflake shipments from {start_date} to {end_date}...")
        raw = read_orders(start_date, end_date)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        raw.write_parquet(cache_path, compression="zstd")
        print(f"wrote {raw.height:,d} raw rows ({start_date}..{end_date}) to {cache_path}")
    return enrich_shifts(raw)


__all__ = [
    "ANALYSIS_ORDERS_SQL",
    "CLHP_ABS_DELTA_MIN",
    "DEFAULT_SHIP_END",
    "DEFAULT_SHIP_START",
    "PRICE_SERIES",
    "add_endpoint_shifts",
    "analysis_orders_sql",
    "assign_movement_category",
    "charge_decomposition",
    "enrich_movement_flags",
    "enrich_shifts",
    "load_analysis_orders",
    "normalize_movement_category",
    "refresh_movement_derived_flags",
    "shift_by_segment",
    "shift_summary",
]

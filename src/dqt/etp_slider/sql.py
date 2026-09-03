"""ETP slider analysis window and SQL rendering."""

from __future__ import annotations

from pathlib import Path

import polars as pl

# Single place to set the cohort ship-date window (ship_date_coalesce, inclusive).
SHIP_DATE_START = "2025-01-01"
SHIP_DATE_END = "2026-08-21"

SQL_DIR = Path("SQL/etp-slider")
CTE_FILE = "etp-slider-cte.sql"
SURVIVAL_CTE_FILE = "etp-slider-survival-cte.sql"
TAIL_FILES = (
    "etp-slider-shift-select.sql",
    "etp-slider-per-load-select.sql",
    "etp-slider-history-select.sql",
)
SURVIVAL_TAIL_FILES = (
    "etp-slider-survival-per-load-select.sql",
    "etp-slider-survival-history-select.sql",
)
COMPOSITE_NAMES = (
    "etp-slider-shift.sql",
    "etp-slider-per-load.sql",
    "etp-slider-history.sql",
    "etp-slider.sql",
)
# etp-slider.sql is the same aggregate tail as shift
COMPOSITE_TAILS = (*TAIL_FILES, TAIL_FILES[0])


# Parquet cache filenames (under data/etp/)
PER_LOAD_CACHE = "etp-slider-per-load.parquet"
HISTORY_CACHE = "etp-slider-history.parquet"
SURVIVAL_PER_LOAD_CACHE = "etp-slider-survival-per-load.parquet"
SURVIVAL_HISTORY_CACHE = "etp-slider-survival-history.parquet"


def shift_aggregate(df_pl: pl.DataFrame) -> pl.DataFrame:
    """Endpoint shift summary — compute locally from cached per-load parquet."""
    return df_pl.select(
        pl.len().alias("n_loads"),
        pl.col("target1_shift_amt").mean().alias("avg_target1_shift_amt"),
        (pl.col("target1_shift_pct").mean() * 100).alias("avg_target1_shift_pct"),
        pl.col("target3_shift_amt").mean().alias("avg_target3_shift_amt"),
        (pl.col("target3_shift_pct").mean() * 100).alias("avg_target3_shift_pct"),
        pl.col("etp10_shift_amt").mean().alias("avg_etp10_shift_amt"),
        (pl.col("etp10_shift_pct").mean() * 100).alias("avg_etp10_shift_pct"),
        pl.col("etp50_shift_amt").mean().alias("avg_etp50_shift_amt"),
        (pl.col("etp50_shift_pct").mean() * 100).alias("avg_etp50_shift_pct"),
    )


def render_etp_slider_sql(
    text: str,
    *,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
) -> str:
    """Substitute cohort date placeholders (``str.format`` / ``query_sf`` params)."""
    return text.format(
        ship_date_start=ship_date_start or SHIP_DATE_START,
        ship_date_end=ship_date_end or SHIP_DATE_END,
    )


def _join_cte_tail(
    cte_text: str,
    tail_name: str,
    *,
    sql_dir: Path,
) -> str:
    tail = (sql_dir / tail_name).read_text().strip()
    tail_body = "\n".join(
        line for line in tail.splitlines() if not line.startswith("--")
    ).strip()
    return f"{cte_text}\n\n{tail_body}"


def slider_sql(
    tail_name: str,
    *,
    sql_dir: Path | None = None,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
    cte_file: str = CTE_FILE,
) -> str:
    """Concatenate shared CTE + final SELECT tail with date params applied."""
    root = sql_dir or SQL_DIR
    cte = render_etp_slider_sql(
        (root / cte_file).read_text().strip(),
        ship_date_start=ship_date_start,
        ship_date_end=ship_date_end,
    )
    return _join_cte_tail(cte, tail_name, sql_dir=root)


def survival_slider_sql(
    tail_name: str,
    *,
    sql_dir: Path | None = None,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
) -> str:
    """Survival cohort SQL (available_on_last + booked_on)."""
    return slider_sql(
        tail_name,
        sql_dir=sql_dir,
        ship_date_start=ship_date_start,
        ship_date_end=ship_date_end,
        cte_file=SURVIVAL_CTE_FILE,
    )


def write_composite_sql_files(*, sql_dir: Path | None = None) -> None:
    """Bake CTE + tail into standalone .sql files (for ad-hoc query_sf paths)."""
    root = sql_dir or SQL_DIR
    for tail_name, out_name in zip(COMPOSITE_TAILS, COMPOSITE_NAMES, strict=True):
        (root / out_name).write_text(slider_sql(tail_name, sql_dir=root) + "\n")


if __name__ == "__main__":
    write_composite_sql_files()
    print(f"Wrote composite SQL for {SHIP_DATE_START} .. {SHIP_DATE_END}")

"""SARIMA pp50 vs ETP pricing comparison on volatile (≥10%) problem loads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from dqt.etp_slider.movement.orders import LEGACY_LEADTIME_CATEGORY
from dqt.etp_slider.problem_loads.cat_report import with_attribution_category
from dqt.etp_slider.problem_loads.leadtime import (
    LC_CATEGORY,
    LEADTIME_ARCHETYPE_LABELS,
    LEADTIME_ARCHETYPE_ORDER,
    enrich_book_time_quotes,
    enrich_lc_davis_quotes,
    join_lc_carrier_cost,
)
from dqt.report_data import left_join_quantile_pack
from dqt.score.constants import COST_COL, DATE_COL, ID_COL

SARIMA_PP_COLS = ("sarima_pp_25", "sarima_pp_50", "sarima_pp_75")
EXPORT_ID_COLS = (
    "loadnumber",
    "ship_date",
    "booked_on_cst",
    "made_available_utc",
    "primary_category",
    "attribution_category",
    "path_archetype",
    "path_archetype_label",
    "leadtime_isolated_ind",
    "index_change_ind",
    "clock_only_ind",
)
EXPORT_SHIFT_COLS = (
    "etp50_avail",
    "etp50_48hr",
    "etp50_shift_amt",
    "etp50_shift_pct",
    "is_tail",
)
EXPORT_SARIMA_COLS = (
    "sarima_pp_25",
    "sarima_pp_50",
    "sarima_pp_75",
    "sarima_dial_at_book",
    "actual_dial_at_book",
)
EXPORT_SCORE_COLS = (
    "carrier_shipment_charges_total",
    "mae_etp_avail",
    "mae_etp_book",
    "mae_sarima_book",
    "att_etp_avail",
    "att_etp_book",
    "att_sarima_book",
    "gap_pp_etp_book",
    "gap_pp_sarima_book",
    "sarima_mae_delta_vs_etp_book",
)


def _join_book_metadata(
    frame: pl.DataFrame,
    *,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> pl.DataFrame:
    """Attach book date / CST from features for dial join."""
    from dqt import resolve_data_dir

    if features_path is None:
        features_path = resolve_data_dir(data_dir) / "features.parquet"
    features_path = Path(features_path)
    if not features_path.exists():
        return frame

    cols = [ID_COL]
    schema = pl.read_parquet(features_path, n_rows=0).columns
    for c in ("booked_on_cst", DATE_COL, "booked_on_date", "made_available_utc"):
        if c in schema and c not in frame.columns:
            cols.append(c)
    if len(cols) == 1:
        return frame

    meta = pl.read_parquet(features_path, columns=cols).rename({ID_COL: "loadnumber"})
    if DATE_COL not in meta.columns and "booked_on_date" in meta.columns:
        meta = meta.with_columns(pl.col("booked_on_date").alias(DATE_COL))
    return frame.join(meta, on="loadnumber", how="left", suffix="_feat")


def _join_path_archetype(
    frame: pl.DataFrame,
    lc_path: Path | str | None,
) -> pl.DataFrame:
    if lc_path is None or "path_archetype" in frame.columns:
        return frame
    lc_path = Path(lc_path)
    if not lc_path.exists():
        return frame
    lc_cols = pl.read_parquet(lc_path, n_rows=0).columns
    if "path_archetype" not in lc_cols:
        return frame
    arch = pl.read_parquet(lc_path, columns=["loadnumber", "path_archetype"])
    return frame.drop("path_archetype", strict=False).join(arch, on="loadnumber", how="left")


def _join_sarima_dial(
    frame: pl.DataFrame,
    dial_path: Path | str | None,
) -> pl.DataFrame:
    if dial_path is None or DATE_COL not in frame.columns:
        return frame
    dial_path = Path(dial_path)
    if not dial_path.exists():
        return frame

    dial = pl.read_parquet(dial_path)
    date_col = "booked_date" if "booked_date" in dial.columns else DATE_COL
    pred_col = "y_hat_sarima_cal" if "y_hat_sarima_cal" in dial.columns else "y_hat_sarima"
    if date_col not in dial.columns or pred_col not in dial.columns or "y" not in dial.columns:
        return frame

    sched = dial.select(
        pl.col(date_col).alias("_book_date"),
        pl.col(pred_col).alias("sarima_dial_at_book"),
        pl.col("y").alias("actual_dial_at_book"),
    ).unique(subset=["_book_date"])
    return (
        frame.with_columns(pl.col(DATE_COL).cast(pl.Date).alias("_book_date"))
        .join(sched, on="_book_date", how="left")
        .drop("_book_date")
    )


def _per_row_pricing_scores(frame: pl.DataFrame) -> pl.DataFrame:
    """Per-load MAE, attainment, gap vs realized carrier cost."""
    if COST_COL not in frame.columns:
        return frame

    cost = pl.col(COST_COL)
    valid_cost = cost.is_finite() & (cost > 0)

    def _mae(quote: str) -> pl.Expr:
        return (
            pl.when(valid_cost & pl.col(quote).is_finite() & (pl.col(quote) > 0))
            .then((pl.col(quote) - cost).abs())
            .otherwise(None)
            .alias(f"mae_{quote.replace('sarima_pp_50', 'sarima_book').replace('etp50_', 'etp_')}")
        )

    # Build with explicit names
    exprs: list[pl.Expr] = [
        pl.when(valid_cost & pl.col("etp50_avail").is_finite() & (pl.col("etp50_avail") > 0))
        .then((pl.col("etp50_avail") - cost).abs())
        .otherwise(None)
        .alias("mae_etp_avail"),
        pl.when(valid_cost & pl.col("etp50_book").is_finite() & (pl.col("etp50_book") > 0))
        .then((pl.col("etp50_book") - cost).abs())
        .otherwise(None)
        .alias("mae_etp_book"),
        pl.when(valid_cost & pl.col("sarima_pp_50").is_finite() & (pl.col("sarima_pp_50") > 0))
        .then((pl.col("sarima_pp_50") - cost).abs())
        .otherwise(None)
        .alias("mae_sarima_book"),
        pl.when(valid_cost & pl.col("etp50_avail").is_finite() & (pl.col("etp50_avail") > 0))
        .then((cost <= pl.col("etp50_avail")).cast(pl.Float64))
        .otherwise(None)
        .alias("att_etp_avail"),
        pl.when(valid_cost & pl.col("etp50_book").is_finite() & (pl.col("etp50_book") > 0))
        .then((cost <= pl.col("etp50_book")).cast(pl.Float64))
        .otherwise(None)
        .alias("att_etp_book"),
        pl.when(valid_cost & pl.col("sarima_pp_50").is_finite() & (pl.col("sarima_pp_50") > 0))
        .then((cost <= pl.col("sarima_pp_50")).cast(pl.Float64))
        .otherwise(None)
        .alias("att_sarima_book"),
    ]
    out = frame.with_columns(*exprs)
    out = out.with_columns(
        pl.when(pl.col("att_etp_book").is_not_null())
        .then((pl.col("att_etp_book") - 0.50) * 100.0)
        .otherwise(None)
        .alias("gap_pp_etp_book"),
        pl.when(pl.col("att_sarima_book").is_not_null())
        .then((pl.col("att_sarima_book") - 0.50) * 100.0)
        .otherwise(None)
        .alias("gap_pp_sarima_book"),
        pl.when(pl.col("mae_sarima_book").is_not_null() & pl.col("mae_etp_book").is_not_null())
        .then(pl.col("mae_sarima_book") - pl.col("mae_etp_book"))
        .otherwise(None)
        .alias("sarima_mae_delta_vs_etp_book"),
    )
    if COST_COL != "carrier_shipment_charges_total":
        out = out.rename({COST_COL: "carrier_shipment_charges_total"})
    return out


def build_sarima_etp_comparison_frame(
    labeled: pl.DataFrame,
    *,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    davis_cache: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
    sarima_pp_path: Path | str | None = None,
    dial_path: Path | str | None = None,
    lc_path: Path | str | None = None,
    problem_only: bool = True,
) -> pl.DataFrame:
    """Volatile loads with ETP vs SARIMA pp50 pricing scores at avail/book."""
    if labeled.is_empty() or "is_tail" not in labeled.columns:
        return pl.DataFrame()

    frame = labeled.filter(pl.col("is_tail").cast(pl.Boolean)) if problem_only else labeled
    if frame.is_empty():
        return pl.DataFrame()

    frame = join_lc_carrier_cost(frame, features_path=features_path, data_dir=data_dir)
    frame = _join_book_metadata(frame, features_path=features_path, data_dir=data_dir)
    frame = enrich_lc_davis_quotes(frame, davis_cache)
    if hist_path is not None:
        frame = enrich_book_time_quotes(
            frame,
            hist_path=hist_path,
            survival_path=survival_path,
            cache_path=book_quotes_cache,
        )
    frame = _join_path_archetype(frame, lc_path)

    if sarima_pp_path is not None and Path(sarima_pp_path).exists():
        pp = pl.read_parquet(sarima_pp_path)
        keep = ["loadnumber", *[c for c in SARIMA_PP_COLS if c in pp.columns]]
        frame = left_join_quantile_pack(frame, pp.select(keep), prefix="sarima_pp_")

    frame = _join_sarima_dial(frame, dial_path)
    frame = with_attribution_category(frame)
    frame = _per_row_pricing_scores(frame)

    if "path_archetype" in frame.columns:
        frame = frame.with_columns(
            pl.col("path_archetype").replace(LEADTIME_ARCHETYPE_LABELS).alias("path_archetype_label")
        )

    ordered = [
        c
        for c in (
            *EXPORT_ID_COLS,
            *EXPORT_SHIFT_COLS,
            *EXPORT_SARIMA_COLS,
            *EXPORT_SCORE_COLS,
        )
        if c in frame.columns
    ]
    extra = [c for c in frame.columns if c not in ordered]
    return frame.select(*ordered, *extra)


def _segment_summary(sub: pl.DataFrame) -> dict[str, Any]:
    if sub.is_empty():
        return {"n_loads": 0, "n_with_sarima": 0}

    n = sub.height
    has_sarima = sub.filter(pl.col("sarima_pp_50").is_finite() & (pl.col("sarima_pp_50") > 0))
    n_sarima = has_sarima.height

    def _mean(col: str) -> float | None:
        if col not in sub.columns:
            return None
        s = sub[col].drop_nulls()
        return round(float(s.mean()), 2) if s.len() else None

    def _med(col: str) -> float | None:
        if col not in sub.columns:
            return None
        s = sub[col].drop_nulls()
        return round(float(s.median()), 2) if s.len() else None

    comparable = has_sarima.filter(pl.col("mae_etp_book").is_not_null() & pl.col("mae_sarima_book").is_not_null())
    share_sarima_wins = None
    if comparable.height:
        share_sarima_wins = round(
            float((comparable["mae_sarima_book"] < comparable["mae_etp_book"]).mean()),
            4,
        )

    return {
        "n_loads": n,
        "n_with_sarima": n_sarima,
        "pct_with_sarima": round(n_sarima / n, 4) if n else 0.0,
        "avg_etp50_shift_amt": _mean("etp50_shift_amt"),
        "med_etp50_shift_amt": _med("etp50_shift_amt"),
        "avg_etp50_shift_pct": _mean("etp50_shift_pct"),
        "avg_mae_etp_avail": _mean("mae_etp_avail"),
        "avg_mae_etp_book": _mean("mae_etp_book"),
        "avg_mae_sarima_book": _mean("mae_sarima_book"),
        "share_sarima_mae_lt_etp_book": share_sarima_wins,
        "avg_sarima_dial_at_book": _mean("sarima_dial_at_book"),
        "avg_actual_dial_at_book": _mean("actual_dial_at_book"),
    }


def summarize_sarima_etp_comparison(frame: pl.DataFrame) -> dict[str, Any]:
    """Segment pivots for ops readout and JSON summary."""
    if frame.is_empty():
        return {"segments": [], "coverage": {"n_loads": 0, "n_with_sarima": 0}}

    segments: list[dict[str, Any]] = []

    def _add(segment: str, label: str, filt: pl.Expr) -> None:
        sub = frame.filter(filt)
        if sub.is_empty():
            return
        row = _segment_summary(sub)
        row["segment"] = segment
        row["label"] = label
        segments.append(row)

    _add("all_volatile", "All volatile (≥10%)", pl.lit(True))

    if "primary_category" in frame.columns:
        _add(
            "leadtime_primary",
            "Lead Time primary",
            pl.col("primary_category").replace(LEGACY_LEADTIME_CATEGORY, LC_CATEGORY) == LC_CATEGORY,
        )

    if "attribution_category" in frame.columns:
        _add("load_features", "Load Features", pl.col("attribution_category") == "LoadFeatures")
        _add(
            "leadtime_clock_only",
            "Lead Time (clocks only)",
            pl.col("attribution_category") == "LeadTimeClockOnly",
        )
        _add(
            "leadtime_index_cooccur",
            "Lead Time + index co-occur",
            pl.col("attribution_category") == "LeadTimeIndexCooccur",
        )

    if "leadtime_isolated_ind" in frame.columns:
        _add(
            "high_confidence_lc",
            "High-confidence LC (clock-only, no index)",
            pl.col("leadtime_isolated_ind") == 1,
        )

    if "path_archetype" in frame.columns:
        for arch in LEADTIME_ARCHETYPE_ORDER:
            if arch == "unknown":
                continue
            _add(
                f"path_{arch}",
                LEADTIME_ARCHETYPE_LABELS.get(arch, arch),
                pl.col("path_archetype") == arch,
            )

    n = frame.height
    n_sarima = frame.filter(pl.col("sarima_pp_50").is_finite() & (pl.col("sarima_pp_50") > 0)).height
    return {
        "segments": segments,
        "coverage": {
            "n_loads": n,
            "n_with_sarima": n_sarima,
            "pct_with_sarima": round(n_sarima / n, 4) if n else 0.0,
        },
        "interpretation": (
            "SARIMA pp50 is a book-date counterfactual dial quote; ETP shift is avail→48hr. "
            "Negative sarima_mae_delta_vs_etp_book means SARIMA was closer to realized cost at book."
        ),
    }


def export_sarima_etp_comparison(
    labeled: pl.DataFrame,
    *,
    out_dir: Path | str,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    davis_cache: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
    sarima_pp_path: Path | str | None = None,
    dial_path: Path | str | None = None,
    lc_path: Path | str | None = None,
    out_csv: Path | str | None = None,
    out_json: Path | str | None = None,
    problem_only: bool = True,
) -> dict[str, Any]:
    """Build frame, write CSV + summary JSON; return paths and summary."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frame = build_sarima_etp_comparison_frame(
        labeled,
        data_dir=data_dir,
        features_path=features_path,
        davis_cache=davis_cache,
        hist_path=hist_path,
        survival_path=survival_path,
        book_quotes_cache=book_quotes_cache,
        sarima_pp_path=sarima_pp_path,
        dial_path=dial_path,
        lc_path=lc_path,
        problem_only=problem_only,
    )
    summary = summarize_sarima_etp_comparison(frame)

    csv_path = Path(out_csv) if out_csv else out_dir / "sarima-vs-etp-volatile.csv"
    json_path = Path(out_json) if out_json else out_dir / "sarima-vs-etp-volatile-summary.json"
    parquet_path = out_dir / "sarima-vs-etp-volatile.parquet"

    if not frame.is_empty():
        frame.write_csv(csv_path)
        frame.write_parquet(parquet_path, compression="zstd")
    else:
        csv_path.write_text("")
        parquet_path.write_text("")

    json_path.write_text(json.dumps(summary, indent=2, default=str))

    return {
        "frame": frame,
        "summary": summary,
        "paths": {
            "csv": csv_path,
            "parquet": parquet_path,
            "summary_json": json_path,
        },
    }


__all__ = [
    "build_sarima_etp_comparison_frame",
    "export_sarima_etp_comparison",
    "summarize_sarima_etp_comparison",
]

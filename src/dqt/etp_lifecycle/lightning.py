"""Lightning / CLHP cost change tracking and threshold calibration."""

from __future__ import annotations

from typing import Any

import polars as pl

from dqt.etp_mart import CHARGE_PATH_COLS, consecutive_feature_deltas
from dqt.etp_slider.movement.feature_path import CLHP_PATH_EPS, CLHP_VALUE_COLS
from dqt.etp_slider.movement.orders import (
    CLHP_ABS_DELTA_MIN,
    INDEX_REL_DELTA_MIN,
    enrich_movement_flags,
)
from dqt.score.constants import ID_COL

LIGHTNING_VALUE_COLS: tuple[str, ...] = CLHP_VALUE_COLS + ("clhp_ma",)
DEFAULT_LIGHTNING_COL = "clhp_pred_tot_cost"
DEFAULT_ABS_USD = CLHP_ABS_DELTA_MIN
DEFAULT_REL_PCT = INDEX_REL_DELTA_MIN

PATH_COLS: tuple[str, ...] = CHARGE_PATH_COLS


def lightning_column_specs(value_cols: tuple[str, ...] | None = None) -> tuple[tuple[str, str, str], ...]:
    """(mart_column, delta_col, avail_col) triples present in Davis endpoint frames."""
    cols = value_cols or LIGHTNING_VALUE_COLS
    return tuple((c, f"{c}_delta", f"{c}_avail") for c in cols)


def _rel_pct_expr(delta_col: str, avail_col: str) -> pl.Expr:
    denom = pl.col(avail_col).fill_null(0).abs().clip(lower_bound=1e-6)
    return (pl.col(delta_col).fill_null(0).abs() / denom).alias(f"{delta_col}_rel_pct")


def enrich_endpoint_lightning(davis: pl.DataFrame) -> pl.DataFrame:
    """Add relative Lightning deltas and movement flags to a Davis endpoint frame."""
    if davis.is_empty():
        return davis
    exprs: list[pl.Expr] = []
    for col, delta_col, avail_col in lightning_column_specs():
        if delta_col in davis.columns and avail_col in davis.columns:
            exprs.append(_rel_pct_expr(delta_col, avail_col))
    out = davis.with_columns(exprs) if exprs else davis
    return enrich_movement_flags(out)


def lightning_material_expr(
    delta_col: str,
    avail_col: str,
    *,
    abs_usd: float = DEFAULT_ABS_USD,
    rel_pct: float = DEFAULT_REL_PCT,
) -> pl.Expr:
    """True when abs delta ≥ ``abs_usd`` OR rel change ≥ ``rel_pct`` vs avail baseline."""
    delta_abs = pl.col(delta_col).fill_null(0).abs()
    denom = pl.col(avail_col).fill_null(0).abs().clip(lower_bound=1e-6)
    return delta_abs.ge(abs_usd) | (delta_abs / denom).ge(rel_pct)


def flag_lightning_endpoint(
    df: pl.DataFrame,
    *,
    value_col: str = DEFAULT_LIGHTNING_COL,
    abs_usd: float = DEFAULT_ABS_USD,
    rel_pct: float = DEFAULT_REL_PCT,
) -> pl.DataFrame:
    """Per-row Lightning material-change indicator for one CLHP column."""
    delta_col = f"{value_col}_delta"
    avail_col = f"{value_col}_avail"
    if delta_col not in df.columns or avail_col not in df.columns:
        return df.with_columns(pl.lit(0).cast(pl.Int8).alias(f"{value_col}_material_ind"))
    fired = lightning_material_expr(delta_col, avail_col, abs_usd=abs_usd, rel_pct=rel_pct)
    return df.with_columns(fired.cast(pl.Int8).alias(f"{value_col}_material_ind"))


def build_lightning_calibration_frame(
    davis: pl.DataFrame,
    labeled: pl.DataFrame | None = None,
    *,
    value_col: str = DEFAULT_LIGHTNING_COL,
) -> pl.DataFrame:
    """Cohort frame for threshold sweeps: abs/rel Lightning shift vs movement labels."""
    delta_col = f"{value_col}_delta"
    avail_col = f"{value_col}_avail"
    if davis.is_empty() or delta_col not in davis.columns:
        return pl.DataFrame()

    frame = enrich_endpoint_lightning(davis)
    rel_col = f"{delta_col}_rel_pct"
    if rel_col not in frame.columns:
        frame = frame.with_columns(_rel_pct_expr(delta_col, avail_col))

    label_cols = ("primary_category", "is_tail", "etp50_shift_pct")
    if labeled is not None and not labeled.is_empty() and "primary_category" in labeled.columns:
        label_select = [ID_COL, *[c for c in label_cols if c in labeled.columns]]
        frame = frame.drop(
            [c for c in label_cols if c in frame.columns],
            strict=False,
        ).join(labeled.select(label_select).unique(ID_COL), on=ID_COL, how="left")

    shipment_expr = (
        (pl.col("primary_category") == "ShipmentChange").cast(pl.Int8)
        if "primary_category" in frame.columns
        else pl.lit(0).cast(pl.Int8)
    )
    frame = frame.with_columns(
        shipment_expr.alias("shipment_change_ind"),
        (pl.col(delta_col).fill_null(0).abs()).alias(f"{value_col}_abs_delta"),
    )

    core = [ID_COL, delta_col, avail_col, rel_col, "clhp_change_ind", "charge_inc_ind"]
    optional = [
        "clhp_pred_line_delta",
        "clhp_pred_line_avail",
        *label_cols,
        f"{value_col}_abs_delta",
        "shipment_change_ind",
    ]
    out_cols: list[str] = []
    for c in core + optional:
        if c in frame.columns and c not in out_cols:
            out_cols.append(c)
    return frame.select(out_cols)


def clhp_miss_driver_expr() -> pl.Expr:
    """Why a ShipmentChange-labeled load missed the CLHP Lightning gate."""
    charge = pl.col("charge_inc_ind").fill_null(0) == 1
    equip = pl.col("equip_change_ind").fill_null(0) == 1
    clocks = pl.col("clocks_moved_ind").fill_null(0) == 1
    return (
        pl.when(charge & equip)
        .then(pl.lit("charge_and_equip"))
        .when(charge)
        .then(pl.lit("charge_inc"))
        .when(equip)
        .then(pl.lit("equip"))
        .when(clocks)
        .then(pl.lit("clocks_only_label_drift"))
        .otherwise(pl.lit("other"))
    )


def join_mde_context(
    df: pl.DataFrame,
    mde_events: pl.DataFrame | None,
    *,
    id_col: str = ID_COL,
) -> pl.DataFrame:
    """Add ``mde_applied_ind`` and ``n_mde_events`` from timeline event rows."""
    if df.is_empty():
        return df.with_columns(
            pl.lit(0).cast(pl.Int8).alias("mde_applied_ind"),
            pl.lit(0).cast(pl.UInt32).alias("n_mde_events"),
        )
    if mde_events is None or mde_events.is_empty() or id_col not in mde_events.columns:
        return df.with_columns(
            pl.lit(0).cast(pl.Int8).alias("mde_applied_ind"),
            pl.lit(0).cast(pl.UInt32).alias("n_mde_events"),
        )

    from dqt.etp_slider.drift.mde_timeline import aggregate_mde_events_by_load

    agg = aggregate_mde_events_by_load(mde_events)
    out = df.drop([c for c in ("mde_applied_ind", "n_mde_events") if c in df.columns], strict=False).join(
        agg.rename({"mde_timeline_ind": "mde_applied_ind", "n_mde_timeline_events": "n_mde_events"}),
        on=id_col,
        how="left",
    )
    return out.with_columns(
        pl.col("mde_applied_ind").fill_null(0).cast(pl.Int8),
        pl.col("n_mde_events").fill_null(0).cast(pl.UInt32),
    )


def build_clhp_miss_frame(
    davis: pl.DataFrame,
    labeled: pl.DataFrame | None = None,
    *,
    value_col: str = DEFAULT_LIGHTNING_COL,
    mde_events: pl.DataFrame | None = None,
    time_col: str = "made_available_utc",
) -> pl.DataFrame:
    """ShipmentChange loads that missed the CLHP gate, with miss-driver and context flags."""
    if davis.is_empty():
        return pl.DataFrame()

    enriched = enrich_endpoint_lightning(davis)
    cal = build_lightning_calibration_frame(davis, labeled, value_col=value_col)
    flag_cols = [
        c
        for c in (
            "charge_inc_ind",
            "equip_change_ind",
            "clocks_moved_ind",
            "index_change_ind",
            "hard_ft_inc_ind",
            time_col,
            "ship_date",
            "pickup_appt_latest_utc",
        )
        if c in enriched.columns
    ]
    miss = (
        cal.filter((pl.col("primary_category") == "ShipmentChange") & (pl.col("clhp_change_ind") == 0))
        .join(enriched.select([ID_COL, *flag_cols]), on=ID_COL, how="left")
        .with_columns(clhp_miss_driver_expr().alias("miss_driver"))
    )
    if time_col not in miss.columns and "ship_date" in miss.columns:
        miss = miss.with_columns(pl.col("ship_date").alias(time_col))

    from dqt.holidays import tag_holiday_windows

    miss = tag_holiday_windows(miss, time_col)
    miss = join_mde_context(miss, mde_events)
    miss = miss.with_columns(
        pl.col(time_col).dt.truncate("1w").alias("week"),
        pl.col("mde_applied_ind").fill_null(0).cast(pl.Int8).alias("mde_context"),
    )
    return miss.with_columns(
        pl.when(pl.col("in_holiday_window"))
        .then(pl.lit("holiday_window"))
        .when(pl.col("mde_context") == 1)
        .then(pl.lit("mde_applied"))
        .otherwise(pl.lit("normal"))
        .alias("event_context"),
    )


def aggregate_clhp_misses_weekly(miss: pl.DataFrame) -> pl.DataFrame:
    """Weekly stacked counts by ``miss_driver``."""
    if miss.is_empty() or "week" not in miss.columns:
        return pl.DataFrame()
    return (
        miss.group_by("week", "miss_driver")
        .len()
        .rename({"len": "n"})
        .sort("week", "miss_driver")
    )


def summarize_clhp_miss_context(miss: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Miss counts by context, plus cross-tab of context × miss_driver."""
    if miss.is_empty():
        return pl.DataFrame()
    total = miss.height
    rows: list[dict[str, object]] = []
    for ctx in ("holiday_window", "mde_applied", "normal"):
        if ctx == "holiday_window":
            sub = miss.filter(pl.col("in_holiday_window"))
        elif ctx == "mde_applied":
            sub = miss.filter(pl.col("mde_applied_ind") == 1)
        else:
            sub = miss.filter(~pl.col("in_holiday_window") & (pl.col("mde_applied_ind") == 0))
        rows.append(
            {
                "context": ctx,
                "n_misses": sub.height,
                "pct_of_misses": sub.height / total if total else None,
            }
        )
    by_driver = (
        miss.group_by("event_context", "miss_driver")
        .len()
        .rename({"len": "n"})
        .sort("event_context", "miss_driver")
    )
    return pl.DataFrame(rows), by_driver


def sweep_lightning_thresholds(
    frame: pl.DataFrame,
    *,
    value_col: str = DEFAULT_LIGHTNING_COL,
    pct_grid: list[float] | None = None,
    abs_usd: float = DEFAULT_ABS_USD,
    truth_col: str = "clhp_change_ind",
) -> pl.DataFrame:
    """Grid search: what rel-% cutoff reproduces ``truth_col`` / shipment labels."""
    delta_col = f"{value_col}_delta"
    avail_col = f"{value_col}_avail"
    if frame.is_empty() or delta_col not in frame.columns:
        return pl.DataFrame()

    grid = pct_grid or [0.02, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
    rows: list[dict[str, Any]] = []
    n = frame.height
    truth_expr = pl.col(truth_col).fill_null(0) == 1 if truth_col in frame.columns else pl.lit(False)
    shipment_expr = (
        pl.col("shipment_change_ind").fill_null(0) == 1
        if "shipment_change_ind" in frame.columns
        else pl.lit(False)
    )

    for pct in grid:
        pred = lightning_material_expr(delta_col, avail_col, abs_usd=abs_usd, rel_pct=pct)
        flagged = frame.filter(pred)
        n_flag = flagged.height
        if truth_col in frame.columns:
            tp = frame.filter(pred & truth_expr).height
            fn = frame.filter(~pred & truth_expr).height
            prec = tp / n_flag if n_flag else None
            rec = tp / (tp + fn) if (tp + fn) else None
        else:
            tp = fn = prec = rec = None

        ship_tp = frame.filter(pred & shipment_expr).height if "shipment_change_ind" in frame.columns else None
        rows.append(
            {
                "rel_pct_threshold": pct,
                "abs_usd_gate": abs_usd,
                "n_flagged": n_flag,
                "pct_cohort_flagged": n_flag / n if n else None,
                "precision_vs_clhp_change_ind": prec,
                "recall_vs_clhp_change_ind": rec,
                "n_shipment_change_flagged": ship_tp,
            }
        )
    return pl.DataFrame(rows)


def _path_change_rows(snaps: pl.DataFrame, loadnumber: int) -> list[dict[str, Any]]:
    if snaps.is_empty():
        return []
    sub = snaps.filter(pl.col(ID_COL) == loadnumber).sort("snapshot_utc")
    rows: list[dict[str, Any]] = []
    for col in PATH_COLS:
        if col not in sub.columns:
            continue
        prev_val: Any = None
        for row in sub.iter_rows(named=True):
            cur = row.get(col)
            if cur is None:
                continue
            if prev_val is not None and cur != prev_val:
                rows.append(
                    {
                        "ts": row.get("snapshot_utc"),
                        "hours_before_pickup": row.get("hours_before_pickup"),
                        "hours_since_available": row.get("hours_since_available"),
                        "metric": col,
                        "delta_usd": None,
                        "delta_other": None,
                        "category": "RandomChange" if col == "path_taken" else "ShipmentChange",
                        "driver_text": f"{col} {prev_val}→{cur}",
                        "confidence": "high",
                        "evidence_cols": [col],
                        "is_pricing_driver": True,
                    }
                )
            prev_val = cur
    return rows


def lightning_events_from_deltas(
    feat_deltas: pl.DataFrame,
    *,
    abs_usd: float = DEFAULT_ABS_USD,
    rel_pct: float = DEFAULT_REL_PCT,
    etp_co_move_usd: float = 25.0,
    require_etp_co_move: bool = False,
) -> list[dict[str, Any]]:
    """Hourly consecutive Lightning cost moves from feature snapshot deltas."""
    if feat_deltas.is_empty():
        return []

    events: list[dict[str, Any]] = []
    for row in feat_deltas.iter_rows(named=True):
        detp = row.get("delta_etp50")
        if require_etp_co_move and (detp is None or abs(float(detp)) < etp_co_move_usd):
            continue

        for col in LIGHTNING_VALUE_COLS:
            dcol = f"delta_{col}"
            if dcol not in row or row[dcol] is None:
                continue
            delta = float(row[dcol])
            prior = row.get(f"{col}_t0")
            if prior is None:
                prior = row.get(col)
            rel = abs(delta) / abs(float(prior)) if prior not in (None, 0) else None
            material = abs(delta) >= abs_usd or (rel is not None and rel >= rel_pct)
            if not material:
                continue

            rel_note = f" ({rel * 100:.0f}% vs prior snap)" if rel is not None else ""
            etp_note = f", ETP {float(detp):+,.0f}" if detp is not None else ""
            events.append(
                {
                    "ts": row.get("snapshot_utc_t1"),
                    "hours_before_pickup": row.get("hours_before_pickup"),
                    "hours_since_available": row.get("hours_since_available"),
                    "metric": col,
                    "delta_usd": delta,
                    "delta_other": rel,
                    "category": "ShipmentChange",
                    "driver_text": f"Lightning {col} {delta:+,.0f}{rel_note}{etp_note}",
                    "confidence": "high" if (rel or 0) >= rel_pct else "medium",
                    "evidence_cols": [dcol, "delta_etp50"] if detp is not None else [dcol],
                    "is_pricing_driver": True,
                }
            )
    return events


def lightning_endpoint_events(
    davis_row: dict[str, Any] | None,
    *,
    abs_usd: float = DEFAULT_ABS_USD,
    rel_pct: float = DEFAULT_REL_PCT,
) -> list[dict[str, Any]]:
    """Avail→48hr Lightning endpoint shift summary for the material ledger."""
    if not davis_row:
        return []
    events: list[dict[str, Any]] = []
    for col, delta_col, avail_col in lightning_column_specs():
        if delta_col not in davis_row or davis_row[delta_col] is None:
            continue
        delta = float(davis_row[delta_col])
        avail = davis_row.get(avail_col)
        rel = abs(delta) / abs(float(avail)) if avail not in (None, 0) else None
        if abs(delta) < abs_usd and (rel is None or rel < rel_pct):
            continue
        rel_note = f" ({rel * 100:.0f}% vs avail)" if rel is not None else ""
        events.append(
            {
                "metric": col,
                "delta_usd": delta,
                "delta_other": rel,
                "category": "ShipmentChange",
                "driver_text": f"Lightning {col} avail→48hr {delta:+,.0f}{rel_note}",
                "confidence": "high",
                "evidence_cols": [delta_col, avail_col],
                "is_pricing_driver": True,
            }
        )
    return events


def lightning_path_checkpoint_events(
    feat_snaps: pl.DataFrame,
    loadnumber: int,
    *,
    eps: float = CLHP_PATH_EPS,
) -> list[dict[str, Any]]:
    """Mid-path CLHP checkpoint jumps (matches drift explorer path scan)."""
    from itertools import pairwise

    from dqt.etp_slider.movement.feature_path import (
        PATH_MARK_HRS,
        _checkpoint_clhp_values,
    )
    from dqt.etp_timeline import MARK_LABELS

    cps = _checkpoint_clhp_values(feat_snaps, loadnumber)
    if not cps:
        return []
    events: list[dict[str, Any]] = []
    col = next((c for c in CLHP_VALUE_COLS if c in feat_snaps.columns), "clhp_pred_tot_cost")
    for m0, m1 in pairwise(PATH_MARK_HRS):
        if m0 not in cps or m1 not in cps:
            continue
        delta = cps[m1] - cps[m0]
        if abs(delta) < eps:
            continue
        base = abs(cps[m0]) or 1e-6
        rel = abs(delta) / base
        events.append(
            {
                "hours_before_pickup": m1 if m1 != 999 else None,
                "metric": col,
                "delta_usd": delta,
                "delta_other": rel,
                "category": "ShipmentChange",
                "driver_text": (
                    f"Lightning path {col} {delta:+,.0f} ({rel * 100:.0f}%) "
                    f"@ {MARK_LABELS.get(m1, m1)}"
                ),
                "confidence": "high",
                "evidence_cols": [col],
                "is_pricing_driver": True,
            }
        )
    return events


def build_lightning_tracking(
    feat_snaps: pl.DataFrame,
    loadnumber: int,
    *,
    davis_row: dict[str, Any] | None = None,
    abs_usd: float = DEFAULT_ABS_USD,
    rel_pct: float = DEFAULT_REL_PCT,
) -> dict[str, Any]:
    """Aggregate Lightning change signals for one load."""
    sub = feat_snaps.filter(pl.col(ID_COL) == loadnumber) if not feat_snaps.is_empty() else pl.DataFrame()
    deltas = consecutive_feature_deltas(sub) if not sub.is_empty() else pl.DataFrame()
    hourly = lightning_events_from_deltas(deltas, abs_usd=abs_usd, rel_pct=rel_pct)
    endpoint = lightning_endpoint_events(davis_row, abs_usd=abs_usd, rel_pct=rel_pct)
    path_cp = lightning_path_checkpoint_events(sub, loadnumber) if not sub.is_empty() else []
    path_meta = _path_change_rows(sub, loadnumber) if not sub.is_empty() else []

    endpoint_summary: dict[str, Any] = {}
    if davis_row:
        for col, delta_col, avail_col in lightning_column_specs():
            if delta_col in davis_row:
                endpoint_summary[f"{col}_delta"] = davis_row.get(delta_col)
                endpoint_summary[f"{col}_avail"] = davis_row.get(avail_col)
                endpoint_summary[f"{col}_rel_pct"] = (
                    abs(float(davis_row[delta_col])) / abs(float(davis_row[avail_col]))
                    if davis_row.get(avail_col) not in (None, 0) and davis_row.get(delta_col) is not None
                    else None
                )
        endpoint_summary["clhp_change_ind"] = davis_row.get("clhp_change_ind")

    return {
        "hourly_events": hourly,
        "endpoint_events": endpoint,
        "path_checkpoint_events": path_cp,
        "path_meta_events": path_meta,
        "endpoint_summary": endpoint_summary,
        "feat_deltas": deltas,
    }


__all__ = [
    "DEFAULT_ABS_USD",
    "DEFAULT_LIGHTNING_COL",
    "DEFAULT_REL_PCT",
    "LIGHTNING_VALUE_COLS",
    "aggregate_clhp_misses_weekly",
    "build_clhp_miss_frame",
    "build_lightning_calibration_frame",
    "build_lightning_tracking",
    "clhp_miss_driver_expr",
    "enrich_endpoint_lightning",
    "flag_lightning_endpoint",
    "join_mde_context",
    "lightning_column_specs",
    "lightning_endpoint_events",
    "lightning_events_from_deltas",
    "lightning_material_expr",
    "lightning_path_checkpoint_events",
    "summarize_clhp_miss_context",
    "sweep_lightning_thresholds",
]

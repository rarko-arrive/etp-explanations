"""Self-contained HTML report: indexed ETP/slider drift on long-lead loads.

Rebuilds the Chart.js artifact in ``documentation/etp-slider/reports/`` —
drift curve (indexed to Available = 100), cohort attrition, window bucket
table, and avg-vs-median bars. Checkpoints are hours-to-pickup marks
(168 → 24) from ``etp-slider-history.parquet``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

from dqt.etp_slider.movement.orders import (
    LEGACY_LEADTIME_CATEGORY,
    normalize_movement_category,
)
from dqt.etp_slider.sql import SHIP_DATE_END, SHIP_DATE_START

DEFAULT_LEAD_MIN_DAYS = 7
DEFAULT_LEAD_MAX_DAYS = 14
DEFAULT_AVAIL_START = "2026-07-27"
DEFAULT_AVAIL_END = "2026-08-21"

# Preset report variants (see scripts/build_etp_drift_report.py --variant).
BRADEN_SHIP_START = "2026-06-01"
BRADEN_SHIP_END = "2026-08-21"

REPORT_VARIANTS: dict[str, dict[str, Any]] = {
    "braden": {
        "label": "Braden window",
        "slug_suffix": "_braden",
        "avail_start": DEFAULT_AVAIL_START,
        "avail_end": DEFAULT_AVAIL_END,
        "ship_date_start": BRADEN_SHIP_START,
        "ship_date_end": BRADEN_SHIP_END,
        "no_avail_filter": False,
    },
    "mart-max": {
        "label": "Mart max eligible",
        "slug_suffix": "_mart_max",
        "no_avail_filter": True,
    },
}

MART_MIN_BOOKING_HRS = 72
HISTORY_END_HRS_BEFORE_PICKUP = 24
CHECKPOINT_HRS: tuple[int, ...] = (168, 144, 120, 96, 72, 48, 24)
MARK_HRS: tuple[int, ...] = (999, *CHECKPOINT_HRS)
MARK_LABELS: dict[int, str] = {
    999: "Available",
    168: "7 days out",
    144: "6 days out",
    120: "5 days out",
    96: "4 days out",
    72: "3 days out",
    48: "2 days out",
    24: "1 day out",
}
_MARK_ORDER: dict[int, int] = {h: i for i, h in enumerate(MARK_HRS)}


def _sort_by_mark_hrs(df: pl.DataFrame) -> pl.DataFrame:
    """Sort checkpoint rows left-to-right: Available → 7d out → … → 1d out."""
    return (
        df.with_columns(pl.col("mark_hrs").replace(_MARK_ORDER).alias("_ord"))
        .sort("_ord")
        .drop("_ord")
    )


BUCKET_LABELS: tuple[str, ...] = (
    "14 to 7 days",
    "7 to 6 days",
    "6 to 5 days",
    "5 to 4 days",
    "4 to 3 days",
    "3 to 2 days",
    "2 to 1 days",
)
METRICS: tuple[tuple[str, str, str], ...] = (
    ("target1", "audit", "TARGET1"),
    ("target3", "audit", "TARGET3"),
    ("etp10", "model", "ETP10"),
    ("etp50", "model", "ETP50"),
)


def lead_window_ids(
    per_load: pl.DataFrame,
    *,
    min_days: float = DEFAULT_LEAD_MIN_DAYS,
    max_days: float = DEFAULT_LEAD_MAX_DAYS,
    avail_start: date | None = None,
    avail_end: date | None = None,
) -> pl.DataFrame:
    """Loadnumbers in booking window, optionally filtered by made-available date."""
    if "booking_window_hrs" not in per_load.columns:
        raise ValueError("per_load missing booking_window_hrs")
    lo_h = min_days * 24
    hi_h = max_days * 24
    filt = per_load.filter(
        pl.col("booking_window_hrs").is_between(lo_h, hi_h, closed="both")
    )
    if avail_start is not None:
        filt = filt.filter(
            pl.col("made_available_utc")
            >= pl.lit(datetime.combine(avail_start, datetime.min.time()))
        )
    if avail_end is not None:
        end_dt = datetime.combine(avail_end, datetime.max.time())
        filt = filt.filter(pl.col("made_available_utc") <= pl.lit(end_dt))
    return filt.select("loadnumber").unique()


def _baseline(df: pl.DataFrame, col: str) -> pl.DataFrame:
    return (
        df.filter(pl.col(col).is_not_null())
        .sort(["loadnumber", "hours_since_available", "snapshot_utc"])
        .group_by("loadnumber")
        .agg(pl.col(col).first().alias(f"{col}_base"))
    )


def _at_mark(df: pl.DataFrame, col: str, mark_hrs: int) -> pl.DataFrame:
    sub = df.filter(pl.col(col).is_not_null())
    sort_cols = ["loadnumber"]
    if "hours_since_available" in sub.columns:
        sort_cols.append("hours_since_available")
    if "snapshot_utc" in sub.columns:
        sort_cols.append("snapshot_utc")
    if mark_hrs == 999:
        return (
            sub.sort(sort_cols)
            .group_by("loadnumber")
            .agg(pl.col(col).first().alias(col))
        )
    return (
        sub.filter(pl.col("hours_before_pickup") >= mark_hrs)
        .sort(["loadnumber", "hours_before_pickup"])
        .group_by("loadnumber")
        .agg(pl.col(col).first().alias(col))
    )


def _indexed_at_marks(
    hist: pl.DataFrame,
    load_ids: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    """Per-metric indexed values (Available = 100) for each checkpoint."""
    h = hist.join(load_ids, on="loadnumber", how="inner")
    model = h.filter(pl.col("source") == "model")
    audit = h.filter(pl.col("source") == "audit")
    out: dict[str, pl.DataFrame] = {}

    for col, src, key in METRICS:
        df = model if src == "model" else audit
        base = _baseline(df, col)
        parts: list[pl.DataFrame] = []
        for mark in MARK_HRS:
            snap = _at_mark(df, col, mark).join(base, on="loadnumber", how="inner")
            snap = snap.filter(pl.col(f"{col}_base") > 0)
            parts.append(
                snap.select(
                    "loadnumber",
                    pl.lit(mark).alias("mark_hrs"),
                    (pl.col(col) / pl.col(f"{col}_base") * 100).alias("idx"),
                    pl.col(col).alias("val"),
                    pl.col(f"{col}_base").alias("base"),
                )
            )
        out[key] = pl.concat(parts)
    return out


def build_curve(indexed: dict[str, pl.DataFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mark in MARK_HRS:
        row: dict[str, Any] = {
            "MARK_HRS": mark,
            "MARK_LABEL": MARK_LABELS[mark],
        }
        etp50 = indexed["ETP50"].filter(pl.col("mark_hrs") == mark)
        row["N_LOADS"] = etp50.height
        for _col, _src, key in METRICS:
            sub = indexed[key].filter(pl.col("mark_hrs") == mark)
            row[f"{key}_AVG_IDX"] = float(sub["idx"].mean() or 100.0)
            row[f"{key}_MED_IDX"] = float(sub["idx"].median() or 100.0)
        row["ETP50_P25_IDX"] = float(etp50["idx"].quantile(0.25) or 100.0)
        row["ETP50_P75_IDX"] = float(etp50["idx"].quantile(0.75) or 100.0)
        rows.append(row)
    return rows


def build_summary(indexed: dict[str, pl.DataFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pairs = list(pairwise(MARK_HRS))
    for (m0, m1), bucket in zip(pairs, BUCKET_LABELS, strict=True):
        row: dict[str, Any] = {"BUCKET": bucket}
        n_targets: int | None = None
        n_etp: int | None = None
        for _col, _src, key in METRICS:
            sub = indexed[key]
            left = sub.filter(pl.col("mark_hrs") == m0).select(
                "loadnumber", pl.col("val").alias("v0"), pl.col("base")
            )
            right = sub.filter(pl.col("mark_hrs") == m1).select(
                "loadnumber", pl.col("val").alias("v1")
            )
            j = left.join(right, on="loadnumber", how="inner")
            amt = j["v1"] - j["v0"]
            pct = amt / j["v0"]
            if key.startswith("TARGET"):
                n_targets = j.height
            else:
                n_etp = j.height
            row[f"AVG_{key}_SHIFT_AMT"] = float(amt.mean() or 0.0)
            row[f"MED_{key}_SHIFT_AMT"] = float(amt.median() or 0.0)
            row[f"AVG_{key}_SHIFT_PCT"] = float(pct.mean() or 0.0)
            row[f"MED_{key}_SHIFT_PCT"] = float(pct.median() or 0.0)
        row["N_LOADS_TARGETS"] = n_targets or 0
        row["N_LOADS_ETP"] = n_etp or 0
        rows.append(row)
    return rows


def build_cohort_meta(
    per_load: pl.DataFrame,
    cohort_ids: pl.DataFrame,
    curve: list[dict[str, Any]],
    *,
    lead_min_days: float,
    lead_max_days: float,
    avail_start: date | None,
    avail_end: date | None,
) -> dict[str, Any]:
    """Report metadata derived from the filtered cohort and mart bounds."""
    cohort = per_load.join(cohort_ids, on="loadnumber", how="inner")
    n0 = curve[0]["N_LOADS"] if curve else cohort.height
    n_last = curve[-1]["N_LOADS"] if curve else cohort.height
    n_168 = next((r["N_LOADS"] for r in curve if r["MARK_HRS"] == 168), n0)

    avail_min = cohort["made_available_utc"].min()
    avail_max = cohort["made_available_utc"].max()
    meta: dict[str, Any] = {
        "n_loads": cohort.height,
        "lead_min_days": lead_min_days,
        "lead_max_days": lead_max_days,
        "lead_min_hrs": int(lead_min_days * 24),
        "lead_max_hrs": int(lead_max_days * 24),
        "avail_start": avail_start.isoformat() if avail_start else None,
        "avail_end": avail_end.isoformat() if avail_end else None,
        "cohort_avail_min": avail_min.isoformat(sep=" ") if avail_min else None,
        "cohort_avail_max": avail_max.isoformat(sep=" ") if avail_max else None,
        "mart_ship_start": SHIP_DATE_START,
        "mart_ship_end": SHIP_DATE_END,
        "mart_n_loads": per_load.height,
        "mart_avail_min": (
            per_load["made_available_utc"].min().isoformat(sep=" ")
            if per_load.height
            else None
        ),
        "mart_avail_max": (
            per_load["made_available_utc"].max().isoformat(sep=" ")
            if per_load.height
            else None
        ),
        "n_at_availability": n0,
        "n_at_7d_out": n_168,
        "n_at_24h_out": n_last,
        "pct_at_24h_out": round(100 * n_last / n0, 1) if n0 else 0.0,
        "attrition_flat": bool(n0 and n_last / n0 >= 0.99),
        "cohort_mode": "mart",
        "attrition_title": "Telemetry coverage at checkpoints",
        "attrition_y_label": "Loads with ETP50 at checkpoint",
        "checkpoints_hrs": list(CHECKPOINT_HRS),
        "generated_utc": datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    if curve and len(curve) > 1:
        meta["first_week_etp50_avg_pct"] = round(curve[1]["ETP50_AVG_IDX"] - 100, 2)
        meta["endpoint_etp50_avg_pct"] = round(curve[-1]["ETP50_AVG_IDX"] - 100, 2)
        meta["first_week_etp50_med_pct"] = round(curve[1]["ETP50_MED_IDX"] - 100, 2)
    return meta


def _enrich_summary_meta(meta: dict[str, Any], summary: list[dict[str, Any]]) -> None:
    """Attach window-table stats used in chart notes."""
    meta["etp50_windows"] = len(summary)
    meta["etp50_med_zero_windows"] = sum(
        1 for r in summary if r.get("MED_ETP50_SHIFT_AMT", 0) == 0
    )
    meta["etp50_med_max_amt"] = max(
        (r.get("MED_ETP50_SHIFT_AMT", 0) for r in summary), default=0.0
    )


MAX_EXPLORER_LOADS_DEFAULT = 5000
HC_GALLERY_SIZE_DEFAULT = 10
EXPLORER_METRICS: tuple[str, ...] = ("ETP50", "ETP10", "TARGET1", "TARGET3")

_CATALOG_SIGNAL_COLS: tuple[str, ...] = (
    "total_charges_delta",
    "hard_ft_delta",
    "book_2_pkup_delta",
    "avail_2_book_delta",
    "dat_rate_delta",
    "fuel_cost_delta",
    "lag7_cpm_delta",
    "clhp_pred_tot_cost_delta",
    "clhp_pred_line_delta",
)
_INDEX_BASELINE_COLS: tuple[tuple[str, str], ...] = (
    ("dat_rate_delta", "dat_rate_avail"),
    ("fuel_cost_delta", "fuel_cost_avail"),
    ("lag7_cpm_delta", "lag7_cpm_avail"),
)
_CATALOG_STRING_COLS: tuple[str, ...] = (
    "equipment_type_avail",
    "equipment_type_48hr",
)
_CATALOG_FLAG_COLS: tuple[str, ...] = (
    "charge_inc_ind",
    "equip_change_ind",
    "clhp_change_ind",
    "hard_ft_inc_ind",
    "clocks_moved_ind",
    "clock_only_ind",
    "index_change_ind",
    "leadtime_isolated_ind",
    "index_clock_overlap_ind",
)

CATEGORY_ORDER: tuple[str, ...] = (
    "ShipmentChange",
    "LeadtimeChange",
    "RandomChange",
    "DifficultyOverride",
    "Unclassified",
)
CATEGORY_LABELS: dict[str, str] = {
    "ShipmentChange": "Shipment Change",
    "LeadtimeChange": "Lead Time Change",
    LEGACY_LEADTIME_CATEGORY: "Lead Time Change",
    "RandomChange": "Random / Market Change",
    "DifficultyOverride": "Difficulty Override",
    "Unclassified": "Unclassified",
}


def build_category_breakdown(tail: pl.DataFrame) -> list[dict[str, Any]]:
    """Count / share / avg $ shift of problem loads by primary movement category."""
    if tail.is_empty() or "primary_category" not in tail.columns:
        return []
    n = tail.height
    stats = (
        tail.with_columns(
            pl.col("primary_category").replace(LEGACY_LEADTIME_CATEGORY, "LeadtimeChange")
        )
        .group_by("primary_category")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
        )
    )
    by_cat = {r["primary_category"]: r for r in stats.iter_rows(named=True)}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in CATEGORY_ORDER:
        if cat not in by_cat:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        out.append(
            {
                "category": cat,
                "label": CATEGORY_LABELS.get(cat, cat),
                "n_loads": k,
                "pct": round(k / n, 4),
                "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
                if row["avg_shift_amt"] is not None
                else None,
                "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
                if row["avg_shift_pct"] is not None
                else None,
            }
        )
        seen.add(cat)
    for cat in sorted(by_cat):
        if cat in seen:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        out.append(
            {
                "category": cat,
                "label": CATEGORY_LABELS.get(cat, cat),
                "n_loads": k,
                "pct": round(k / n, 4),
                "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
                if row["avg_shift_amt"] is not None
                else None,
                "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
                if row["avg_shift_pct"] is not None
                else None,
            }
        )
    return out


def _catalog_signals(row: dict[str, Any]) -> dict[str, Any] | None:
    """Per-load Davis deltas and flags for explorer category panel."""
    signals: dict[str, Any] = {}
    for col in _CATALOG_SIGNAL_COLS:
        val = row.get(col)
        if val is not None:
            signals[col] = round(float(val), 2)
    for col in _CATALOG_STRING_COLS:
        val = row.get(col)
        if val is not None:
            signals[col] = str(val)
    flags: dict[str, int] = {}
    for col in _CATALOG_FLAG_COLS:
        val = row.get(col)
        if val is not None:
            flags[col] = int(val)
    if flags:
        signals["flags"] = flags
    index_rel: dict[str, float] = {}
    index_fired: dict[str, bool] = {}
    from dqt.etp_slider.movement.orders import INDEX_REL_DELTA_MIN

    for delta_col, avail_col in _INDEX_BASELINE_COLS:
        delta = row.get(delta_col)
        baseline = row.get(avail_col)
        if delta is None or baseline is None:
            continue
        base_abs = abs(float(baseline))
        if base_abs < 1e-6:
            continue
        rel = abs(float(delta)) / base_abs
        index_rel[delta_col] = round(rel, 4)
        index_fired[delta_col] = rel >= INDEX_REL_DELTA_MIN
    if index_rel:
        signals["index_rel_pct"] = index_rel
        signals["index_fired"] = index_fired
        signals["index_gate_pct"] = INDEX_REL_DELTA_MIN
    return signals or None


def _fmt_hours_delta(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}{abs(v):.0f}h"


def _fmt_dollar_delta(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):,.0f}"


_INDEX_SUMMARY_COLS: tuple[tuple[str, str], ...] = (
    ("DAT", "dat_rate_delta"),
    ("fuel", "fuel_cost_delta"),
    ("lag7 CPM", "lag7_cpm_delta"),
)


def _catalog_feature_change(row: dict[str, Any], category: str) -> str | None:
    """Compact primary feature delta for explorer picker labels."""
    category = normalize_movement_category(category)
    if category == "ShipmentChange":
        if row.get("equip_change_ind") == 1:
            a, b = row.get("equipment_type_avail"), row.get("equipment_type_48hr")
            if a and b:
                return f"equip {a}→{b}"
        v = row.get("total_charges_delta")
        if v is not None and float(v) > 50:
            return f"charges {_fmt_dollar_delta(float(v))}"
        if row.get("clhp_change_ind") == 1:
            for label, col in (
                ("CLHP tot", "clhp_pred_tot_cost_delta"),
                ("CLHP line", "clhp_pred_line_delta"),
            ):
                cv = row.get(col)
                if cv is not None and abs(float(cv)) > 0:
                    return f"{label} {_fmt_dollar_delta(float(cv))}"
            return "CLHP changed"
        if row.get("equip_change_ind") == 1:
            return "equip changed"
        return "shipment changed"
    if category == "DifficultyOverride":
        v = row.get("hard_ft_delta")
        if v is None:
            return "hard_ft increased"
        iv = int(round(float(v)))
        return f"hard_ft {iv:+d}"
    if category == "LeadtimeChange":
        candidates: list[tuple[str, float]] = []
        for name, col in (("book_2_pkup", "book_2_pkup_delta"), ("avail_2_book", "avail_2_book_delta")):
            v = row.get(col)
            if v is not None:
                candidates.append((name, float(v)))
        if not candidates:
            return "clocks moved"
        name, val = max(candidates, key=lambda x: abs(x[1]))
        return f"{name} {_fmt_hours_delta(val)}"
    if category == "RandomChange":
        if row.get("index_change_ind") == 1:
            movers: list[tuple[str, float]] = []
            for label, col in _INDEX_SUMMARY_COLS:
                v = row.get(col)
                if v is not None:
                    movers.append((label, float(v)))
            if movers:
                label, val = max(movers, key=lambda x: abs(x[1]))
                return f"{label} {_fmt_dollar_delta(val)}"
        return "catch-all"
    return None


ANNOTATION_NUMERIC_COLS: tuple[str, ...] = (
    "total_charges",
    "hard_ft_cnt",
    "clhp_pred_tot_cost",
    "clhp_pred_line",
    "dat_rate",
    "fuel_cost",
    "lag7_cpm_x_miles",
)
ANNOTATION_CATEGORICAL_COLS: tuple[str, ...] = ("load_type", "path_taken", "override_flag")
CHARGE_ANNOTATION_EPS = 50.0
CLHP_ANNOTATION_EPS = 50.0
HARD_FT_ANNOTATION_EPS = 1.0
_INDEX_ANNOTATION_LABELS: dict[str, str] = {
    "dat_rate": "DAT",
    "fuel_cost": "fuel",
    "lag7_cpm_x_miles": "lag7 CPM",
}


def resolve_feature_snapshots_path(cache_dir: Path) -> Path | None:
    """Feature snapshot parquet (flat cache or etp_lake mart partitions)."""
    cache_dir = Path(cache_dir)
    flat = cache_dir / "etp-feature-history.parquet"
    if flat.is_file():
        return flat
    mart = cache_dir.parent / "etp_lake" / "mart" / "feature_snapshots"
    if mart.is_dir() and any(mart.glob("ship_month=*/*.parquet")):
        return mart
    return None


def load_feature_snapshots(path: Path, loadnumbers: list[int]) -> pl.DataFrame | None:
    """Load model feature snapshots for embedded explorer loads only."""
    if not loadnumbers or path is None:
        return None
    path = Path(path)
    base_cols = ("loadnumber", "hours_before_pickup", "hours_since_available", "snapshot_utc")
    want = list(base_cols) + list(ANNOTATION_NUMERIC_COLS) + list(ANNOTATION_CATEGORICAL_COLS)
    try:
        if path.is_dir():
            lf = pl.scan_parquet(str(path / "ship_month=*" / "*.parquet"))
        else:
            lf = pl.scan_parquet(path)
        schema = lf.collect_schema().names()
        pick = [c for c in want if c in schema]
        if "loadnumber" not in pick or "hours_before_pickup" not in pick:
            return None
        return (
            lf.filter(pl.col("loadnumber").is_in(loadnumbers))
            .select(pick)
            .sort(["loadnumber", "hours_before_pickup", "hours_since_available"])
            .collect()
        )
    except (OSError, ValueError, KeyError, pl.exceptions.ComputeError):
        return None


def _checkpoint_feature_values(
    feat: pl.DataFrame, loadnumber: int
) -> dict[int, dict[str, Any]]:
    """Feature levels at each explorer mark for one load."""
    sub = feat.filter(pl.col("loadnumber") == loadnumber)
    if sub.is_empty():
        return {}
    out: dict[int, dict[str, Any]] = {}
    cols = [
        c
        for c in (*ANNOTATION_NUMERIC_COLS, *ANNOTATION_CATEGORICAL_COLS)
        if c in sub.columns
    ]
    for mark in MARK_HRS:
        vals: dict[str, Any] = {}
        for col in cols:
            snap = _at_mark(sub, col, mark)
            row = snap.filter(pl.col("loadnumber") == loadnumber)
            if row.is_empty():
                continue
            v = row[col][0]
            if v is not None:
                vals[col] = v
        if vals:
            out[mark] = vals
    return out


def _events_between_checkpoints(
    v0: dict[str, Any], v1: dict[str, Any], *, mark_hrs: int
) -> list[dict[str, Any]]:
    """Material movement-category feature changes observed entering ``mark_hrs``."""
    events: list[dict[str, Any]] = []
    mark_label = MARK_LABELS.get(mark_hrs, str(mark_hrs))

    if "total_charges" in v0 and "total_charges" in v1:
        delta = float(v1["total_charges"]) - float(v0["total_charges"])
        if delta > CHARGE_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "label": mark_label,
                    "text": f"charges +${delta:,.0f}",
                    "category": "ShipmentChange",
                }
            )

    if "clhp_pred_tot_cost" in v0 and "clhp_pred_tot_cost" in v1:
        delta = float(v1["clhp_pred_tot_cost"]) - float(v0["clhp_pred_tot_cost"])
        if abs(delta) >= CLHP_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "label": mark_label,
                    "text": f"happy path {_fmt_dollar_delta(delta)}",
                    "category": "ShipmentChange",
                }
            )
    elif "clhp_pred_line" in v0 and "clhp_pred_line" in v1:
        delta = float(v1["clhp_pred_line"]) - float(v0["clhp_pred_line"])
        if abs(delta) >= CLHP_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "label": mark_label,
                    "text": f"happy path line {_fmt_dollar_delta(delta)}",
                    "category": "ShipmentChange",
                }
            )

    if "hard_ft_cnt" in v0 and "hard_ft_cnt" in v1:
        delta = float(v1["hard_ft_cnt"]) - float(v0["hard_ft_cnt"])
        if abs(delta) >= HARD_FT_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "label": mark_label,
                    "text": f"hard_ft {delta:+.0f} (difficulty)",
                    "category": "DifficultyOverride",
                }
            )

    if (
        "load_type" in v0
        and "load_type" in v1
        and v0["load_type"] != v1["load_type"]
        and v1["load_type"] is not None
    ):
        events.append(
            {
                "mark_hrs": mark_hrs,
                "label": mark_label,
                "text": f"equip {v0['load_type']}→{v1['load_type']}",
                "category": "ShipmentChange",
            }
        )

    for col in _INDEX_ANNOTATION_LABELS:
        if col in v0 and col in v1:
            pass  # index markers use avail-baseline logic in build_load_feature_annotations

    if (
        "path_taken" in v0
        and "path_taken" in v1
        and v0["path_taken"] != v1["path_taken"]
        and v1["path_taken"] is not None
    ):
        events.append(
            {
                "mark_hrs": mark_hrs,
                "label": mark_label,
                "text": f"path → {v1['path_taken']}",
                "category": "RandomChange",
            }
        )

    return events


def _index_events_from_available(
    cps: dict[int, dict[str, Any]],
    marks: list[int],
) -> list[dict[str, Any]]:
    """First checkpoint where each index feature crosses INDEX_REL_DELTA_MIN vs Available."""
    from dqt.etp_slider.movement.orders import INDEX_REL_DELTA_MIN

    if 999 not in cps:
        return []
    v_avail = cps[999]
    events: list[dict[str, Any]] = []
    fired: set[str] = set()
    for mark in marks:
        if mark == 999 or mark not in cps:
            continue
        v_mark = cps[mark]
        mark_label = MARK_LABELS.get(mark, str(mark))
        for col, name in _INDEX_ANNOTATION_LABELS.items():
            if col in fired or col not in v_avail or col not in v_mark:
                continue
            base = abs(float(v_avail[col]))
            if base < 1e-6:
                continue
            v0f = float(v_avail[col])
            v1f = float(v_mark[col])
            rel = abs(v1f - v0f) / base
            if rel >= INDEX_REL_DELTA_MIN:
                sign = "+" if v1f >= v0f else "−"
                events.append(
                    {
                        "mark_hrs": mark,
                        "label": mark_label,
                        "text": f"{name} {sign}{rel * 100:.0f}% vs avail",
                        "category": "RandomChange",
                    }
                )
                fired.add(col)
    return events


def build_load_feature_annotations(
    feat: pl.DataFrame,
    loadnumbers: list[int],
) -> dict[str, list[dict[str, Any]]]:
    """Per-load checkpoint events for explorer chart markers."""
    out: dict[str, list[dict[str, Any]]] = {}
    marks = list(MARK_HRS)
    for ln in loadnumbers:
        cps = _checkpoint_feature_values(feat, int(ln))
        if not cps:
            continue
        events: list[dict[str, Any]] = []
        for m0, m1 in pairwise(marks):
            if m0 not in cps or m1 not in cps:
                continue
            events.extend(_events_between_checkpoints(cps[m0], cps[m1], mark_hrs=m1))
        events.extend(_index_events_from_available(cps, marks))
        if events:
            out[str(ln)] = events
    return out


def _hc_gallery_rows(tail: pl.DataFrame, *, n: int = HC_GALLERY_SIZE_DEFAULT) -> pl.DataFrame:
    """Top high-confidence LC loads by ETP50 % shift (avail→48hr)."""
    if tail.is_empty() or "leadtime_isolated_ind" not in tail.columns:
        return pl.DataFrame()
    return (
        tail.filter(pl.col("leadtime_isolated_ind") == 1)
        .sort("etp50_shift_pct", descending=True)
        .head(n)
    )


def build_load_explorer(
    indexed: dict[str, pl.DataFrame],
    tail: pl.DataFrame,
    *,
    max_loads: int = MAX_EXPLORER_LOADS_DEFAULT,
    hc_gallery_size: int = HC_GALLERY_SIZE_DEFAULT,
) -> dict[str, Any]:
    """Indexed checkpoint series for problem loads (embedded in HTML report)."""
    if tail.is_empty():
        return {"catalog": [], "series": {}, "n_total": 0, "n_embedded": 0, "hc_gallery": []}

    hc_gallery_df = _hc_gallery_rows(tail, n=hc_gallery_size)
    embed_ids: set[int] = set(
        tail.sort("etp50_shift_pct", descending=True).head(max_loads)["loadnumber"].to_list()
    )
    if not hc_gallery_df.is_empty():
        embed_ids.update(int(x) for x in hc_gallery_df["loadnumber"].to_list())
    tail_sorted = tail.filter(pl.col("loadnumber").is_in(list(embed_ids))).sort(
        "etp50_shift_pct", descending=True
    )
    catalog: list[dict[str, Any]] = []
    series: dict[str, dict[str, Any]] = {}
    from dqt.etp_slider.problem_loads.leadtime import LEADTIME_ARCHETYPE_LABELS

    for row in tail_sorted.iter_rows(named=True):
        ln = int(row["loadnumber"])
        pct = float(row["etp50_shift_pct"])
        cat = normalize_movement_category(row.get("primary_category"))
        amt = row.get("etp50_shift_amt")
        avail = row.get("etp50_avail")
        hr48 = row.get("etp50_48hr")
        feature_change = _catalog_feature_change(row, cat)
        arch = row.get("path_archetype")
        arch_label = LEADTIME_ARCHETYPE_LABELS.get(arch, arch) if arch else None
        label = f"{ln} (+{pct * 100:.1f}%)"
        if arch_label and arch != "unknown":
            label += f" · {arch_label}"
        if feature_change:
            label += f" · {feature_change}"
        entry: dict[str, Any] = {
            "loadnumber": ln,
            "shift_pct": round(pct, 4),
            "shift_amt": round(float(amt), 2) if amt is not None else None,
            "etp50_avail": round(float(avail), 2) if avail is not None else None,
            "etp50_48hr": round(float(hr48), 2) if hr48 is not None else None,
            "category": cat,
            "category_label": CATEGORY_LABELS.get(cat, cat),
            "feature_change": feature_change,
            "label": label,
        }
        if arch:
            entry["path_archetype"] = arch
            if arch_label:
                entry["path_archetype_label"] = arch_label
        isolated = int(row.get("leadtime_isolated_ind") or 0)
        index_overlap = int(row.get("index_clock_overlap_ind") or 0)
        index_flag = int(row.get("index_change_ind") or 0)
        if cat == "LeadtimeChange":
            if isolated:
                entry["leadtime_confidence"] = "isolated"
                entry["leadtime_confidence_label"] = "High confidence (clock-only)"
                label += " · clock-only"
                entry["label"] = label
            elif index_overlap or index_flag:
                entry["leadtime_confidence"] = "index_co_occur"
                entry["leadtime_confidence_label"] = "Index co-occur"
        sig = _catalog_signals(row)
        if sig:
            entry["signals"] = sig
        catalog.append(entry)

    for ln in tail_sorted["loadnumber"].to_list():
        key = str(ln)
        series[key] = {}
        for metric in EXPLORER_METRICS:
            sub = _sort_by_mark_hrs(
                indexed[metric].filter(pl.col("loadnumber") == ln)
            )
            if sub.is_empty():
                continue
            marks = sub["mark_hrs"].to_list()
            series[key][metric] = {
                "marks": marks,
                "labels": [MARK_LABELS.get(int(h), str(h)) for h in marks],
                "idx": [round(float(x), 2) for x in sub["idx"].to_list()],
                "val": [
                    round(float(x), 2) if x is not None else None
                    for x in sub["val"].to_list()
                ],
            }

    hc_gallery: list[dict[str, Any]] = []
    catalog_ids = {int(r["loadnumber"]) for r in catalog}
    for rank, row in enumerate(hc_gallery_df.iter_rows(named=True), start=1):
        ln = int(row["loadnumber"])
        pct = float(row["etp50_shift_pct"])
        hc_gallery.append(
            {
                "rank": rank,
                "loadnumber": ln,
                "shift_pct": round(pct, 4),
                "shift_amt": round(float(row.get("etp50_shift_amt") or 0), 2),
                "embedded": ln in catalog_ids,
                "label": f"#{rank} {ln} (+{pct * 100:.1f}%)",
            }
        )

    return {
        "catalog": catalog,
        "series": series,
        "n_total": tail.height,
        "n_embedded": len(catalog),
        "hc_gallery": hc_gallery,
        "hc_gallery_size": hc_gallery_size,
    }


def resolve_davis_cache(
    cache_dir: Path,
    *,
    avail_start: date | None = None,
    avail_end: date | None = None,
) -> Path | None:
    """Pick analysis-davis raw parquet for movement-category joins."""
    cache_dir = Path(cache_dir)
    if avail_start is not None and avail_end is not None:
        exact = cache_dir / f"analysis-davis-raw-{avail_start}_{avail_end}.parquet"
        if exact.is_file():
            return exact
    raw_files = [p for p in cache_dir.glob("analysis-davis-raw-*.parquet") if p.is_file()]
    if raw_files:
        return max(raw_files, key=lambda p: p.stat().st_size)
    return None


def _tail_filters_active(
    tail_pct: float | None,
    tail_amt: float | None,
) -> bool:
    return (tail_pct is not None and tail_pct > 0) or (tail_amt is not None and tail_amt > 0)


def _prepare_tail_cohort(
    per_load: pl.DataFrame,
    cohort_ids: pl.DataFrame,
    *,
    tail_pct: float | None,
    tail_amt: float | None = None,
    tail_mode: str | None = None,
    davis_path: Path | None = None,
    feature_snapshots_path: Path | None = None,
) -> pl.DataFrame:
    """Problem loads within the report cohort (ETP50 pct / $ shift thresholds)."""
    from dqt.etp_slider.problem_loads.tail import (
        TAIL_AMT_DEFAULT,
        TAIL_PCT_DEFAULT,
        TAIL_THRESHOLD_MODE_DEFAULT,
        TailThresholdMode,
        assign_movement_category,
        enrich_davis_flags,
        flag_tail_loads,
    )

    mode: TailThresholdMode = (tail_mode or TAIL_THRESHOLD_MODE_DEFAULT)  # type: ignore[assignment]
    if tail_pct is None and tail_amt is None:
        tail_pct = TAIL_PCT_DEFAULT
        tail_amt = TAIL_AMT_DEFAULT

    cohort = per_load.join(cohort_ids, on="loadnumber", how="inner")
    flagged = flag_tail_loads(
        cohort,
        pct_threshold=tail_pct if tail_pct and tail_pct > 0 else None,
        amt_threshold=tail_amt if tail_amt and tail_amt > 0 else None,
        mode=mode,
    )
    tail = flagged.filter(pl.col("is_tail"))
    if tail.is_empty():
        return tail

    if davis_path is not None and davis_path.exists():
        davis = enrich_davis_flags(pl.read_parquet(davis_path))
        flag_cols = [
            c
            for c in (
                "total_charges_delta",
                "hard_ft_delta",
                "book_2_pkup_delta",
                "avail_2_book_delta",
                "dat_rate_delta",
                "fuel_cost_delta",
                "lag7_cpm_delta",
                "clhp_pred_tot_cost_delta",
                "clhp_pred_line_delta",
                "dat_rate_avail",
                "fuel_cost_avail",
                "lag7_cpm_avail",
                "clhp_pred_tot_cost_avail",
                "clhp_pred_line_avail",
                "equipment_type_avail",
                "equipment_type_48hr",
                "is_hyperlocal",
                "charge_inc_ind",
                "equip_change_ind",
                "clhp_change_ind",
                "hard_ft_inc_ind",
                "clocks_moved_ind",
                "clock_only_ind",
                "index_change_ind",
                "leadtime_isolated_ind",
                "index_clock_overlap_ind",
            )
            if c in davis.columns
        ]
        tail = tail.join(
            davis.select("loadnumber", *flag_cols),
            on="loadnumber",
            how="left",
        )
        for col in (
            "charge_inc_ind",
            "equip_change_ind",
            "clhp_change_ind",
            "hard_ft_inc_ind",
            "clocks_moved_ind",
            "clock_only_ind",
            "index_change_ind",
            "leadtime_isolated_ind",
            "index_clock_overlap_ind",
        ):
            if col in tail.columns:
                tail = tail.with_columns(pl.col(col).fill_null(0).cast(pl.Int8))

    if feature_snapshots_path is not None:
        from dqt.etp_slider.movement.feature_path import apply_feature_path_upgrades

        feat = load_feature_snapshots(
            feature_snapshots_path, tail["loadnumber"].to_list()
        )
        tail = apply_feature_path_upgrades(tail, feat)

    return assign_movement_category(tail)


def build_report_payload(
    hist: pl.DataFrame,
    per_load: pl.DataFrame,
    cohort_ids: pl.DataFrame,
    *,
    lead_min_days: float,
    lead_max_days: float,
    avail_start: date | None,
    avail_end: date | None,
    tail: pl.DataFrame | None = None,
    tail_pct: float | None = None,
    tail_amt: float | None = None,
    tail_mode: str | None = None,
    max_explorer_loads: int = MAX_EXPLORER_LOADS_DEFAULT,
    debug: bool = False,
    feature_snapshots: pl.DataFrame | None = None,
    mde_timeline_cache: Path | str | None = None,
    mde_timeline_force: bool = False,
    hist_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    indexed = _indexed_at_marks(hist, cohort_ids)
    curve = build_curve(indexed)
    summary = build_summary(indexed)
    meta = build_cohort_meta(
        per_load,
        cohort_ids,
        curve,
        lead_min_days=lead_min_days,
        lead_max_days=lead_max_days,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    _enrich_summary_meta(meta, summary)
    if debug:
        meta["debug"] = True
    payload: dict[str, Any] = {"summary": summary, "curve": curve, "meta": meta}
    if tail is not None and not tail.is_empty():
        from dqt.etp_slider.problem_loads.leadtime import (
            LEADTIME_ARCHETYPE_LABELS,
            LEADTIME_ARCHETYPE_ORDER,
            enrich_path_archetypes_from_indexed,
        )

        tail = enrich_path_archetypes_from_indexed(tail, indexed)
        explorer = build_load_explorer(indexed, tail, max_loads=max_explorer_loads)
        explorer["archetype_labels"] = LEADTIME_ARCHETYPE_LABELS
        explorer["archetype_order"] = list(LEADTIME_ARCHETYPE_ORDER)
        embedded_ids = [int(r["loadnumber"]) for r in explorer["catalog"]]
        if feature_snapshots is not None and not feature_snapshots.is_empty():
            ann = build_load_feature_annotations(feature_snapshots, embedded_ids)
            if ann:
                explorer["annotations"] = ann
                meta["explorer_annotations"] = True
        if embedded_ids and mde_timeline_cache is not None:
            from dqt.etp_slider.drift.mde_timeline import (
                build_mde_explorer_overlay,
                load_mde_timeline_events,
            )

            cache_path = Path(mde_timeline_cache)
            should_load = cache_path.is_file() or mde_timeline_force
            if should_load:
                try:
                    mde_events = load_mde_timeline_events(
                        embedded_ids,
                        cache_path=cache_path,
                        force=mde_timeline_force,
                    )
                except (OSError, ValueError, KeyError, pl.exceptions.ComputeError):
                    mde_events = pl.DataFrame()
                if not mde_events.is_empty():
                    marks_by_load = {
                        k: v.get("ETP50", {}).get("marks", list(MARK_HRS))
                        for k, v in explorer.get("series", {}).items()
                    }
                    timing = per_load.filter(pl.col("loadnumber").is_in(embedded_ids)).select(
                        "loadnumber", "pickup_appt_latest_utc"
                    )
                    mde_ann, mde_meta = build_mde_explorer_overlay(
                        mde_events,
                        timing,
                        embedded_ids,
                        marks_by_load=marks_by_load,
                    )
                    if mde_ann:
                        explorer["mde_annotations"] = mde_ann
                        meta["explorer_mde"] = True
                        for entry in explorer["catalog"]:
                            key = str(entry["loadnumber"])
                            if key in mde_meta:
                                entry["mde"] = mde_meta[key]
        payload["explorer"] = explorer
        meta["n_tail_loads"] = explorer["n_total"]
        meta["n_tail_embedded"] = explorer["n_embedded"]
        meta["tail_pct_threshold"] = tail_pct
        meta["tail_amt_threshold"] = tail_amt
        meta["tail_threshold_mode"] = tail_mode
        breakdown_primary = build_category_breakdown(tail)
        if breakdown_primary:
            payload["category_breakdown_primary"] = breakdown_primary
        from dqt.etp_slider.problem_loads.cat_report import (
            ATTRIBUTION_COLORS,
            ATTRIBUTION_LABELS,
            build_attribution_problem_breakdown,
        )
        from dqt.etp_slider.sql import HISTORY_CACHE, SURVIVAL_PER_LOAD_CACHE

        cache_dir = Path(hist_path).parent if hist_path else None
        book_cache: Path | None = None
        survival_path: Path | None = None
        resolved_hist: Path | None = Path(hist_path) if hist_path else None
        if cache_dir is not None:
            survival_path = cache_dir / SURVIVAL_PER_LOAD_CACHE
            if resolved_hist is None:
                resolved_hist = cache_dir / HISTORY_CACHE
            if avail_start is not None and avail_end is not None:
                book_cache = cache_dir / f"leadtime-book-quotes-{avail_start}_{avail_end}.parquet"

        breakdown = build_attribution_problem_breakdown(
            tail,
            data_dir=data_dir,
            hist_path=resolved_hist,
            survival_path=survival_path,
            book_quotes_cache=book_cache,
        )
        if breakdown:
            payload["category_breakdown"] = breakdown
            meta["attribution_colors"] = ATTRIBUTION_COLORS
            meta["attribution_labels"] = ATTRIBUTION_LABELS
            by_cat = {r["category"]: r for r in breakdown}
            hc = by_cat.get("LeadTimeClockOnly")
            idx = by_cat.get("LeadTimeIndexCooccur")
            if hc:
                meta["leadtime_hc_n"] = hc["n_loads"]
                meta["leadtime_hc_avg_amt"] = hc.get("avg_shift_amt")
                meta["leadtime_hc_avg_pct"] = hc.get("avg_shift_pct")
            if idx:
                meta["leadtime_index_n"] = idx["n_loads"]
                meta["leadtime_index_avg_amt"] = idx.get("avg_shift_amt")
                meta["leadtime_index_avg_pct"] = idx.get("avg_shift_pct")
        elif breakdown_primary:
            payload["category_breakdown"] = breakdown_primary
        from dqt.etp_slider.problem_loads.leadtime import (
            build_leadtime_archetype_breakdown,
            build_leadtime_confidence_breakdown,
        )

        lt_arch = build_leadtime_archetype_breakdown(
            tail,
            data_dir=data_dir,
            hist_path=resolved_hist,
            survival_path=survival_path,
            book_quotes_cache=book_cache,
        )
        if lt_arch:
            payload["leadtime_archetype_breakdown"] = lt_arch
        lt_conf = build_leadtime_confidence_breakdown(tail)
        if lt_conf:
            payload["leadtime_confidence_breakdown"] = lt_conf
        if debug:
            from dqt.etp_slider.problem_loads.tail import build_movement_qa

            qa = build_movement_qa(tail)
            if qa:
                payload["movement_qa"] = qa
        from dqt.etp_slider.movement.docs import build_movement_category_docs

        payload["movement_categories"] = build_movement_category_docs()
    return payload


def _format_date_span(start: date, end: date) -> str:
    """Human-readable inclusive date span."""
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
    if start.year == end.year:
        return (
            f"{start.strftime('%b')} {start.day} – "
            f"{end.strftime('%b')} {end.day}, {end.year}"
        )
    return (
        f"{start.strftime('%b')} {start.day}, {start.year} – "
        f"{end.strftime('%b')} {end.day}, {end.year}"
    )


def apply_report_variant_meta(meta: dict[str, Any], variant: str | None) -> None:
    if not variant:
        return
    cfg = REPORT_VARIANTS.get(variant)
    if cfg is None:
        raise ValueError(f"unknown report variant {variant!r} — choose from {sorted(REPORT_VARIANTS)}")
    meta["report_variant"] = variant
    meta["report_label"] = cfg["label"]


def _cohort_dates(meta: dict[str, Any]) -> tuple[str, str | None]:
    """Primary available-window label and optional sublabel (actual posts in cohort)."""
    if meta.get("avail_start") and meta.get("avail_end"):
        a0 = date.fromisoformat(meta["avail_start"])
        a1 = date.fromisoformat(meta["avail_end"])
        primary = _format_date_span(a0, a1)
        sub: str | None = None
        if meta.get("cohort_avail_min") and meta.get("cohort_avail_max"):
            c0 = date.fromisoformat(meta["cohort_avail_min"][:10])
            c1 = date.fromisoformat(meta["cohort_avail_max"][:10])
            if c0 != a0 or c1 != a1:
                sub = f"Actual posts in cohort: {_format_date_span(c0, c1)}"
        return primary, sub
    if meta.get("cohort_avail_min") and meta.get("cohort_avail_max"):
        c0 = date.fromisoformat(meta["cohort_avail_min"][:10])
        c1 = date.fromisoformat(meta["cohort_avail_max"][:10])
        return _format_date_span(c0, c1), "All made-available dates in mart (no date filter)"
    return (
        f"{meta['mart_ship_start']} → {meta['mart_ship_end']}",
        "Mart ship_date_coalesce window",
    )


def _eyebrow(meta: dict[str, Any]) -> str:
    mode = meta.get("cohort_mode", "mart")
    label = "Survival cohort" if mode == "survival" else "Mart cohort"
    variant = meta.get("report_label")
    if variant:
        return f"ETP Coverage Analytics · {label} · {variant} · Generated {meta['generated_utc']}"
    return f"ETP Coverage Analytics · {label} · Generated {meta['generated_utc']}"


def _ship_window_label(meta: dict[str, Any]) -> str | None:
    """Human-readable ship-date pull span for report headers."""
    start = meta.get("pull_ship_start") or meta.get("mart_ship_start")
    end = meta.get("pull_ship_end") or meta.get("mart_ship_end")
    if not start or not end:
        return None
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = date.fromisoformat(str(end)[:10])
        return _format_date_span(d0, d1)
    except ValueError:
        return f"{start} → {end}"


def _cohort_strip_html(meta: dict[str, Any]) -> str:
    dates, dates_sub = _cohort_dates(meta)
    lead = f"{meta['lead_min_days']:.0f}–{meta['lead_max_days']:.0f} days"
    avail_label = (
        "Final Available window"
        if meta.get("cohort_mode") == "survival"
        else "Available window"
    )
    sub_html = f'<div class="chip-sub">{dates_sub}</div>' if dates_sub else ""
    ship_label = _ship_window_label(meta)
    ship_chip = ""
    if ship_label:
        ship_lbl = (
            "Ship-date pull (SF)"
            if meta.get("cohort_mode") == "survival"
            else "Mart ship dates"
        )
        ship_chip = f"""<div class="cohort-chip">
    <div class="chip-label">{ship_lbl}</div>
    <div class="chip-value">{ship_label}</div>
  </div>"""
    variant_chip = ""
    if meta.get("report_label"):
        variant_chip = f"""<div class="cohort-chip">
    <div class="chip-label">Report variant</div>
    <div class="chip-value">{meta['report_label']}</div>
  </div>"""
    return f"""<div class="cohort-strip">
  {variant_chip}
  <div class="cohort-chip">
    <div class="chip-label">{avail_label}</div>
    <div class="chip-value">{dates}</div>
    {sub_html}
  </div>
  {ship_chip}
  <div class="cohort-chip cohort-chip-accent">
    <div class="chip-label">Report cohort</div>
    <div class="chip-value">{meta["n_loads"]:,} loads</div>
  </div>
  <div class="cohort-chip">
    <div class="chip-label">Lead time (avail → pickup)</div>
    <div class="chip-value">{lead}</div>
  </div>
</div>"""


def _methodology(meta: dict[str, Any]) -> str:
    if meta.get("cohort_mode") == "survival":
        avail_filter = ""
        if meta.get("avail_start") and meta.get("avail_end"):
            avail_filter = (
                f"available_on_last between {meta['avail_start']} and {meta['avail_end']}. "
            )
        checkpoints = "/".join(str(h) for h in meta["checkpoints_hrs"])
        ship_lo = meta.get("pull_ship_start") or meta["mart_ship_start"]
        ship_hi = meta.get("pull_ship_end") or meta["mart_ship_end"]
        return (
            f"<strong>Population</strong> ({meta['n_loads']:,} loads): Covered TL loads, "
            f"ship_date_coalesce {ship_lo} → {ship_hi}, "
            f"lead time (available_on_last → pickup appt latest) "
            f"{meta['lead_min_hrs']}–{meta['lead_max_hrs']} hours, "
            f"unbooked ≥24h after final Available. {avail_filter}"
            f"<strong>Available baseline (100)</strong>: first ETP snapshot at/after "
            f"available_on_last (before the 168h mark). "
            f"<strong>Checkpoints</strong> ({checkpoints}h to pickup): last snapshot at/before "
            f"each mark. Loads drop from a mark onward once booked. "
            f"<strong>Curve</strong>: mean/median indexed ETP among loads still unbooked at "
            f"each checkpoint; P25/P75 band is ETP50 only. "
            f"<strong>Window table</strong>: consecutive checkpoint shifts among loads with "
            f"both endpoints while still on the board. "
            f"<strong>Caveat</strong>: associative lead-time patterns, not causal attribution."
        )
    avail_filter = ""
    if meta.get("avail_start") and meta.get("avail_end"):
        avail_filter = (
            f"Report filter: made_available_utc between {meta['avail_start']} and "
            f"{meta['avail_end']}. "
        )
    checkpoints = "/".join(str(h) for h in meta["checkpoints_hrs"])
    return (
        f"<strong>Mart population</strong> ({meta['mart_n_loads']:,} loads): Covered TL loads with "
        f"ship_date_coalesce {meta['mart_ship_start']} → {meta['mart_ship_end']}, "
        f"booking window &gt; {MART_MIN_BOOKING_HRS}h, and stable pickup appointment "
        f"(zero model-log appt-date mismatches from available through 48h before pickup). "
        f"Made-available timestamp is <code>available_on_first_cst</code> (first posting), not "
        f"available_on_last. "
        f"<strong>Report cohort</strong> ({meta['n_loads']:,} loads): {avail_filter}"
        f"booking_window_hrs {meta['lead_min_hrs']}–{meta['lead_max_hrs']} "
        f"({meta['lead_min_days']:.0f}–{meta['lead_max_days']:.0f} days avail → pickup). "
        f"<strong>Telemetry</strong>: model + audit snapshots from made_available through "
        f"{HISTORY_END_HRS_BEFORE_PICKUP}h before pickup only (per "
        f"<code>etp-slider-history.sql</code>). "
        f"<strong>Index baseline (100)</strong>: first snapshot at/after made_available — "
        f"ETP10/50 from ETP_MODEL_LOGS, Target 1/3 from ETP_AUDIT. "
        f"<strong>Checkpoints</strong> ({checkpoints}h to pickup): for each load, the snapshot "
        f"with hours_before_pickup ≥ mark closest to that mark (minimum hbp among qualifying snaps). "
        f"<strong>Curve</strong>: cross-load mean/median of indexed values at each checkpoint among "
        f"loads with a reading; P25/P75 band is ETP50 only. "
        f"<strong>Window table</strong>: per-load (value_end − value_start) / value_start between "
        f"consecutive checkpoints, then averaged; loads must have both endpoints. "
        f"<strong>Attrition chart</strong>: loads with ETP50 at each mark ({meta['n_at_availability']:,} "
        f"at Available → {meta['n_at_24h_out']:,} at 1 day out, {meta['pct_at_24h_out']:.0f}% of "
        f"Available cohort). This reflects telemetry coverage, not live unbooked survival — "
        f"the mart is Covered freight with history through 24h out. "
        f"<strong>Caveat</strong>: associative lead-time patterns, not causal attribution."
    )


def _note_drift(meta: dict[str, Any]) -> str:
    if meta.get("cohort_mode") == "survival":
        return (
            "Each load indexed to 100 at its first ETP reading after final Available "
            "(before the 7-day mark). Lines = average indexed value across loads still "
            "unbooked at each checkpoint; shaded band = ETP50 P25–P75. "
            "n shrinks at each mark as loads book."
        )
    return (
        "Each load indexed to 100 at its first reading after made_available (first posting). "
        "Solid lines = cross-load mean indexed value among loads with telemetry at that checkpoint; "
        "dashed = median. Shaded band = ETP50 P25–P75. "
        f"Telemetry ends {HISTORY_END_HRS_BEFORE_PICKUP}h before pickup in the mart."
    )


def _note_attrition(meta: dict[str, Any]) -> str:
    if meta.get("cohort_mode") == "survival":
        return (
            f"Loads from the {meta['n_loads']:,}-load cohort still unbooked (and appt-stable) "
            f"at each mark ({meta['n_at_availability']:,} at Available → "
            f"{meta['n_at_24h_out']:,} at 1 day out). "
            "The drift curve above averages over these shrinking populations."
        )
    return (
        f"{meta['n_loads']:,} loads in the report cohort. Bars show how many have model ETP50 "
        f"readings at each checkpoint ({meta['n_at_availability']:,} at Available, "
        f"{meta['n_at_7d_out']:,} at 7 days out, {meta['n_at_24h_out']:,} at 1 day out). "
        "This is telemetry coverage, not live unbooked survival — the mart is Covered loads "
        "with appt-stable model history."
    )


def _note_avmed(meta: dict[str, Any]) -> str:
    n_zero = meta.get("etp50_med_zero_windows", 0)
    n = meta.get("etp50_windows", 0)
    base = (
        "Grouped bars: mean vs median ETP50 $ shift between consecutive checkpoint windows. "
        "The gap is skew — a few movers lifting the average while most loads stay flat."
    )
    if n and n_zero == n:
        return (
            f"{base} Median is $0 in all {n} windows (≥50% of loads unchanged each window); "
            "light bars sit on the $0 baseline — they are not missing."
        )
    if n_zero:
        return (
            f"{base} Median is $0 in {n_zero}/{n} windows; light bars on the baseline "
            f"mean most loads did not move (max median ${meta.get('etp50_med_max_amt', 0):.0f})."
        )
    return base


def _note_table(meta: dict[str, Any]) -> str:
    med0 = meta.get("first_week_etp50_med_pct", 0.0)
    if med0 < 0.5:
        early = (
            f"In the {BUCKET_LABELS[0]} window, the cohort median ETP50 shift is ~{med0:.1f}% — "
            "most loads flat, averages driven by a minority of movers."
        )
    else:
        early = (
            f"In the {BUCKET_LABELS[0]} window, median ETP50 shift is ~{med0:.1f}% — "
            "movement is already visible in the first week."
        )
    return (
        f"{early} Where median catches up to average, movement has become broad-based. "
        "Tab columns are avg/median % and $ shift per consecutive checkpoint window."
    )


def _category_section_html(meta: dict[str, Any]) -> str:
    if meta.get("cohort_mode") == "survival" or meta.get("show_explorer") is False:
        return ""
    if not meta.get("n_tail_loads"):
        return ""
    from dqt.etp_slider.problem_loads.tail import (
        TAIL_THRESHOLD_MODE_DEFAULT,
        format_tail_threshold,
    )

    rule = format_tail_threshold(
        pct_threshold=meta.get("tail_pct_threshold"),
        amt_threshold=meta.get("tail_amt_threshold"),
        mode=meta.get("tail_threshold_mode") or TAIL_THRESHOLD_MODE_DEFAULT,
    )
    n = meta["n_tail_loads"]
    embedded = meta.get("n_tail_embedded", n)
    qa_block = ""
    if meta.get("debug"):
        qa_block = """
  <details id="movementQaDetails" style="margin-top:14px">
    <summary>Classification QA (clocks &amp; catch-all)</summary>
    <div id="movementQa" class="note" style="margin-top:8px"></div>
  </details>"""
    return f"""<section id="categorySection">
  <h2>Problem loads by movement category</h2>
  <div class="note">Problem loads (avail→48hr ETP50): <strong>{rule}</strong> — <strong>{n:,}</strong> loads. Donut splits <strong>Lead Time</strong> into clock-only high confidence vs index co-occur (DAT/fuel/lag7 ≥10% endpoint drift); primary Random stays &lt;1%. Table includes ETP p50 MAE vs realized carrier cost at available and book when pricing data is available. Explorer embeds up to {embedded:,} by shift.</div>
  <div class="category-grid">
    <div class="chartbox donut"><canvas id="categoryDonut"></canvas></div>
    <div id="categoryTable" class="category-table-wrap"></div>
  </div>
  <div id="leadtimeArchetypeWrap" class="leadtime-archetype-wrap" style="display:none">
    <h3>Lead Time Change — path shape</h3>
    <p class="note" style="margin:4px 0 10px">Sub-breakdown of ≥10% LeadtimeChange loads by indexed ETP50 climb from available→48hr (checkpoint deltas on etp50_idx_*). MAE columns match the category table: ETP p50 vs realized carrier cost at available and book.</p>
    <div id="leadtimeArchetypeTable" class="category-table-wrap"></div>
  </div>
  <div id="leadtimeConfidenceBreakdownWrap" class="leadtime-archetype-wrap" style="display:none">
    <h3>Lead Time Change — confidence vs confounders</h3>
    <p class="note" style="margin:4px 0 10px">High-confidence (clock-only, no ≥10% index drift) loads proxy deterministic lead-time repricing. Compared to LC population baseline; lift rows are descriptive not causal.</p>
    <div id="leadtimeConfidenceBreakdownTable" class="category-table-wrap"></div>
  </div>{qa_block}
</section>"""


def _explorer_section_html(meta: dict[str, Any], note: str) -> str:
    """Problem-load explorer markup (omitted for survival / no tail loads)."""
    if meta.get("cohort_mode") == "survival" or meta.get("show_explorer") is False:
        return ""
    if not meta.get("n_tail_loads"):
        return ""
    return f"""<section id="explorerSection">
  <h2>Problem load explorer</h2>
  <div class="note">{note}</div>
  <div class="explorer-bar">
    <div>
      <label for="loadSearch">Search load</label><br>
      <input type="search" id="loadSearch" list="loadList" placeholder="Loadnumber or label…" autocomplete="off">
      <datalist id="loadList"></datalist>
    </div>
    <div>
      <label for="categoryFilter">Category</label><br>
      <select id="categoryFilter"><option value="">All Categories</option></select>
    </div>
    <div>
      <label for="archetypeFilter">Path shape</label><br>
      <select id="archetypeFilter"><option value="">All path shapes</option></select>
    </div>
    <div id="leadtimeConfidenceWrap" style="display:none">
      <label for="leadtimeConfidenceFilter">Lead time confidence</label><br>
      <select id="leadtimeConfidenceFilter">
        <option value="">All lead time loads</option>
        <option value="isolated">High confidence only (no index drift)</option>
        <option value="index_co_occur">With index co-occur (lag7/fuel/DAT)</option>
      </select>
    </div>
    <div>
      <label for="loadSelect">Pick load</label><br>
      <select id="loadSelect" size="1"></select>
    </div>
    <div class="explorer-nav">
      <button type="button" id="loadPrev" title="Previous in filtered list">← Prev</button>
      <span class="rank-label" id="loadRank">—</span>
      <button type="button" id="loadNext" title="Next in filtered list">Next →</button>
      <button type="button" id="hcGalleryBtn" class="preset-btn" title="Filter to high-confidence LC, sorted by ETP % shift">Top HC examples</button>
    </div>
  </div>
  <div class="callout" id="loadMeta"></div>
  <div class="explorer-body">
    <div class="chartbox"><canvas id="loadChart"></canvas></div>
    <div id="loadAnnotations" class="load-annotations"></div>
    <aside id="loadCategoryPanel" class="category-panel" aria-live="polite">
      <div class="cat-panel-head">
        <span class="cat-panel-eyebrow">Classification</span>
        <h3 id="catPanelTitle"></h3>
        <p id="catPanelTagline" class="cat-panel-tagline"></p>
      </div>
      <p id="catPanelSummary" class="cat-panel-summary"></p>
      <p id="catPanelPriorityNote" class="cat-panel-priority"></p>
      <div id="catPanelCriteria" class="cat-panel-block"></div>
      <div id="catPanelLoadSignals" class="cat-panel-block"></div>
      <div id="catPanelMermaid" class="mermaid-wrap"></div>
    </aside>
  </div>
</section>"""


def _note_explorer(meta: dict[str, Any]) -> str:
    n = meta.get("n_tail_loads")
    if not n:
        return "Problem-load explorer disabled for this build."
    from dqt.etp_slider.problem_loads.tail import (
        TAIL_THRESHOLD_MODE_DEFAULT,
        format_tail_threshold,
    )

    rule = format_tail_threshold(
        pct_threshold=meta.get("tail_pct_threshold"),
        amt_threshold=meta.get("tail_amt_threshold"),
        mode=meta.get("tail_threshold_mode") or TAIL_THRESHOLD_MODE_DEFAULT,
    )
    embedded = meta.get("n_tail_embedded", n)
    cap = (
        f" Showing top {embedded:,} by shift magnitude."
        if embedded < n
        else ""
    )
    davis_note = ""
    if not meta.get("davis_cache"):
        davis_note = (
            " Movement categories need a Davis cache "
            f"(analysis-davis-raw-{meta.get('avail_start')}_{meta.get('avail_end')}.parquet"
            " or any analysis-davis-raw-*.parquet) — loads show as Unclassified until built."
        )
    elif meta.get("n_tail_classified") is not None and meta.get("n_tail_loads"):
        classified = meta["n_tail_classified"]
        total = meta["n_tail_loads"]
        if classified < total:
            davis_note = (
                f" Davis cache labels {classified:,} of {total:,} tail loads; "
                f"loads outside the cache window stay Unclassified."
            )
    base = (
        f"Problem loads ({rule}, avail→48hr) in this cohort: "
        f"<strong>{n:,}</strong>.{cap} Search by loadnumber or filter by movement category, "
        "path shape, and (for Lead Time) high-confidence clock-only loads without lag7/fuel/DAT drift."
        f"{davis_note} "
        "Chart shows indexed values (Available = 100) and absolute $ at each checkpoint."
    )
    if meta.get("explorer_annotations"):
        base += (
            " Vertical markers on the explorer chart show when material feature "
            "changes were first observed between checkpoints (charges, happy path, hard_ft, "
            "equip/index). is_flamed is not in model logs — hard_ft_cnt is the "
            "difficulty proxy."
        )
    if meta.get("explorer_mde"):
        base += (
            " Purple vertical markers and the MDE banner show "
            "<strong>Market Movement</strong> MDE applications only "
            "(excludes HVHR, Lane Maker, and experiment MDEs)."
        )
    return base


def render_drift_html(payload: dict[str, Any]) -> str:
    """Render the Chart.js HTML shell (matches exec drift report layout)."""
    meta = payload["meta"]
    data: dict[str, Any] = {
        "summary": payload["summary"],
        "curve": payload["curve"],
        "meta": meta,
    }
    if "explorer" in payload:
        data["explorer"] = payload["explorer"]
    if "category_breakdown" in payload:
        data["category_breakdown"] = payload["category_breakdown"]
    if "leadtime_archetype_breakdown" in payload:
        data["leadtime_archetype_breakdown"] = payload["leadtime_archetype_breakdown"]
    if "leadtime_confidence_breakdown" in payload:
        data["leadtime_confidence_breakdown"] = payload["leadtime_confidence_breakdown"]
    if "movement_qa" in payload:
        data["movement_qa"] = payload["movement_qa"]
    if "movement_categories" in payload:
        data["movement_categories"] = payload["movement_categories"]
    data_json = json.dumps(data, separators=(",", ":"))
    template_path = Path(__file__).parent / "templates" / "etp_drift_report.html"
    html = template_path.read_text()
    html = html.replace("__EYEBROW__", _eyebrow(meta))
    html = html.replace("__COHORT_STRIP__", _cohort_strip_html(meta))
    html = html.replace(
        "__ATTRITION_TITLE__",
        meta.get("attrition_title", "Telemetry coverage at checkpoints"),
    )
    explorer_note = _note_explorer(meta)
    html = html.replace("__CATEGORY_SECTION__", _category_section_html(meta))
    html = html.replace("__EXPLORER_SECTION__", _explorer_section_html(meta, explorer_note))
    html = html.replace("__NOTE_DRIFT__", _note_drift(meta))
    html = html.replace("__NOTE_ATTRITION__", _note_attrition(meta))
    html = html.replace("__NOTE_TABLE__", _note_table(meta))
    html = html.replace("__NOTE_AVMED__", _note_avmed(meta))
    html = html.replace("__METHODOLOGY__", _methodology(meta))
    html = html.replace("__DATA_JSON__", data_json)
    return html


def build_drift_report(
    *,
    hist_path: Path,
    per_load_path: Path,
    out_path: Path,
    lead_min_days: float = DEFAULT_LEAD_MIN_DAYS,
    lead_max_days: float = DEFAULT_LEAD_MAX_DAYS,
    avail_start: date | None = None,
    avail_end: date | None = None,
    tail_pct: float | None = None,
    tail_amt: float | None = None,
    tail_mode: str | None = None,
    davis_path: Path | None = None,
    max_explorer_loads: int = MAX_EXPLORER_LOADS_DEFAULT,
    report_variant: str | None = None,
    debug: bool = False,
    feature_snapshots_path: Path | None = None,
    mde_timeline_cache: Path | str | None = None,
    mde_timeline_force: bool = False,
) -> dict[str, Any]:
    """End-to-end: filter cohort, aggregate checkpoints, write HTML."""
    per_load = pl.read_parquet(per_load_path)
    cohort_ids = lead_window_ids(
        per_load,
        min_days=lead_min_days,
        max_days=lead_max_days,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    if cohort_ids.is_empty():
        raise ValueError("cohort empty — widen filters")

    from dqt.etp_slider.problem_loads.tail import (
        TAIL_AMT_DEFAULT,
        TAIL_PCT_DEFAULT,
        TAIL_THRESHOLD_MODE_DEFAULT,
    )

    if tail_pct is None and tail_amt is None:
        tail_pct = TAIL_PCT_DEFAULT
        tail_amt = TAIL_AMT_DEFAULT

    tail: pl.DataFrame | None = None
    feat_path = feature_snapshots_path or resolve_feature_snapshots_path(hist_path.parent)
    if _tail_filters_active(tail_pct, tail_amt):
        tail = _prepare_tail_cohort(
            per_load,
            cohort_ids,
            tail_pct=tail_pct,
            tail_amt=tail_amt,
            tail_mode=tail_mode or TAIL_THRESHOLD_MODE_DEFAULT,
            davis_path=davis_path,
            feature_snapshots_path=feat_path,
        )

    hist = pl.read_parquet(hist_path)
    feat_df: pl.DataFrame | None = None
    if tail is not None and not tail.is_empty() and feat_path is not None:
        embedded_ids = (
            tail.sort("etp50_shift_pct", descending=True)
            .head(max_explorer_loads)["loadnumber"]
            .to_list()
        )
        feat_df = load_feature_snapshots(feat_path, embedded_ids)
    if mde_timeline_cache is None and avail_start is not None and avail_end is not None:
        mde_timeline_cache = hist_path.parent / f"explorer-mde-timeline-{avail_start}_{avail_end}.parquet"
    payload = build_report_payload(
        hist,
        per_load,
        cohort_ids,
        lead_min_days=lead_min_days,
        lead_max_days=lead_max_days,
        avail_start=avail_start,
        avail_end=avail_end,
        tail=tail,
        tail_pct=tail_pct,
        tail_amt=tail_amt,
        tail_mode=tail_mode or TAIL_THRESHOLD_MODE_DEFAULT,
        max_explorer_loads=max_explorer_loads,
        debug=debug,
        feature_snapshots=feat_df,
        mde_timeline_cache=mde_timeline_cache,
        mde_timeline_force=mde_timeline_force,
        hist_path=hist_path,
        data_dir=hist_path.parent.parent if hist_path.parent.name == "etp" else hist_path.parent,
    )
    if tail is not None and not tail.is_empty() and "primary_category" in tail.columns:
        payload["meta"]["n_tail_classified"] = int(
            tail.filter(pl.col("primary_category") != "Unclassified").height
        )

    if davis_path is not None and davis_path.exists():
        payload["meta"]["davis_cache"] = str(davis_path)
    apply_report_variant_meta(payload["meta"], report_variant)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_drift_html(payload))
    return {"out_path": str(out_path), "meta": payload["meta"]}


__all__ = [
    "BUCKET_LABELS",
    "CHECKPOINT_HRS",
    "DEFAULT_AVAIL_END",
    "DEFAULT_AVAIL_START",
    "DEFAULT_LEAD_MAX_DAYS",
    "DEFAULT_LEAD_MIN_DAYS",
    "EXPLORER_METRICS",
    "HC_GALLERY_SIZE_DEFAULT",
    "HISTORY_END_HRS_BEFORE_PICKUP",
    "MARK_HRS",
    "MART_MIN_BOOKING_HRS",
    "MAX_EXPLORER_LOADS_DEFAULT",
    "REPORT_VARIANTS",
    "apply_report_variant_meta",
    "build_category_breakdown",
    "build_cohort_meta",
    "build_curve",
    "build_drift_report",
    "build_load_explorer",
    "build_load_feature_annotations",
    "build_report_payload",
    "build_summary",
    "lead_window_ids",
    "load_feature_snapshots",
    "render_drift_html",
    "resolve_davis_cache",
    "resolve_feature_snapshots_path",
]

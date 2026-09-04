"""Extract and classify problem ETP50 tail loads (long-lead cohort).

Default problem-load gate: **≥10%** ETP50 avail→48hr shift (pct-only).
Use ``--tail-amt`` / ``--tail-mode`` for $ floors or scaled rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

from dqt.etp_slider.drift.report import _indexed_at_marks, lead_window_ids

TAIL_PCT_DEFAULT = 0.10
TAIL_AMT_DEFAULT: float | None = None
TAIL_BASELINE_COL_DEFAULT = "etp50_avail"
TailThresholdMode = Literal["and", "or", "scaled"]
TAIL_THRESHOLD_MODE_DEFAULT: TailThresholdMode = "and"
LEAD_MIN_DAYS_DEFAULT = 7
LEAD_MAX_DAYS_DEFAULT = 14

CHECKPOINT_MARKS: tuple[int, ...] = (168, 48, 24)

_DAVIS_FLAG_COLS: tuple[str, ...] = (
    "total_charges_delta",
    "hard_ft_delta",
    "book_2_pkup_delta",
    "avail_2_book_delta",
    "dat_rate_delta",
    "fuel_cost_delta",
    "lag7_cpm_delta",
    "is_hyperlocal",
    "charge_inc_ind",
    "equip_change_ind",
    "hard_ft_inc_ind",
    "clocks_moved_ind",
    "clock_only_ind",
    "index_change_ind",
)


def filter_lead_window(
    per_load: pl.DataFrame,
    *,
    min_d: float = LEAD_MIN_DAYS_DEFAULT,
    max_d: float = LEAD_MAX_DAYS_DEFAULT,
    avail_start: date | str | None = None,
    avail_end: date | str | None = None,
) -> pl.DataFrame:
    """Rows in booking window, optionally filtered by made-available date."""
    if "booking_window_hrs" not in per_load.columns:
        raise ValueError("per_load missing booking_window_hrs")
    lo_h, hi_h = min_d * 24, max_d * 24
    out = per_load.filter(pl.col("booking_window_hrs").is_between(lo_h, hi_h, closed="both"))
    if avail_start is not None:
        start = date.fromisoformat(avail_start) if isinstance(avail_start, str) else avail_start
        out = out.filter(pl.col("made_available_utc").dt.date() >= pl.lit(start))
    if avail_end is not None:
        end = date.fromisoformat(avail_end) if isinstance(avail_end, str) else avail_end
        out = out.filter(pl.col("made_available_utc").dt.date() <= pl.lit(end))
    return out


@dataclass(frozen=True)
class TailThresholdConfig:
    """Problem-load gate on ETP50 avail→48hr endpoint shift.

    Modes
    -----
    and (default)
        Both pct and $ floors must pass — reduces noise on small $ / small % moves.
    or
        Either floor passes — inclusive; can ~2× cohort vs pct-only.
    scaled
        ``shift_amt >= max(amt_floor, pct × baseline_etp)`` — one $ bar that scales
        with load size while keeping a noise floor for cheap freight.
    """

    pct: float | None = TAIL_PCT_DEFAULT
    amt: float | None = TAIL_AMT_DEFAULT
    mode: TailThresholdMode = TAIL_THRESHOLD_MODE_DEFAULT
    baseline_col: str = TAIL_BASELINE_COL_DEFAULT

    @classmethod
    def defaults(cls) -> TailThresholdConfig:
        return cls()

    def active(self) -> bool:
        return (self.pct is not None and self.pct > 0) or (self.amt is not None and self.amt > 0)


def tail_threshold_expr(
    *,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = TAIL_AMT_DEFAULT,
    mode: TailThresholdMode = TAIL_THRESHOLD_MODE_DEFAULT,
    baseline_col: str = TAIL_BASELINE_COL_DEFAULT,
    shift_col: str = "etp50_shift_pct",
    shift_amt_col: str = "etp50_shift_amt",
) -> pl.Expr:
    """Polars expression for problem-load membership."""
    pct = pct_threshold if pct_threshold is not None else TAIL_PCT_DEFAULT

    if mode == "scaled":
        baseline = pl.col(baseline_col).fill_null(0).abs()
        scaled_amt = baseline * pct
        if amt_threshold is not None and amt_threshold > 0:
            required_amt = pl.max_horizontal(pl.lit(amt_threshold), scaled_amt)
        else:
            required_amt = scaled_amt
        return pl.col(shift_amt_col) >= required_amt

    parts: list[pl.Expr] = []
    if pct_threshold is not None and pct_threshold > 0:
        parts.append(pl.col(shift_col) >= pct_threshold)
    if amt_threshold is not None and amt_threshold > 0:
        parts.append(pl.col(shift_amt_col) >= amt_threshold)
    if not parts:
        raise ValueError("at least one of pct_threshold or amt_threshold is required")

    if mode == "or":
        expr = parts[0]
        for part in parts[1:]:
            expr = expr | part
        return expr

    if mode == "and":
        return pl.all_horizontal(parts)

    raise ValueError(f"unknown tail threshold mode {mode!r} — use and | or | scaled")


def format_tail_threshold(
    *,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = TAIL_AMT_DEFAULT,
    mode: TailThresholdMode = TAIL_THRESHOLD_MODE_DEFAULT,
) -> str:
    """Human-readable problem-load rule for report copy."""
    if mode == "scaled":
        pct = pct_threshold if pct_threshold is not None else TAIL_PCT_DEFAULT
        pct_part = f"{pct * 100:.0f}% × ETP50 at available"
        if amt_threshold is not None and amt_threshold > 0:
            return f"shift ≥ max(${amt_threshold:,.0f}, {pct_part})"
        return f"shift ≥ {pct_part}"

    bits: list[str] = []
    if pct_threshold is not None and pct_threshold > 0:
        bits.append(f"≥{pct_threshold * 100:.0f}%")
    if amt_threshold is not None and amt_threshold > 0:
        bits.append(f"≥${amt_threshold:,.0f}")
    if not bits:
        return "disabled"
    joiner = " or " if mode == "or" else " and "
    return joiner.join(bits)


def flag_tail_loads(
    per_load: pl.DataFrame,
    *,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = TAIL_AMT_DEFAULT,
    mode: TailThresholdMode = TAIL_THRESHOLD_MODE_DEFAULT,
    baseline_col: str = TAIL_BASELINE_COL_DEFAULT,
    shift_col: str = "etp50_shift_pct",
    shift_amt_col: str = "etp50_shift_amt",
) -> pl.DataFrame:
    """Mark problem loads using configured pct / $ / mode rules."""
    if shift_col not in per_load.columns:
        raise ValueError(f"per_load missing {shift_col}")
    if amt_threshold is not None and amt_threshold > 0 and shift_amt_col not in per_load.columns:
        raise ValueError(f"per_load missing {shift_amt_col}")
    if mode == "scaled" and baseline_col not in per_load.columns:
        raise ValueError(f"per_load missing {baseline_col} (required for scaled mode)")
    return per_load.with_columns(
        tail_threshold_expr(
            pct_threshold=pct_threshold,
            amt_threshold=amt_threshold,
            mode=mode,
            baseline_col=baseline_col,
            shift_col=shift_col,
            shift_amt_col=shift_amt_col,
        ).alias("is_tail"),
    )


def enrich_davis_flags(raw: pl.DataFrame) -> pl.DataFrame:
    """Apply movement-category indicator columns (mirrors analysis-davis notebook)."""
    from dqt.etp_slider.movement.orders import enrich_movement_flags

    return enrich_movement_flags(raw)


def assign_movement_category(df: pl.DataFrame) -> pl.DataFrame:
    """Delegate to shared taxonomy in :mod:`dqt.analysis_orders`."""
    from dqt.etp_slider.movement.orders import assign_movement_category as _assign

    return _assign(df)


def per_load_checkpoint_shifts(
    hist: pl.DataFrame,
    load_ids: pl.DataFrame,
    *,
    marks: tuple[int, ...] = CHECKPOINT_MARKS,
) -> pl.DataFrame:
    """Pivot ETP50 indexed values (Available = 100) at selected checkpoints."""
    indexed = _indexed_at_marks(hist, load_ids.select("loadnumber"))
    etp50 = indexed["ETP50"].filter(pl.col("mark_hrs").is_in(marks))
    wide = etp50.pivot(on="mark_hrs", index="loadnumber", values="idx")
    rename = {str(m): f"etp50_idx_{m}h" for m in marks}
    return wide.rename(rename)


def tail_segment_summary(tail: pl.DataFrame) -> pl.DataFrame:
    """Aggregate shift stats by primary movement category."""
    if tail.is_empty():
        raise ValueError("tail cohort empty — widen filters or lower threshold")
    n = tail.height
    return (
        tail.group_by("primary_category")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_pct").median().alias("med_shift_pct"),
        )
        .with_columns((pl.col("n_loads") / n).alias("pct_of_tail"))
        .sort("n_loads", descending=True)
    )


def tail_examples(
    tail: pl.DataFrame,
    *,
    n_per_category: int = 5,
) -> pl.DataFrame:
    """Top movers per category for Ops visualization."""
    cols = [
        c
        for c in (
            "loadnumber",
            "primary_category",
            "etp50_shift_pct",
            "etp50_shift_amt",
            "total_charges_delta",
            "hard_ft_delta",
            "book_2_pkup_delta",
        )
        if c in tail.columns
    ]
    return (
        tail.sort("etp50_shift_pct", descending=True)
        .group_by("primary_category", maintain_order=True)
        .head(n_per_category)
        .select(cols)
    )


def build_tail_dataset(
    *,
    per_load: pl.DataFrame | None = None,
    hist: pl.DataFrame | None = None,
    per_load_path: Path | str | None = None,
    hist_path: Path | str | None = None,
    davis_path: Path | str | None = None,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = TAIL_AMT_DEFAULT,
    threshold_mode: TailThresholdMode = TAIL_THRESHOLD_MODE_DEFAULT,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    avail_start: date | str | None = None,
    avail_end: date | str | None = None,
    shift_col: str = "etp50_shift_pct",
) -> pl.DataFrame:
    """Build enriched tail load frame (one row per problem load)."""
    if per_load is None:
        if per_load_path is None:
            raise ValueError("Provide per_load or per_load_path")
        per_load = pl.read_parquet(per_load_path)

    cohort = filter_lead_window(
        per_load,
        min_d=lead_min,
        max_d=lead_max,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    flagged = flag_tail_loads(
        cohort,
        pct_threshold=pct_threshold,
        amt_threshold=amt_threshold,
        mode=threshold_mode,
        shift_col=shift_col,
    )
    tail = flagged.filter(pl.col("is_tail"))
    if tail.is_empty():
        raise ValueError("tail cohort empty — widen filters or lower threshold")

    if hist is None:
        if hist_path is None:
            raise ValueError("Provide hist or hist_path")
        hist = pl.read_parquet(hist_path)

    checkpoints = per_load_checkpoint_shifts(hist, tail.select("loadnumber"))
    tail = tail.join(checkpoints, on="loadnumber", how="left")

    if davis_path is not None:
        davis = enrich_davis_flags(pl.read_parquet(davis_path))
        flag_cols = [c for c in _DAVIS_FLAG_COLS if c in davis.columns]
        tail = tail.join(
            davis.select("loadnumber", *flag_cols),
            on="loadnumber",
            how="left",
        )
        for col in (
            "charge_inc_ind",
            "hard_ft_inc_ind",
            "clocks_moved_ind",
            "clock_only_ind",
            "index_change_ind",
        ):
            if col in tail.columns:
                tail = tail.with_columns(pl.col(col).fill_null(0).cast(pl.Int8))

    return assign_movement_category(tail)


def build_movement_qa(tail: pl.DataFrame) -> dict[str, Any]:
    """QA splits: lead-time clock detection, blocked clocks, random catch-all."""
    from dqt.etp_slider.movement.orders import CLOCK_DELTA_MIN, LEGACY_LEADTIME_CATEGORY

    if tail.is_empty():
        return {}

    out: dict[str, Any] = {"clock_delta_min": CLOCK_DELTA_MIN}

    if "book_2_pkup_delta" in tail.columns:
        abs_delta = tail["book_2_pkup_delta"].drop_nulls().abs()
        if abs_delta.len() > 0:
            out["book_2_pkup_delta_abs"] = {
                "n": abs_delta.len(),
                "p25": round(float(abs_delta.quantile(0.25)), 1),
                "p50": round(float(abs_delta.quantile(0.50)), 1),
                "p75": round(float(abs_delta.quantile(0.75)), 1),
                "pct_ge_threshold": round(float((abs_delta >= CLOCK_DELTA_MIN).mean()), 4),
            }

    if "clocks_moved_ind" not in tail.columns:
        return out

    det = tail.filter(pl.col("primary_category").is_in(["LeadtimeChange", LEGACY_LEADTIME_CATEGORY]))
    rand = tail.filter(pl.col("primary_category") == "RandomChange")
    blocked = tail.filter(
        (pl.col("clocks_moved_ind") == 1) & pl.col("primary_category").is_in(["ShipmentChange", "DifficultyOverride"])
    )

    out["leadtime"] = {
        "n": det.height,
        "n_clock_only": int(det.filter(pl.col("clock_only_ind") == 1).height)
        if "clock_only_ind" in det.columns
        else det.height,
        "avg_shift_amt": round(float(det["etp50_shift_amt"].mean()), 2) if det.height else None,
    }
    out["clocks_blocked_by_priority"] = blocked.height

    if rand.height and "index_change_ind" in rand.columns:
        out["random_change"] = {
            "n": rand.height,
            "n_index_detected": int(rand.filter(pl.col("index_change_ind") == 1).height),
            "n_catchall": int(rand.filter(pl.col("index_change_ind") == 0).height),
        }

    return out


def cohort_load_ids(
    per_load: pl.DataFrame,
    *,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    avail_start: date | None = None,
    avail_end: date | None = None,
) -> pl.DataFrame:
    """Loadnumbers in lead window (compat wrapper around drift report helper)."""
    return lead_window_ids(
        per_load,
        min_days=lead_min,
        max_days=lead_max,
        avail_start=avail_start,
        avail_end=avail_end,
    )


__all__ = [
    "CHECKPOINT_MARKS",
    "LEAD_MAX_DAYS_DEFAULT",
    "LEAD_MIN_DAYS_DEFAULT",
    "TAIL_AMT_DEFAULT",
    "TAIL_BASELINE_COL_DEFAULT",
    "TAIL_PCT_DEFAULT",
    "TAIL_THRESHOLD_MODE_DEFAULT",
    "TailThresholdConfig",
    "TailThresholdMode",
    "assign_movement_category",
    "build_movement_qa",
    "build_tail_dataset",
    "cohort_load_ids",
    "enrich_davis_flags",
    "filter_lead_window",
    "flag_tail_loads",
    "format_tail_threshold",
    "per_load_checkpoint_shifts",
    "tail_examples",
    "tail_segment_summary",
    "tail_threshold_expr",
]

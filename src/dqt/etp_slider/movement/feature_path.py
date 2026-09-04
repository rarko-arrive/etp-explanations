"""Path-based feature detection along the avail→48hr checkpoint path.

Explorer timeline markers scan consecutive checkpoints; Davis endpoint flags can
miss mid-path moves that net out by 48hr. OR path signals into movement flags
so classification matches what the drift explorer shows.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import polars as pl

from dqt.etp_slider.movement.orders import refresh_movement_derived_flags

# Match drift explorer annotation thresholds.
CHARGE_PATH_EPS = 50.0
CLHP_PATH_EPS = 50.0
HARD_FT_PATH_EPS = 1.0

PATH_MARK_HRS: tuple[int, ...] = (999, 168, 144, 120, 96, 72, 48, 24)
CLHP_VALUE_COLS: tuple[str, ...] = ("clhp_pred_tot_cost", "clhp_pred_line")


def _feat_at_mark(sub: pl.DataFrame, col: str, mark_hrs: int) -> Any:
    """First feature reading at/after checkpoint mark (999 = available)."""
    if col not in sub.columns:
        return None
    rows = sub.filter(pl.col(col).is_not_null())
    if rows.is_empty():
        return None
    if mark_hrs == 999:
        row = rows.sort(["hours_since_available", "snapshot_utc"]).head(1)
    else:
        row = (
            rows.filter(pl.col("hours_before_pickup") >= mark_hrs)
            .sort(["hours_before_pickup", "hours_since_available"])
            .head(1)
        )
    if row.is_empty():
        return None
    return row[col][0]


def _checkpoint_numeric_values(feat: pl.DataFrame, loadnumber: int, col: str) -> dict[int, float]:
    sub = feat.filter(pl.col("loadnumber") == loadnumber)
    if sub.is_empty() or col not in sub.columns:
        return {}
    out: dict[int, float] = {}
    for mark in PATH_MARK_HRS:
        val = _feat_at_mark(sub, col, mark)
        if val is not None:
            out[mark] = float(val)
    return out


def _checkpoint_clhp_values(feat: pl.DataFrame, loadnumber: int) -> dict[int, float]:
    sub = feat.filter(pl.col("loadnumber") == loadnumber)
    if sub.is_empty():
        return {}
    col = next((c for c in CLHP_VALUE_COLS if c in sub.columns), None)
    if col is None:
        return {}
    return _checkpoint_numeric_values(feat, loadnumber, col)


def _load_has_abs_path_change(cps: dict[int, float], *, eps: float) -> bool:
    for m0, m1 in pairwise(PATH_MARK_HRS):
        if m0 not in cps or m1 not in cps:
            continue
        if abs(cps[m1] - cps[m0]) >= eps:
            return True
    return False


def _load_has_charge_path_increase(cps: dict[int, float], *, eps: float = CHARGE_PATH_EPS) -> bool:
    for m0, m1 in pairwise(PATH_MARK_HRS):
        if m0 not in cps or m1 not in cps:
            continue
        if cps[m1] - cps[m0] > eps:
            return True
    return False


def _load_has_categorical_path_change(feat: pl.DataFrame, loadnumber: int, col: str) -> bool:
    sub = feat.filter(pl.col("loadnumber") == loadnumber)
    if sub.is_empty() or col not in sub.columns:
        return False
    cps: dict[int, Any] = {}
    for mark in PATH_MARK_HRS:
        val = _feat_at_mark(sub, col, mark)
        if val is not None:
            cps[mark] = val
    for m0, m1 in pairwise(PATH_MARK_HRS):
        if m0 not in cps or m1 not in cps:
            continue
        v0, v1 = cps[m0], cps[m1]
        if v0 != v1 and v1 is not None:
            return True
    return False


def detect_feature_path_flags(feat: pl.DataFrame) -> pl.DataFrame:
    """Per-load path indicators aligned with drift explorer checkpoint scans."""
    schema = {
        "loadnumber": pl.Int64,
        "clhp_path_change_ind": pl.Int8,
        "charge_path_inc_ind": pl.Int8,
        "hard_ft_path_change_ind": pl.Int8,
        "equip_path_change_ind": pl.Int8,
        "path_taken_change_ind": pl.Int8,
    }
    if feat.is_empty() or "loadnumber" not in feat.columns:
        return pl.DataFrame(schema=schema)

    rows: list[dict[str, Any]] = []
    for ln in feat["loadnumber"].unique().to_list():
        ln_i = int(ln)
        clhp_cps = _checkpoint_clhp_values(feat, ln_i)
        charge_cps = _checkpoint_numeric_values(feat, ln_i, "total_charges")
        hard_ft_cps = _checkpoint_numeric_values(feat, ln_i, "hard_ft_cnt")
        rows.append(
            {
                "loadnumber": ln_i,
                "clhp_path_change_ind": int(_load_has_abs_path_change(clhp_cps, eps=CLHP_PATH_EPS)),
                "charge_path_inc_ind": int(_load_has_charge_path_increase(charge_cps, eps=CHARGE_PATH_EPS)),
                "hard_ft_path_change_ind": int(_load_has_abs_path_change(hard_ft_cps, eps=HARD_FT_PATH_EPS)),
                "equip_path_change_ind": int(_load_has_categorical_path_change(feat, ln_i, "load_type")),
                "path_taken_change_ind": int(_load_has_categorical_path_change(feat, ln_i, "path_taken")),
            }
        )
    return pl.DataFrame(rows)


def apply_feature_path_upgrades(df: pl.DataFrame, feat: pl.DataFrame | None) -> pl.DataFrame:
    """OR path-based signals into movement flags and refresh derived columns."""
    if feat is None or feat.is_empty() or df.is_empty():
        return df
    path = detect_feature_path_flags(feat)
    if path.is_empty():
        return df

    out = df.join(path, on="loadnumber", how="left")
    for col in (
        "clhp_path_change_ind",
        "charge_path_inc_ind",
        "hard_ft_path_change_ind",
        "equip_path_change_ind",
        "path_taken_change_ind",
    ):
        if col in out.columns:
            out = out.with_columns(pl.col(col).fill_null(0).cast(pl.Int8))

    if "clhp_change_ind" not in out.columns:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("clhp_change_ind"))
    out = out.with_columns(
        (pl.col("clhp_change_ind") | pl.col("clhp_path_change_ind")).cast(pl.Int8).alias("clhp_change_ind")
    )

    if "charge_inc_ind" not in out.columns:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("charge_inc_ind"))
    out = out.with_columns(
        (pl.col("charge_inc_ind") | pl.col("charge_path_inc_ind")).cast(pl.Int8).alias("charge_inc_ind")
    )

    if "hard_ft_inc_ind" not in out.columns:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("hard_ft_inc_ind"))
    out = out.with_columns(
        (pl.col("hard_ft_inc_ind") | pl.col("hard_ft_path_change_ind")).cast(pl.Int8).alias("hard_ft_inc_ind")
    )

    if "equip_change_ind" not in out.columns:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("equip_change_ind"))
    out = out.with_columns(
        (pl.col("equip_change_ind") | pl.col("equip_path_change_ind")).cast(pl.Int8).alias("equip_change_ind")
    )

    if "path_taken_change_ind" not in out.columns:
        out = out.with_columns(pl.lit(0).cast(pl.Int8).alias("path_taken_change_ind"))

    return refresh_movement_derived_flags(out)


__all__ = [
    "CHARGE_PATH_EPS",
    "CLHP_PATH_EPS",
    "HARD_FT_PATH_EPS",
    "PATH_MARK_HRS",
    "apply_feature_path_upgrades",
    "detect_feature_path_flags",
]

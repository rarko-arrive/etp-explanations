"""Descriptive attribution of ETP endpoint shifts to movement signals."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from dqt.etp_slider.movement.orders import INDEX_DELTA_SPECS

# Clock + index features for shift attribution (Davis avail→48hr deltas).
MOVEMENT_SHIFT_FEATURES: tuple[tuple[str, str], ...] = (
    ("book_2_pkup_delta", "book_2_pkup"),
    ("avail_2_book_delta", "avail_2_book"),
    ("lag7_cpm_delta", "lag7_cpm"),
    ("fuel_cost_delta", "fuel_cost"),
    ("dat_rate_delta", "dat_rate"),
)

OVERLAP_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("index_only", "Index only", "index_only_ind"),
    ("leadtime_isolated", "Lead time isolated", "leadtime_isolated_ind"),
    ("both", "Index + clocks (both)", "index_clock_overlap_ind"),
)


def movement_overlap_summary(
    df: pl.DataFrame,
    *,
    pct_denominator: int | None = None,
) -> pl.DataFrame:
    """Share of loads in each index/clock overlap segment with shift stats."""
    if df.is_empty():
        return pl.DataFrame()

    n = pct_denominator if pct_denominator is not None else df.height
    rows: list[dict[str, Any]] = []

    for key, label, col in OVERLAP_SEGMENTS:
        if col not in df.columns:
            continue
        sub = df.filter(pl.col(col) == 1)
        if sub.is_empty():
            continue
        rows.append(_overlap_row(key, label, sub, n))

    if "index_change_ind" in df.columns and "clocks_moved_ind" in df.columns:
        neither = df.filter(
            (pl.col("index_change_ind") == 0) & (pl.col("clocks_moved_ind") == 0)
        )
        if neither.height:
            rows.append(_overlap_row("neither", "Neither index nor clocks", neither, n))

    if not rows:
        return pl.DataFrame()

    order = {key: i for i, (key, _, _) in enumerate(OVERLAP_SEGMENTS)}
    order["neither"] = len(order)
    rows.sort(key=lambda r: order.get(r["segment"], 99))
    return pl.DataFrame(rows)


def movement_overlap_by_primary(df: pl.DataFrame) -> pl.DataFrame:
    """Cross-tab overlap segment × primary_category (problem-load QA view)."""
    if df.is_empty() or "primary_category" not in df.columns:
        return pl.DataFrame()

    rows: list[dict[str, Any]] = []
    for key, label, col in OVERLAP_SEGMENTS:
        if col not in df.columns:
            continue
        sub = df.filter(pl.col(col) == 1)
        if sub.is_empty():
            continue
        for cat, cnt in (
            sub.group_by("primary_category").len().iter_rows()
        ):
            rows.append(
                {
                    "overlap_segment": key,
                    "overlap_label": label,
                    "primary_category": cat,
                    "n_loads": int(cnt),
                }
            )
    return pl.DataFrame(rows)


def movement_shift_attribution(
    df: pl.DataFrame,
    *,
    target_amt: str = "etp50_shift_amt",
    target_pct: str = "etp50_shift_pct",
) -> dict[str, pl.DataFrame]:
    """Univariate and multivariate descriptive links to ETP shift (not causal OLS).

    Returns:
        ``univariate`` — Pearson r, slope ($ shift per unit Δ), n per feature.
        ``multivariate`` — OLS coefficients on all features jointly (+ intercept, R²).
    """
    features = [col for col, _ in MOVEMENT_SHIFT_FEATURES if col in df.columns]
    if not features or target_amt not in df.columns:
        return {}

    work = df.filter(pl.col(target_amt).is_finite())
    uni_rows: list[dict[str, Any]] = []
    for col, label in MOVEMENT_SHIFT_FEATURES:
        if col not in work.columns:
            continue
        sub = work.filter(pl.col(col).is_finite())
        n = sub.height
        if n < 3:
            continue
        x = sub[col].to_numpy()
        y_amt = sub[target_amt].to_numpy()
        y_pct = sub[target_pct].to_numpy() if target_pct in sub.columns else None
        r_amt = float(np.corrcoef(x, y_amt)[0, 1]) if np.std(x) > 0 and np.std(y_amt) > 0 else None
        slope_amt = float(np.cov(x, y_amt, ddof=0)[0, 1] / np.var(x)) if np.var(x) > 0 else None
        row: dict[str, Any] = {
            "feature": col,
            "label": label,
            "n": n,
            "corr_shift_amt": round(r_amt, 4) if r_amt is not None else None,
            "slope_amt_per_unit": round(slope_amt, 4) if slope_amt is not None else None,
        }
        if y_pct is not None and np.std(y_pct) > 0 and np.std(x) > 0:
            row["corr_shift_pct"] = round(float(np.corrcoef(x, y_pct)[0, 1]), 4)
        uni_rows.append(row)

    multi = _multivariate_ols(work, target_amt, features)
    out: dict[str, pl.DataFrame] = {}
    if uni_rows:
        out["univariate"] = pl.DataFrame(uni_rows)
    if multi is not None:
        out["multivariate"] = multi
    return out


def _overlap_row(key: str, label: str, sub: pl.DataFrame, n: int) -> dict[str, Any]:
    return {
        "segment": key,
        "label": label,
        "n_loads": sub.height,
        "pct_of_group": round(sub.height / n, 4) if n else 0.0,
        "avg_shift_amt": round(float(sub["etp50_shift_amt"].mean() or 0), 2)
        if "etp50_shift_amt" in sub.columns
        else None,
        "avg_shift_pct": round(float(sub["etp50_shift_pct"].mean() or 0), 4)
        if "etp50_shift_pct" in sub.columns
        else None,
        "pct_index_primary": round(
            float(sub.filter(pl.col("primary_category") == "RandomChange").height / sub.height), 4
        )
        if "primary_category" in sub.columns and sub.height
        else None,
        "pct_leadtime_primary": round(
            float(
                sub.filter(
                    pl.col("primary_category").is_in(["LeadtimeChange", "DeterministicChange"])
                ).height
                / sub.height
            ),
            4,
        )
        if "primary_category" in sub.columns and sub.height
        else None,
    }


def _multivariate_ols(
    df: pl.DataFrame,
    target: str,
    features: list[str],
) -> pl.DataFrame | None:
    exprs = [pl.col(target).is_finite(), *[pl.col(c).is_finite() for c in features]]
    sub = df.filter(pl.all_horizontal(exprs))
    if sub.height < len(features) + 2:
        return None

    y = sub[target].to_numpy()
    x = sub.select(features).to_numpy()
    design = np.column_stack([np.ones(len(y)), x])
    beta, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    y_hat = design @ beta
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None

    rows: list[dict[str, Any]] = [
        {
            "term": "intercept",
            "label": "Intercept",
            "coef_usd": round(float(beta[0]), 4),
            "n_obs": sub.height,
            "r_squared": round(r2, 4) if r2 is not None else None,
        }
    ]
    x_std = x.std(axis=0)
    y_std = float(y.std())
    for i, (col, label) in enumerate(
        pair for pair in MOVEMENT_SHIFT_FEATURES if pair[0] in features
    ):
        coef = float(beta[i + 1])
        std_beta = (coef * x_std[i] / y_std) if y_std > 0 and x_std[i] > 0 else None
        rows.append(
            {
                "term": col,
                "label": label,
                "coef_usd": round(coef, 4),
                "std_coef": round(std_beta, 4) if std_beta is not None else None,
                "n_obs": sub.height,
                "r_squared": round(r2, 4) if r2 is not None else None,
            }
        )
    return pl.DataFrame(rows)


__all__ = [
    "INDEX_DELTA_SPECS",
    "MOVEMENT_SHIFT_FEATURES",
    "OVERLAP_SEGMENTS",
    "movement_overlap_by_primary",
    "movement_overlap_summary",
    "movement_shift_attribution",
]

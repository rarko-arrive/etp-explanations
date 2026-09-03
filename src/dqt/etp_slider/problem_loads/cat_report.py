"""Three-way movement category report (LoadFeatures / LeadTime / Random).

Collapses the four public movement labels into the mart-aligned trio requested
for executive readouts, with segment pivots and per-category sub-breakdowns.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from dqt.etp_slider.movement.orders import LEGACY_LEADTIME_CATEGORY
from dqt.etp_slider.problem_loads.cohort import (
    build_cohort_funnel,
    load_cohort_universe_summary,
    segment_by_category_overview,
    segment_overview,
    shift_magnitude_stats,
)
from dqt.etp_slider.problem_loads.leadtime import (
    build_leadtime_archetype_breakdown,
    build_leadtime_confidence_breakdown,
    leadtime_archetype_pricing_pivot,
    leadtime_pricing_accuracy_by_archetype,
    secondary_flags_summary,
)
from dqt.etp_slider.problem_loads.shift_direction import (
    shift_direction_by_group,
    shift_direction_rates,
)
from dqt.etp_slider.problem_loads.tail import (
    LEAD_MAX_DAYS_DEFAULT,
    LEAD_MIN_DAYS_DEFAULT,
    TAIL_PCT_DEFAULT,
    TailThresholdMode,
    format_tail_threshold,
)

COLLAPSED_ORDER: tuple[str, ...] = ("LoadFeatures", "LeadTime", "Random")
COLLAPSED_LABELS: dict[str, str] = {
    "LoadFeatures": "Load Features",
    "LeadTime": "Lead Time",
    "Random": "Random / Market",
}
COLLAPSED_COLORS: dict[str, str] = {
    "LoadFeatures": "#e8a33d",
    "LeadTime": "#3d8f83",
    "Random": "#3b6fb5",
}
_LOAD_FEATURE_PRIMARIES: tuple[str, ...] = ("ShipmentChange", "DifficultyOverride")
_LOAD_FEATURE_LABELS: dict[str, str] = {
    "ShipmentChange": "Shipment Change",
    "DifficultyOverride": "Difficulty Override",
}
VOLATILITY_ORDER: tuple[str, ...] = ("all", "volatile", "stable")
VOLATILITY_LABELS: dict[str, str] = {
    "all": "All shipments",
    "volatile": "Volatile (≥ threshold)",
    "stable": "Stable (< threshold)",
}
VOLATILITY_COLORS: dict[str, str] = {
    "all": "#1a2332",
    "volatile": "#c05b3c",
    "stable": "#3b6fb5",
}
ATTRIBUTION_ORDER: tuple[str, ...] = (
    "LoadFeatures",
    "LeadTimeClockOnly",
    "LeadTimeIndexCooccur",
    "Random",
)
ATTRIBUTION_LABELS: dict[str, str] = {
    "LoadFeatures": "Load Features",
    "LeadTimeClockOnly": "Lead Time (clocks only)",
    "LeadTimeIndexCooccur": "Lead Time + Index co-occur",
    "Random": "Random / Market (primary)",
}
ATTRIBUTION_COLORS: dict[str, str] = {
    "LoadFeatures": "#e8a33d",
    "LeadTimeClockOnly": "#3d8f83",
    "LeadTimeIndexCooccur": "#5ba4c9",
    "Random": "#3b6fb5",
}


def normalize_primary(col: str = "primary_category") -> pl.Expr:
    """Canonical primary label (LeadtimeChange, not legacy DeterministicChange)."""
    return pl.col(col).replace(LEGACY_LEADTIME_CATEGORY, "LeadtimeChange")


def collapse_category_expr(col: str = "primary_category") -> pl.Expr:
    """Map primary movement label → LoadFeatures / LeadTime / Random."""
    primary = normalize_primary(col)
    return (
        pl.when(primary.is_in(_LOAD_FEATURE_PRIMARIES))
        .then(pl.lit("LoadFeatures"))
        .when(primary == "LeadtimeChange")
        .then(pl.lit("LeadTime"))
        .otherwise(pl.lit("Random"))
    )


def with_collapsed_category(df: pl.DataFrame) -> pl.DataFrame:
    if "primary_category" not in df.columns:
        return df.with_columns(pl.lit("Random").alias("collapsed_category"))
    return df.with_columns(collapse_category_expr().alias("collapsed_category"))


def _round_row_stats(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("avg_shift_amt", "med_shift_amt", "p75_shift_amt", "p90_shift_amt", "mae_avail", "mae_book"):
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]), 2)
    for key in ("avg_shift_pct", "med_shift_pct", "p75_shift_pct", "p90_shift_pct"):
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]), 4)
    for key in ("pct_of_parent", "pct_of_category", "pct_of_problem", "pct_of_collapsed"):
        if key in out and out[key] is not None:
            out[key] = round(float(out[key]), 4)
    return out


def build_volatility_overview(labeled: pl.DataFrame) -> list[dict[str, Any]]:
    """Top-level All / Volatile / Stable shift stats for the parent cohort."""
    overview = segment_overview(labeled)
    if overview.is_empty():
        return []
    rename = {"parent": "all", "problem": "volatile", "stable": "stable"}
    rows: list[dict[str, Any]] = []
    for r in overview.iter_rows(named=True):
        key = rename.get(r["segment"], r["segment"])
        if key not in VOLATILITY_ORDER:
            continue
        rows.append(
            _round_row_stats(
                {
                    "segment": key,
                    "label": VOLATILITY_LABELS[key],
                    "n_loads": int(r["n_loads"]),
                    "pct_of_parent": float(r["pct_of_parent"]),
                    "avg_shift_amt": r.get("avg_shift_amt"),
                    "med_shift_amt": r.get("med_shift_amt"),
                    "p75_shift_amt": r.get("p75_shift_amt"),
                    "avg_shift_pct": r.get("avg_shift_pct"),
                    "med_shift_pct": r.get("med_shift_pct"),
                    "p75_shift_pct": r.get("p75_shift_pct"),
                }
            )
        )
    order = {k: i for i, k in enumerate(VOLATILITY_ORDER)}
    rows.sort(key=lambda r: order.get(r["segment"], 99))
    return rows


def build_collapsed_category_by_volatility(labeled: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Three-way movement split within All / Volatile / Stable segments."""
    framed = with_collapsed_category(labeled)
    out: dict[str, list[dict[str, Any]]] = {}
    for seg_key, filt in (
        ("all", pl.lit(True)),
        ("volatile", pl.col("is_tail")),
        ("stable", ~pl.col("is_tail")),
    ):
        sub = framed.filter(filt)
        if sub.is_empty():
            out[seg_key] = []
            continue
        n = sub.height
        stats = (
            sub.group_by("collapsed_category")
            .agg(
                pl.len().alias("n_loads"),
                pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
                pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            )
        )
        by_cat = {r["collapsed_category"]: r for r in stats.iter_rows(named=True)}
        rows: list[dict[str, Any]] = []
        for cat in COLLAPSED_ORDER:
            if cat not in by_cat:
                continue
            row = by_cat[cat]
            k = int(row["n_loads"])
            rows.append(
                {
                    "category": cat,
                    "label": COLLAPSED_LABELS[cat],
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
        out[seg_key] = rows
    return out


def with_attribution_category(df: pl.DataFrame) -> pl.DataFrame:
    """Four-way volatile-load attribution: split Lead Time by index co-occurrence."""
    framed = with_collapsed_category(df)
    if "index_change_ind" not in framed.columns:
        return framed.with_columns(
            pl.when(pl.col("collapsed_category") == "LeadTime")
            .then(pl.lit("LeadTimeClockOnly"))
            .otherwise(pl.col("collapsed_category"))
            .alias("attribution_category")
        )
    return framed.with_columns(
        pl.when(pl.col("collapsed_category") == "LoadFeatures")
        .then(pl.lit("LoadFeatures"))
        .when(pl.col("collapsed_category") == "Random")
        .then(pl.lit("Random"))
        .when(
            (pl.col("collapsed_category") == "LeadTime")
            & (pl.col("index_change_ind").fill_null(0) == 1)
        )
        .then(pl.lit("LeadTimeIndexCooccur"))
        .when(pl.col("collapsed_category") == "LeadTime")
        .then(pl.lit("LeadTimeClockOnly"))
        .otherwise(pl.lit("Random"))
        .alias("attribution_category")
    )


def _attribution_mae_by_category(
    problem: pl.DataFrame,
    *,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Mean absolute ETP p50 error vs realized carrier cost, by attribution slice."""
    from dqt.etp_slider.problem_loads.leadtime import (
        enrich_book_time_quotes,
        join_lc_carrier_cost,
    )
    from dqt.score.constants import COST_COL

    if problem.is_empty() or "etp50_avail" not in problem.columns:
        return {}

    frame = join_lc_carrier_cost(
        problem, features_path=features_path, data_dir=data_dir
    )
    if COST_COL not in frame.columns:
        return {}

    if hist_path is not None:
        frame = enrich_book_time_quotes(
            frame,
            hist_path=hist_path,
            survival_path=survival_path,
            cache_path=book_quotes_cache,
        )

    labeled = with_attribution_category(frame)
    valid_avail = (
        pl.col(COST_COL).is_finite()
        & pl.col("etp50_avail").is_finite()
        & (pl.col("etp50_avail") > 0)
    )
    mae_exprs = [
        pl.when(valid_avail)
        .then((pl.col("etp50_avail") - pl.col(COST_COL)).abs())
        .alias("_mae_avail"),
    ]
    agg_exprs = [pl.col("_mae_avail").mean().alias("mae_avail")]
    if "etp50_book" in labeled.columns:
        valid_book = (
            pl.col(COST_COL).is_finite()
            & pl.col("etp50_book").is_finite()
            & (pl.col("etp50_book") > 0)
        )
        mae_exprs.append(
            pl.when(valid_book)
            .then((pl.col("etp50_book") - pl.col(COST_COL)).abs())
            .alias("_mae_book")
        )
        agg_exprs.append(pl.col("_mae_book").mean().alias("mae_book"))
    labeled = labeled.with_columns(*mae_exprs)
    stats = labeled.group_by("attribution_category").agg(*agg_exprs)
    return {r["attribution_category"]: r for r in stats.iter_rows(named=True)}


def build_attribution_problem_breakdown(
    problem: pl.DataFrame,
    *,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Donut/table rows splitting Lead Time by index co-occurrence on volatile loads."""
    if problem.is_empty():
        return []
    labeled = with_attribution_category(problem)
    n = labeled.height
    stats = (
        labeled.group_by("attribution_category")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
        )
    )
    by_cat = {r["attribution_category"]: r for r in stats.iter_rows(named=True)}
    mae_by_cat = _attribution_mae_by_category(
        problem,
        data_dir=data_dir,
        features_path=features_path,
        hist_path=hist_path,
        survival_path=survival_path,
        book_quotes_cache=book_quotes_cache,
    )
    out: list[dict[str, Any]] = []
    for cat in ATTRIBUTION_ORDER:
        if cat not in by_cat:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        entry: dict[str, Any] = {
            "category": cat,
            "label": ATTRIBUTION_LABELS[cat],
            "n_loads": k,
            "pct": round(k / n, 4),
            "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
            if row["avg_shift_amt"] is not None
            else None,
            "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
            if row["avg_shift_pct"] is not None
            else None,
        }
        mae_row = mae_by_cat.get(cat)
        if mae_row:
            if mae_row.get("mae_avail") is not None:
                entry["mae_avail"] = round(float(mae_row["mae_avail"]), 2)
            if mae_row.get("mae_book") is not None:
                entry["mae_book"] = round(float(mae_row["mae_book"]), 2)
        out.append(entry)
    return out


def build_collapsed_problem_breakdown(problem: pl.DataFrame) -> list[dict[str, Any]]:
    """Donut/table rows for problem loads by collapsed category."""
    if problem.is_empty():
        return []
    labeled = with_collapsed_category(problem)
    n = labeled.height
    stats = (
        labeled.group_by("collapsed_category")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
        )
    )
    by_cat = {r["collapsed_category"]: r for r in stats.iter_rows(named=True)}
    out: list[dict[str, Any]] = []
    for cat in COLLAPSED_ORDER:
        if cat not in by_cat:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        out.append(
            {
                "category": cat,
                "label": COLLAPSED_LABELS[cat],
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


def build_collapsed_segment_overview(labeled: pl.DataFrame) -> pl.DataFrame:
    """Parent / problem / stable stats by collapsed category (cat_sum-style long table)."""
    if labeled.is_empty():
        return pl.DataFrame()
    framed = with_collapsed_category(labeled)
    n_parent = framed.height
    cat_sizes = {
        r["collapsed_category"]: int(r["n_category"])
        for r in framed.group_by("collapsed_category")
        .len()
        .rename({"len": "n_category"})
        .iter_rows(named=True)
    }
    rows: list[dict[str, Any]] = []
    for cat in COLLAPSED_ORDER:
        if cat not in cat_sizes:
            continue
        cat_df = framed.filter(pl.col("collapsed_category") == cat)
        n_cat = cat_sizes[cat]
        for segment, filt in (
            ("parent", pl.lit(True)),
            ("problem", pl.col("is_tail")),
            ("stable", ~pl.col("is_tail")),
        ):
            sub = cat_df.filter(filt)
            stats = shift_magnitude_stats(sub)
            n = int(stats.get("n_loads", 0))
            rows.append(
                _round_row_stats(
                    {
                        "segment": segment,
                        "collapsed_category": cat,
                        "category_label": COLLAPSED_LABELS[cat],
                        "n_loads": n,
                        "pct_of_parent": round(n / n_parent, 4) if n_parent else 0.0,
                        "pct_of_category": round(n / n_cat, 4) if n_cat else 0.0,
                        **{k: v for k, v in stats.items() if k != "n_loads"},
                    }
                )
            )
    return pl.DataFrame(rows)


def build_collapsed_segment_pivot(labeled: pl.DataFrame) -> list[dict[str, Any]]:
    """One row per collapsed category — parent / problem / stable side by side."""
    long = build_collapsed_segment_overview(labeled)
    if long.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for cat in COLLAPSED_ORDER:
        sub = long.filter(pl.col("collapsed_category") == cat)
        if sub.is_empty():
            continue
        label = COLLAPSED_LABELS[cat]
        row: dict[str, Any] = {
            "collapsed_category": cat,
            "category_label": label,
        }
        for segment in ("parent", "problem", "stable"):
            seg = sub.filter(pl.col("segment") == segment)
            if seg.is_empty():
                continue
            r = seg.row(0, named=True)
            row[f"n_{segment}"] = r["n_loads"]
            row[f"pct_parent_{segment}"] = r["pct_of_parent"]
            row[f"avg_amt_{segment}"] = round(float(r.get("avg_shift_amt") or 0), 2)
            row[f"avg_pct_{segment}"] = round(float(r.get("avg_shift_pct") or 0), 4)
            row[f"med_amt_{segment}"] = round(float(r.get("med_shift_amt") or 0), 2)
        n_cat = int(row.get("n_parent", 0))
        row["pct_category_problem"] = round(row.get("n_problem", 0) / n_cat, 4) if n_cat else 0.0
        rows.append(row)
    return rows


def _problem_subset(labeled: pl.DataFrame, collapsed: str) -> pl.DataFrame:
    return with_collapsed_category(labeled.filter(pl.col("is_tail"))).filter(
        pl.col("collapsed_category") == collapsed
    )


def _parent_subset(labeled: pl.DataFrame, collapsed: str) -> pl.DataFrame:
    return with_collapsed_category(labeled).filter(pl.col("collapsed_category") == collapsed)


def build_load_features_breakdown(labeled: pl.DataFrame) -> list[dict[str, Any]]:
    subset = _problem_subset(labeled, "LoadFeatures")
    if subset.is_empty() or "primary_category" not in subset.columns:
        return []
    n = subset.height
    parent_dir = shift_direction_by_group(
        _parent_subset(labeled, "LoadFeatures").with_columns(
            normalize_primary().alias("primary_category")
        ),
        "primary_category",
    )
    stats = (
        subset.with_columns(normalize_primary().alias("primary_category"))
        .group_by("primary_category")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
        )
    )
    by_cat = {r["primary_category"]: r for r in stats.iter_rows(named=True)}
    out: list[dict[str, Any]] = []
    for cat in _LOAD_FEATURE_PRIMARIES:
        if cat not in by_cat:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        direction = parent_dir.get(cat, {})
        out.append(
            {
                "subcategory": cat,
                "label": _LOAD_FEATURE_LABELS[cat],
                "n_loads": k,
                "pct_of_group": round(k / n, 4),
                "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
                if row["avg_shift_amt"] is not None
                else None,
                "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
                if row["avg_shift_pct"] is not None
                else None,
                "pct_shift_positive": direction.get("pct_shift_positive"),
                "pct_shift_negative": direction.get("pct_shift_negative"),
            }
        )
    return out


def build_random_breakdown(labeled: pl.DataFrame) -> list[dict[str, Any]]:
    """Sub-breakdown of Random problem loads: index-detected vs catch-all residual."""
    subset = _problem_subset(labeled, "Random")
    if subset.is_empty():
        return []
    n = subset.height
    parent_random = _parent_subset(labeled, "Random")
    if "index_change_ind" not in subset.columns:
        pos, neg = shift_direction_rates(parent_random)
        return [
            {
                "subcategory": "all",
                "label": "All Random / Market",
                "n_loads": n,
                "pct_of_group": 1.0,
                "avg_shift_amt": round(float(subset["etp50_shift_amt"].mean() or 0), 2),
                "avg_shift_pct": round(float(subset["etp50_shift_pct"].mean() or 0), 4),
                "pct_shift_positive": pos,
                "pct_shift_negative": neg,
            }
        ]
    parent_dir = shift_direction_by_group(parent_random, "index_change_ind")
    rows: list[dict[str, Any]] = []
    for ind, label in ((1, "Index drift detected"), (0, "Catch-all / no index trigger")):
        sub = subset.filter(pl.col("index_change_ind") == ind)
        if sub.is_empty():
            continue
        k = sub.height
        direction = parent_dir.get(ind, {})
        rows.append(
            {
                "subcategory": "index" if ind else "catch_all",
                "label": label,
                "n_loads": k,
                "pct_of_group": round(k / n, 4),
                "avg_shift_amt": round(float(sub["etp50_shift_amt"].mean() or 0), 2),
                "avg_shift_pct": round(float(sub["etp50_shift_pct"].mean() or 0), 4),
                "pct_shift_positive": direction.get("pct_shift_positive"),
                "pct_shift_negative": direction.get("pct_shift_negative"),
            }
        )
    return rows


def build_leadtime_secondary_breakdown(problem: pl.DataFrame) -> list[dict[str, Any]]:
    """Co-occurring signal rates on LeadTime problem loads."""
    subset = _problem_subset(problem, "LeadTime")
    if subset.is_empty():
        return []
    flags = secondary_flags_summary(subset)
    if flags.is_empty():
        return []
    return [
        {
            "signal": r["signal"],
            "label": r["signal"].replace("_", " ").title(),
            "n_loads": int(r["n_loads"]),
            "pct_of_group": float(r["pct_of_lc"]),
        }
        for r in flags.iter_rows(named=True)
    ]


def build_category_details(
    labeled: pl.DataFrame,
    *,
    data_dir: Path | str | None = None,
    davis_cache: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> dict[str, Any]:
    """Per collapsed category sub-breakdown tables."""
    problem = labeled.filter(pl.col("is_tail"))
    leadtime_rows = build_leadtime_archetype_breakdown(problem, labeled=labeled)
    if data_dir is not None and leadtime_rows:
        arch_pricing = leadtime_archetype_pricing_pivot(
            leadtime_pricing_accuracy_by_archetype(
                labeled,
                data_dir=data_dir,
                davis_cache=davis_cache,
                hist_path=hist_path,
                survival_path=survival_path,
                book_quotes_cache=book_quotes_cache,
                problem_only=True,
            )
        )
        if not arch_pricing.is_empty():
            p50 = {
                r["path_archetype"]: r
                for r in arch_pricing.filter(pl.col("quote") == "etp50").iter_rows(named=True)
            }
            enriched: list[dict[str, Any]] = []
            for row in leadtime_rows:
                merged = dict(row)
                stats = p50.get(row.get("archetype"))
                if stats:
                    for key in (
                        "n_create",
                        "att_create",
                        "gap_pp_create",
                        "mae_create",
                        "n_book",
                        "att_book",
                        "gap_pp_book",
                        "mae_book",
                    ):
                        val = stats.get(key)
                        if val is not None:
                            merged[key] = val
                enriched.append(merged)
            leadtime_rows = enriched

    return {
        "LoadFeatures": build_load_features_breakdown(labeled),
        "LeadTime": leadtime_rows,
        "LeadTime_confidence": build_leadtime_confidence_breakdown(
            labeled,
            data_dir=data_dir,
            davis_cache=davis_cache,
            hist_path=hist_path,
            survival_path=survival_path,
            book_quotes_cache=book_quotes_cache,
        ),
        "LeadTime_secondary": build_leadtime_secondary_breakdown(labeled),
        "Random": build_random_breakdown(labeled),
    }


def build_cat_report_payload(
    labeled: pl.DataFrame,
    *,
    avail_start: str | None = None,
    avail_end: str | None = None,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = None,
    threshold_mode: TailThresholdMode = "scaled",
    include_volatility_layer: bool = False,
    data_dir: Path | str | None = None,
    davis_cache: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
    per_load: pl.DataFrame | None = None,
    per_load_path: Path | str | None = None,
    universe_summary: dict[str, Any] | None = None,
    data_dir_for_universe: Path | str | None = None,
) -> dict[str, Any]:
    """JSON-serializable payload for the standalone 3-category HTML report."""
    problem = labeled.filter(pl.col("is_tail"))
    n_parent = labeled.height
    n_problem = problem.height
    rule = format_tail_threshold(
        pct_threshold=pct_threshold,
        amt_threshold=amt_threshold,
        mode=threshold_mode,
    )
    overview = shift_magnitude_stats(problem)
    lc_row = (
        build_collapsed_segment_overview(labeled)
        .filter(
            (pl.col("collapsed_category") == "LeadTime") & (pl.col("segment") == "problem")
        )
        .to_dicts()
    )
    lc_n = int(lc_row[0]["n_loads"]) if lc_row else 0
    lc_avg = float(lc_row[0].get("avg_shift_amt") or 0) if lc_row else 0.0

    problem_lc = problem.filter(
        pl.col("primary_category").is_in({"LeadtimeChange", LEGACY_LEADTIME_CATEGORY})
    )
    if "leadtime_isolated_ind" in problem_lc.columns:
        lc_hc_df = problem_lc.filter(pl.col("leadtime_isolated_ind") == 1)
    elif "index_change_ind" in problem_lc.columns:
        lc_hc_df = problem_lc.filter(pl.col("index_change_ind").fill_null(0) == 0)
    else:
        lc_hc_df = problem_lc
    lc_hc_n = lc_hc_df.height
    lc_hc_avg_amt = round(float(lc_hc_df["etp50_shift_amt"].mean() or 0), 2) if lc_hc_n else 0.0
    lc_hc_avg_pct = round(float(lc_hc_df["etp50_shift_pct"].mean() or 0), 4) if lc_hc_n else 0.0
    lc_hc_avg_etp = (
        round(float(lc_hc_df["etp50_avail"].mean() or 0), 2)
        if lc_hc_n and "etp50_avail" in lc_hc_df.columns
        else None
    )

    # Preserve four-way segment table for reference / QA
    four_way = segment_by_category_overview(labeled)

    stable = labeled.filter(~pl.col("is_tail"))
    stable_stats = shift_magnitude_stats(stable)

    meta: dict[str, Any] = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "cohort_rule": f"{lead_min:.0f}–{lead_max:.0f} day lead, avail {avail_start} → {avail_end}",
        "problem_rule": rule,
        "n_parent": n_parent,
        "n_problem": n_problem,
        "n_stable": int(stable.height),
        "pct_problem_of_parent": round(n_problem / n_parent, 4) if n_parent else 0.0,
        "pct_stable_of_parent": round(stable.height / n_parent, 4) if n_parent else 0.0,
        "problem_avg_shift_amt": round(float(overview.get("avg_shift_amt") or 0), 2),
        "problem_avg_shift_pct": round(float(overview.get("avg_shift_pct") or 0), 4),
        "stable_avg_shift_amt": round(float(stable_stats.get("avg_shift_amt") or 0), 2),
        "stable_avg_shift_pct": round(float(stable_stats.get("avg_shift_pct") or 0), 4),
        "leadtime_n": lc_n,
        "leadtime_avg_amt": round(lc_avg, 2),
        "leadtime_hc_n": lc_hc_n,
        "leadtime_hc_avg_amt": lc_hc_avg_amt,
        "leadtime_hc_avg_pct": lc_hc_avg_pct,
        "leadtime_hc_avg_etp50": lc_hc_avg_etp,
        "leadtime_hc_share_of_problem": round(lc_hc_n / n_problem, 4) if n_problem else 0.0,
        "collapsed_order": list(COLLAPSED_ORDER),
        "collapsed_labels": COLLAPSED_LABELS,
        "collapsed_colors": COLLAPSED_COLORS,
        "attribution_order": list(ATTRIBUTION_ORDER),
        "attribution_labels": ATTRIBUTION_LABELS,
        "attribution_colors": ATTRIBUTION_COLORS,
        "include_volatility_layer": include_volatility_layer,
    }
    if include_volatility_layer:
        meta["volatility_labels"] = VOLATILITY_LABELS
        meta["volatility_colors"] = VOLATILITY_COLORS

    payload: dict[str, Any] = {
        "meta": meta,
        "category_breakdown": build_attribution_problem_breakdown(
            problem,
            data_dir=data_dir,
            hist_path=hist_path,
            survival_path=survival_path,
            book_quotes_cache=book_quotes_cache,
        ),
        "category_breakdown_primary": build_collapsed_problem_breakdown(problem),
        "segment_by_category": build_collapsed_segment_overview(labeled).to_dicts(),
        "segment_pivot": build_collapsed_segment_pivot(labeled),
        "category_details": build_category_details(
            labeled,
            data_dir=data_dir,
            davis_cache=davis_cache,
            hist_path=hist_path,
            survival_path=survival_path,
            book_quotes_cache=book_quotes_cache,
        ),
        "four_way_segment_by_category": four_way.to_dicts(),
    }
    if include_volatility_layer:
        payload["volatility_overview"] = build_volatility_overview(labeled)
        payload["category_by_volatility"] = build_collapsed_category_by_volatility(labeled)

    if per_load is None and per_load_path is not None:
        per_load = pl.read_parquet(per_load_path)
    if per_load is not None:
        if universe_summary is None and data_dir_for_universe is not None:
            universe_summary = load_cohort_universe_summary(data_dir=data_dir_for_universe)
        payload["cohort_funnel"] = build_cohort_funnel(
            labeled,
            per_load,
            avail_start=avail_start,
            avail_end=avail_end,
            lead_min=lead_min,
            lead_max=lead_max,
            universe_summary=universe_summary,
        )
    return payload


def render_cat_report_html(payload: dict[str, Any]) -> str:
    """Render standalone HTML from :func:`build_cat_report_payload`."""
    template_path = Path(__file__).resolve().parents[1] / "drift" / "templates" / "etp_cat_report.html"
    html = template_path.read_text()
    data_json = json.dumps(payload, separators=(",", ":"))
    meta = payload["meta"]
    with_volatility = bool(meta.get("include_volatility_layer"))
    title = (
        "ETP Drift — Volatility & Movement Categories (3-way)"
        if with_volatility
        else "ETP Drift — Movement Categories (3-way)"
    )
    html = html.replace("__TITLE__", title)
    html = html.replace("__COHORT_RULE__", meta["cohort_rule"])
    html = html.replace("__PROBLEM_RULE__", meta["problem_rule"])
    html = html.replace("__DATA_JSON__", data_json)
    return html


def write_cat_report_html(
    labeled: pl.DataFrame,
    out_path: Path | str,
    *,
    include_volatility_layer: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build payload and write standalone HTML report."""
    payload = build_cat_report_payload(
        labeled, include_volatility_layer=include_volatility_layer, **kwargs
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_cat_report_html(payload))
    return payload


__all__ = [
    "ATTRIBUTION_COLORS",
    "ATTRIBUTION_LABELS",
    "ATTRIBUTION_ORDER",
    "COLLAPSED_COLORS",
    "COLLAPSED_LABELS",
    "COLLAPSED_ORDER",
    "VOLATILITY_COLORS",
    "VOLATILITY_LABELS",
    "VOLATILITY_ORDER",
    "build_attribution_problem_breakdown",
    "build_cat_report_payload",
    "build_category_details",
    "build_collapsed_category_by_volatility",
    "build_collapsed_problem_breakdown",
    "build_collapsed_segment_overview",
    "build_collapsed_segment_pivot",
    "build_load_features_breakdown",
    "build_random_breakdown",
    "build_volatility_overview",
    "collapse_category_expr",
    "render_cat_report_html",
    "with_attribution_category",
    "with_collapsed_category",
    "write_cat_report_html",
]

"""LeadtimeChange problem-load cohort — shipment-level ETP pricing analysis.

Builds on :mod:`dqt.etp_tail` exports for the since-2025 scaled-10% report cohort.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import polars as pl

from dqt.etp_slider.drift.report import (
    load_feature_snapshots,
    resolve_feature_snapshots_path,
)
from dqt.etp_slider.problem_loads.tail import (
    LEAD_MAX_DAYS_DEFAULT,
    LEAD_MIN_DAYS_DEFAULT,
    build_tail_dataset,
)
from dqt.etp_slider.sql import HISTORY_CACHE, PER_LOAD_CACHE, SURVIVAL_PER_LOAD_CACHE
from dqt.score.constants import COST_COL, ID_COL
from dqt.score.metrics import attainment, gap_pp, mae_usd, mean_error_usd

LC_CATEGORY = "LeadtimeChange"
DEFAULT_AVAIL_START = "2025-01-01"
DEFAULT_AVAIL_END = "2026-08-28"
DEFAULT_DAVIS_SUFFIX = "2025-01-01_2026-08-28"
PATH_MARKS: tuple[int, ...] = (168, 96, 72, 48, 24)
LEADTIME_ARCHETYPE_ORDER: tuple[str, ...] = (
    "late_cliff",
    "mid_plateau",
    "steady",
    "early_burst",
    "unknown",
)
LEADTIME_ARCHETYPE_LABELS: dict[str, str] = {
    "late_cliff": "Late Cliff",
    "mid_plateau": "Mid Plateau",
    "steady": "Steady Climb",
    "early_burst": "Early Burst",
    "unknown": "Unknown",
}

MDE_ENRICHMENT_SQL = (
    Path(__file__).resolve().parents[2] / "SQL" / "etp-slider" / "lc-cohort-mde-enrichment.sql"
)


def haul_band_expr(col: str = "loaded_miles") -> pl.Expr:
    miles = pl.col(col)
    return (
        pl.when(miles < 250)
        .then(pl.lit("Short (<250mi)"))
        .when(miles < 600)
        .then(pl.lit("Mid (250-600mi)"))
        .otherwise(pl.lit("Long (600mi+)"))
    )


def build_lc_cohort(
    *,
    data_dir: Path | str,
    avail_start: str = DEFAULT_AVAIL_START,
    avail_end: str = DEFAULT_AVAIL_END,
    davis_cache: Path | str | None = None,
    pct_threshold: float = 0.10,
    threshold_mode: Literal["and", "or", "scaled"] = "scaled",
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return (full tail, LeadtimeChange subset) for the report-aligned cohort."""
    cache = Path(data_dir) / "etp"
    if davis_cache is None:
        davis_cache = cache / f"analysis-davis-raw-{DEFAULT_DAVIS_SUFFIX}.parquet"
    davis_cache = Path(davis_cache)

    tail = build_tail_dataset(
        per_load_path=cache / PER_LOAD_CACHE,
        hist_path=cache / HISTORY_CACHE,
        davis_path=davis_cache if davis_cache.exists() else None,
        pct_threshold=pct_threshold,
        amt_threshold=None,
        threshold_mode=threshold_mode,
        lead_min=LEAD_MIN_DAYS_DEFAULT,
        lead_max=LEAD_MAX_DAYS_DEFAULT,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    lc = tail.filter(pl.col("primary_category") == LC_CATEGORY)
    return tail, lc


def add_path_checkpoints(
    lc: pl.DataFrame,
    *,
    hist_path: Path | str,
) -> pl.DataFrame:
    """Join indexed ETP50 at 168/96/72/48/24h for path archetyping."""
    from dqt.etp_slider.drift.report import _indexed_at_marks

    hist = pl.read_parquet(hist_path)
    ids = lc.select("loadnumber")
    indexed = _indexed_at_marks(hist, ids)
    etp50 = indexed["ETP50"].filter(pl.col("mark_hrs").is_in(PATH_MARKS))
    wide = etp50.pivot(on="mark_hrs", index="loadnumber", values="idx")
    rename = {str(m): f"etp50_idx_{m}h" for m in PATH_MARKS if str(m) in wide.columns}
    wide = wide.rename(rename)
    return lc.join(wide, on="loadnumber", how="left")


def enrich_path_archetypes_from_indexed(
    loads: pl.DataFrame,
    indexed: dict[str, pl.DataFrame],
) -> pl.DataFrame:
    """Join checkpoint ``etp50_idx_*`` from indexed series and classify path shape."""
    if loads.is_empty() or "ETP50" not in indexed:
        return loads
    etp50 = indexed["ETP50"].filter(pl.col("mark_hrs").is_in(PATH_MARKS))
    if etp50.is_empty():
        return loads
    wide = etp50.pivot(on="mark_hrs", index="loadnumber", values="idx")
    rename = {str(m): f"etp50_idx_{m}h" for m in PATH_MARKS if str(m) in wide.columns}
    wide = wide.rename(rename)
    return classify_path_archetypes(loads.join(wide, on="loadnumber", how="left"))


def classify_path_archetypes(lc: pl.DataFrame) -> pl.DataFrame:
    """Label checkpoint climb shapes (early_burst, mid_plateau, late_cliff, steady)."""
    idx_cols = [f"etp50_idx_{m}h" for m in PATH_MARKS]
    present = [c for c in idx_cols if c in lc.columns]
    if len(present) < 2:
        return lc.with_columns(pl.lit("unknown").alias("path_archetype"))

    base = lc.with_columns(pl.col("etp50_idx_168h").fill_null(100.0).alias("_i168"))
    chain: list[tuple[int, str, str]] = (
        (96, "etp50_idx_96h", "_i96"),
        (72, "etp50_idx_72h", "_i72"),
        (48, "etp50_idx_48h", "_i48"),
    )
    prev_alias = "_i168"
    for _mark, idx_col, out_alias in chain:
        if idx_col in lc.columns:
            base = base.with_columns(pl.col(idx_col).fill_null(pl.col(prev_alias)).alias(out_alias))
        else:
            base = base.with_columns(pl.col(prev_alias).alias(out_alias))
        prev_alias = out_alias
    base = base.with_columns(
        (pl.col("_i96") - pl.col("_i168")).alias("_d_168_96"),
        (pl.col("_i72") - pl.col("_i96")).alias("_d_96_72"),
        (pl.col("_i48") - pl.col("_i72")).alias("_d_72_48"),
        (pl.col("_i72") - pl.col("_i168")).alias("_d_168_72"),
    )

    archetype = (
        pl.when((pl.col("_d_168_96") >= 15) & (pl.col("_d_96_72").abs() <= 8))
        .then(pl.lit("early_burst"))
        .when((pl.col("_d_168_96").abs() <= 5) & (pl.col("_d_96_72") >= 15))
        .then(pl.lit("late_cliff"))
        .when((pl.col("_d_72_48") >= 10) | (pl.col("_d_96_72") >= 10))
        .then(pl.lit("late_cliff"))
        .when(pl.col("_d_168_96").abs() <= 8)
        .then(pl.lit("mid_plateau"))
        .otherwise(pl.lit("steady"))
    )
    return base.with_columns(archetype.alias("path_archetype")).drop(
        "_i168", "_i96", "_i72", "_i48",
        "_d_168_96", "_d_96_72", "_d_72_48", "_d_168_72",
    )


def archetype_segment_summary(
    lc: pl.DataFrame,
    *,
    pct_denominator: int | None = None,
) -> pl.DataFrame:
    """Aggregate ETP50 shift stats by ``path_archetype``."""
    if "path_archetype" not in lc.columns or lc.is_empty():
        return pl.DataFrame()
    n = pct_denominator if pct_denominator is not None else lc.height
    return (
        lc.group_by("path_archetype")
        .agg(
            pl.len().alias("n_loads"),
            (pl.len() / n).alias("pct_of_lc"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            (pl.col("etp50_shift_amt") > 0).mean().alias("pct_shift_positive"),
            (pl.col("etp50_shift_amt") < 0).mean().alias("pct_shift_negative"),
        )
        .sort("n_loads", descending=True)
    )


def _path_archetype_mae(
    lc: pl.DataFrame,
    *,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> dict[str, dict[str, Any]]:
    """Mean absolute ETP p50 error vs realized carrier cost, by path archetype."""
    from dqt.score.constants import COST_COL

    if lc.is_empty() or "path_archetype" not in lc.columns or "etp50_avail" not in lc.columns:
        return {}

    frame = join_lc_carrier_cost(lc, features_path=features_path, data_dir=data_dir)
    if COST_COL not in frame.columns:
        return {}

    if hist_path is not None:
        frame = enrich_book_time_quotes(
            frame,
            hist_path=hist_path,
            survival_path=survival_path,
            cache_path=book_quotes_cache,
        )

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
    if "etp50_book" in frame.columns:
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
    frame = frame.with_columns(*mae_exprs)
    stats = frame.group_by("path_archetype").agg(*agg_exprs)
    return {r["path_archetype"]: r for r in stats.iter_rows(named=True)}


def build_leadtime_archetype_breakdown(
    tail: pl.DataFrame,
    *,
    labeled: pl.DataFrame | None = None,
    data_dir: Path | str | None = None,
    features_path: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Sub-breakdown of LeadtimeChange problem loads by checkpoint climb shape."""
    from dqt.etp_slider.movement.orders import LEGACY_LEADTIME_CATEGORY

    if tail.is_empty() or "primary_category" not in tail.columns:
        return []
    lc = tail.filter(
        pl.col("primary_category").replace(LEGACY_LEADTIME_CATEGORY, LC_CATEGORY) == LC_CATEGORY
    )
    if lc.is_empty():
        return []
    lc = classify_path_archetypes(lc)
    stats = archetype_segment_summary(lc)
    mae_by_arch: dict[str, dict[str, Any]] = {}
    if data_dir is not None or hist_path is not None:
        mae_by_arch = _path_archetype_mae(
            lc,
            data_dir=data_dir,
            features_path=features_path,
            hist_path=hist_path,
            survival_path=survival_path,
            book_quotes_cache=book_quotes_cache,
        )
    from dqt.etp_slider.problem_loads.shift_direction import shift_direction_by_group

    arch_dir = shift_direction_by_group(lc, "path_archetype")
    by_arch = {r["path_archetype"]: r for r in stats.iter_rows(named=True)}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for arch in LEADTIME_ARCHETYPE_ORDER:
        if arch not in by_arch:
            continue
        row = by_arch[arch]
        k = int(row["n_loads"])
        direction = arch_dir.get(arch, {})
        entry: dict[str, Any] = {
            "archetype": arch,
            "label": LEADTIME_ARCHETYPE_LABELS.get(arch, arch),
            "n_loads": k,
            "pct_of_lc": round(float(row["pct_of_lc"]), 4),
            "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
            if row["avg_shift_amt"] is not None
            else None,
            "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
            if row["avg_shift_pct"] is not None
            else None,
            "pct_shift_positive": direction.get("pct_shift_positive"),
            "pct_shift_negative": direction.get("pct_shift_negative"),
        }
        mae_row = mae_by_arch.get(arch)
        if mae_row:
            if mae_row.get("mae_avail") is not None:
                entry["mae_avail"] = round(float(mae_row["mae_avail"]), 2)
            if mae_row.get("mae_book") is not None:
                entry["mae_book"] = round(float(mae_row["mae_book"]), 2)
        out.append(entry)
        seen.add(arch)
    for arch in sorted(by_arch):
        if arch in seen:
            continue
        row = by_arch[arch]
        k = int(row["n_loads"])
        direction = arch_dir.get(arch, {})
        entry = {
            "archetype": arch,
            "label": LEADTIME_ARCHETYPE_LABELS.get(arch, arch),
            "n_loads": k,
            "pct_of_lc": round(float(row["pct_of_lc"]), 4),
            "avg_shift_amt": round(float(row["avg_shift_amt"]), 2)
            if row["avg_shift_amt"] is not None
            else None,
            "avg_shift_pct": round(float(row["avg_shift_pct"]), 4)
            if row["avg_shift_pct"] is not None
            else None,
            "pct_shift_positive": direction.get("pct_shift_positive"),
            "pct_shift_negative": direction.get("pct_shift_negative"),
        }
        mae_row = mae_by_arch.get(arch)
        if mae_row:
            if mae_row.get("mae_avail") is not None:
                entry["mae_avail"] = round(float(mae_row["mae_avail"]), 2)
            if mae_row.get("mae_book") is not None:
                entry["mae_book"] = round(float(mae_row["mae_book"]), 2)
        out.append(entry)
    return out


def detect_target_pullbacks(
    lc: pl.DataFrame,
    *,
    hist_path: Path | str,
) -> pl.DataFrame:
    """Per load: any hour where audit target3 dropped while model etp50 rose."""
    ids = set(lc["loadnumber"].to_list())
    hist = (
        pl.read_parquet(hist_path)
        .filter(pl.col("loadnumber").is_in(ids))
        .sort(["loadnumber", "hours_before_pickup"], descending=True)
    )
    model = hist.filter(pl.col("source") == "model").select(
        "loadnumber",
        "hours_before_pickup",
        pl.col("etp50").alias("model_etp50"),
    )
    audit = hist.filter(pl.col("source") == "audit").select(
        "loadnumber",
        "hours_before_pickup",
        pl.col("target3").alias("audit_t3"),
    )
    merged = model.join(audit, on=["loadnumber", "hours_before_pickup"], how="inner").sort(
        ["loadnumber", "hours_before_pickup"], descending=True
    )
    if merged.is_empty():
        return lc.with_columns(
            pl.lit(0).alias("target_pullback_ind"),
            pl.lit(None).cast(pl.Float64).alias("max_model_minus_t3"),
        )

    merged = merged.with_columns(
        pl.col("model_etp50").diff().over("loadnumber").alias("etp50_delta"),
        pl.col("audit_t3").diff().over("loadnumber").alias("t3_delta"),
    )
    pullbacks = (
        merged.filter((pl.col("etp50_delta") > 0) & (pl.col("t3_delta") < -5))
        .group_by("loadnumber")
        .len()
        .rename({"len": "n_target_pullbacks"})
    )
    gaps = merged.with_columns(
        (pl.col("model_etp50") - pl.col("audit_t3")).alias("gap")
    ).group_by("loadnumber").agg(
        pl.col("gap").max().alias("max_model_minus_t3"),
        pl.col("gap").mean().alias("avg_model_minus_t3"),
    )
    out = lc.join(pullbacks, on="loadnumber", how="left").join(gaps, on="loadnumber", how="left")
    return out.with_columns(
        pl.col("n_target_pullbacks").fill_null(0).cast(pl.Int32),
        (pl.col("n_target_pullbacks") > 0).cast(pl.Int8).alias("target_pullback_ind"),
    )


def secondary_flags_summary(lc: pl.DataFrame) -> pl.DataFrame:
    """Rates of non-primary signals on LeadtimeChange loads."""
    flags = [
        ("charge_inc_ind", "charge"),
        ("clhp_change_ind", "clhp"),
        ("equip_change_ind", "equip"),
        ("hard_ft_inc_ind", "hard_ft"),
        ("index_change_ind", "index"),
    ]
    rows: list[dict[str, Any]] = []
    n = lc.height
    for col, label in flags:
        if col not in lc.columns:
            continue
        k = int(lc.filter(pl.col(col) == 1).height)
        rows.append({"signal": label, "n_loads": k, "pct_of_lc": round(k / n, 4) if n else 0.0})
    clock_only = int(lc.filter(pl.col("clock_only_ind") == 1).height) if "clock_only_ind" in lc.columns else 0
    rows.append({"signal": "clock_only", "n_loads": clock_only, "pct_of_lc": round(clock_only / n, 4) if n else 0.0})
    return pl.DataFrame(rows)


def _index_component_fired_expr(delta_col: str, avail_col: str | None) -> pl.Expr:
    from dqt.etp_slider.movement.orders import index_delta_fired_expr

    if not avail_col:
        return pl.lit(False)
    return index_delta_fired_expr(delta_col, avail_col)


def index_secondary_impact(lc: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Quantify co-occurring market-index drift on LeadtimeChange loads.

    Index flags are secondary — clocks win the primary label — but ``index_change_ind``
    fires on most long-board loads when lag7 CPM / fuel move ≥ threshold during the
    avail→48hr window. Compares shift size with vs without index drift and reports
    per-component trigger rates and correlations (descriptive, not causal OLS).
    """
    from dqt.etp_slider.movement.orders import INDEX_DELTA_SPECS

    n = lc.height
    if n == 0 or "index_change_ind" not in lc.columns:
        return {}

    by_flag = (
        lc.group_by("index_change_ind")
        .agg(
            pl.len().alias("n_loads"),
            (pl.len() / n).alias("pct_of_lc"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            pl.col("etp50_shift_amt").median().alias("med_shift_amt"),
        )
        .with_columns(
            pl.when(pl.col("index_change_ind") == 1)
            .then(pl.lit("index_co_occur"))
            .otherwise(pl.lit("no_index_drift"))
            .alias("segment")
        )
        .sort("index_change_ind")
    )

    component_rows: list[dict[str, Any]] = []
    fired_cols: list[str] = []
    work = lc
    for delta_col, avail_col in INDEX_DELTA_SPECS:
        if delta_col not in work.columns:
            continue
        avail = avail_col if avail_col and avail_col in work.columns else None
        fired_name = f"{delta_col}_fired"
        work = work.with_columns(_index_component_fired_expr(delta_col, avail).alias(fired_name))
        fired_cols.append(fired_name)
        fired = work.filter(pl.col(fired_name))
        not_fired = work.filter(~pl.col(fired_name))
        component_rows.append(
            {
                "index_component": delta_col.replace("_delta", ""),
                "n_fired": fired.height,
                "pct_of_lc": round(fired.height / n, 4),
                "avg_shift_amt_fired": float(fired["etp50_shift_amt"].mean() or 0) if fired.height else None,
                "avg_shift_amt_not_fired": float(not_fired["etp50_shift_amt"].mean() or 0)
                if not_fired.height
                else None,
                "lift_amt": float((fired["etp50_shift_amt"].mean() or 0) - (not_fired["etp50_shift_amt"].mean() or 0))
                if fired.height and not_fired.height
                else None,
            }
        )

    by_component = pl.DataFrame(component_rows)

    corr_rows: list[dict[str, Any]] = []
    for delta_col, _ in INDEX_DELTA_SPECS:
        if delta_col not in lc.columns:
            continue
        filled = pl.col(delta_col).fill_null(0)
        corr_rows.append(
            {
                "feature": delta_col,
                "corr_shift_amt": float(lc.select(pl.corr(filled, pl.col("etp50_shift_amt"))).item() or 0),
                "corr_shift_pct": float(lc.select(pl.corr(filled, pl.col("etp50_shift_pct"))).item() or 0),
            }
        )
    if "book_2_pkup_delta" in lc.columns:
        filled = pl.col("book_2_pkup_delta").fill_null(0)
        corr_rows.append(
            {
                "feature": "book_2_pkup_delta",
                "corr_shift_amt": float(lc.select(pl.corr(filled, pl.col("etp50_shift_amt"))).item() or 0),
                "corr_shift_pct": float(lc.select(pl.corr(filled, pl.col("etp50_shift_pct"))).item() or 0),
            }
        )
    correlations = pl.DataFrame(corr_rows)

    no_idx = lc.filter(pl.col("index_change_ind") == 0)
    yes_idx = lc.filter(pl.col("index_change_ind") == 1)
    idx_rate = yes_idx.height / n
    stable_shift = float(no_idx["etp50_shift_amt"].mean() or 0) if no_idx.height else None
    index_shift = float(yes_idx["etp50_shift_amt"].mean() or 0) if yes_idx.height else None
    lift = (index_shift - stable_shift) if stable_shift is not None and index_shift is not None else None
    lift_summary = pl.DataFrame(
        [
            {
                "index_event_rate": round(idx_rate, 4),
                "avg_shift_no_index": stable_shift,
                "avg_shift_with_index": index_shift,
                "lift_amt_index_vs_no": lift,
                "implied_pooled_lift": lift * idx_rate if lift is not None else None,
                "n_no_index": no_idx.height,
                "n_with_index": yes_idx.height,
            }
        ]
    )

    by_mix = pl.DataFrame()
    if fired_cols:
        short = {
            "dat_rate_delta_fired": "dat",
            "fuel_cost_delta_fired": "fuel",
            "lag7_cpm_delta_fired": "lag7",
        }
        mix_label = (
            pl.concat_str(
                [
                    pl.when(pl.col(c)).then(pl.lit(short.get(c, c))).otherwise(pl.lit(None))
                    for c in fired_cols
                ],
                separator="+",
                ignore_nulls=True,
            )
            .fill_null("(none)")
            .alias("index_mix")
        )
        by_mix = (
            work.with_columns(mix_label)
            .with_columns(
                pl.when(pl.col("index_mix").str.len_chars() == 0)
                .then(pl.lit("(none)"))
                .otherwise(pl.col("index_mix"))
                .alias("index_mix")
            )
            .group_by("index_mix")
            .agg(
                pl.len().alias("n_loads"),
                (pl.len() / n).alias("pct_of_lc"),
                pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
            )
            .sort("n_loads", descending=True)
        )

    return {
        "by_index_flag": by_flag,
        "by_index_component": by_component,
        "correlations": correlations,
        "lift_summary": lift_summary,
        "by_component_mix": by_mix,
    }


def profile_lc_cohort(lc: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Segmentation tables for ops leadership."""
    n = lc.height
    lc = lc.with_columns(
        haul_band_expr().alias("haul_band"),
        pl.col("made_available_utc").dt.strftime("%Y-%m").alias("avail_month"),
    )

    overview = pl.DataFrame(
        {
            "metric": [
                "n_loads",
                "avg_shift_pct",
                "med_shift_pct",
                "avg_shift_amt",
                "med_shift_amt",
                "avg_etp50_avail",
                "pct_clock_only",
            ],
            "value": [
                float(n),
                float(lc["etp50_shift_pct"].mean() or 0),
                float(lc["etp50_shift_pct"].median() or 0),
                float(lc["etp50_shift_amt"].mean() or 0),
                float(lc["etp50_shift_amt"].median() or 0),
                float(lc["etp50_avail"].mean() or 0) if "etp50_avail" in lc.columns else None,
                float(lc["clock_only_ind"].mean() or 0) if "clock_only_ind" in lc.columns else None,
            ],
        }
    )

    by_haul = (
        lc.group_by("haul_band")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
        )
        .sort("haul_band")
    )
    by_hyper = (
        lc.group_by("is_hyperlocal")
        .agg(
            pl.len().alias("n_loads"),
            pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
        )
        .sort("is_hyperlocal")
        if "is_hyperlocal" in lc.columns
        else pl.DataFrame()
    )
    by_month = (
        lc.group_by("avail_month")
        .agg(pl.len().alias("n_loads"), pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"))
        .sort("avail_month")
    )
    by_archetype = (
        archetype_segment_summary(lc, pct_denominator=n)
        if "path_archetype" in lc.columns
        else pl.DataFrame()
    )
    by_pullback = (
        lc.group_by("target_pullback_ind")
        .agg(pl.len().alias("n_loads"), pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"))
        .sort("target_pullback_ind")
        if "target_pullback_ind" in lc.columns
        else pl.DataFrame()
    )
    return {
        "overview": overview,
        "by_haul_band": by_haul,
        "by_hyperlocal": by_hyper,
        "by_avail_month": by_month,
        "by_path_archetype": by_archetype,
        "by_target_pullback": by_pullback,
        "secondary_flags": secondary_flags_summary(lc),
    }


def build_lc_exemplars(lc: pl.DataFrame, *, n_each: int = 10) -> pl.DataFrame:
    """Stratified exemplar list for ops QA."""
    picks: list[pl.DataFrame] = []
    base_cols = [
        c
        for c in (
            "loadnumber",
            "path_archetype",
            "etp50_shift_pct",
            "etp50_shift_amt",
            "etp50_avail",
            "etp50_48hr",
            "clock_only_ind",
            "target_pullback_ind",
            "book_2_pkup_delta",
            "haul_band",
        )
        if c in lc.columns
    ]

    def _tag(df: pl.DataFrame, reason: str) -> pl.DataFrame:
        return df.with_columns(pl.lit(reason).alias("pick_reason")).select(base_cols + ["pick_reason"])

    picks.append(_tag(lc.sort("etp50_shift_pct", descending=True).head(n_each), "top_shift_pct"))
    if "clock_only_ind" in lc.columns:
        picks.append(
            _tag(
                lc.filter(pl.col("clock_only_ind") == 1)
                .sort("etp50_shift_pct", descending=True)
                .head(n_each),
                "clock_only",
            )
        )
    if "book_2_pkup_delta" in lc.columns:
        picks.append(
            _tag(
                lc.sort(pl.col("book_2_pkup_delta").abs(), descending=True).head(n_each),
                "large_clock_delta",
            )
        )
    if "target_pullback_ind" in lc.columns:
        picks.append(
            _tag(
                lc.filter(pl.col("target_pullback_ind") == 1)
                .sort("etp50_shift_pct", descending=True)
                .head(n_each),
                "target_pullback",
            )
        )
    if "path_archetype" in lc.columns:
        for arch in ("late_cliff", "early_burst"):
            sub = lc.filter(pl.col("path_archetype") == arch).sort(
                "etp50_shift_pct", descending=True
            ).head(n_each)
            if sub.height:
                picks.append(_tag(sub, arch))

    if not picks:
        return pl.DataFrame()
    out = pl.concat(picks, how="diagonal").unique("loadnumber", keep="first")
    return out.sort("etp50_shift_pct", descending=True)


LC_PRICING_QUOTES: tuple[tuple[str, float, str], ...] = (
    ("etp50", 0.50, "ETP p50"),
    ("target2", 0.25, "Target 2 (t25)"),
    ("target4", 0.75, "Target 4 (t75)"),
)
LC_PRICING_TIMINGS: tuple[tuple[str, str], ...] = (
    ("available", "Load creation (available)"),
    ("48hr", "48h pre-pickup"),
)
_DAVIS_QUOTE_COLS = (
    "target2_avail",
    "target2_48hr",
    "target4_avail",
    "target4_48hr",
)


def enrich_lc_davis_quotes(
    lc: pl.DataFrame,
    davis_cache: Path | str | None = None,
) -> pl.DataFrame:
    """Join slider target2/4 at available and 48h when missing from LC frame."""
    missing = [c for c in _DAVIS_QUOTE_COLS if c not in lc.columns]
    if not missing or davis_cache is None:
        return lc
    davis_cache = Path(davis_cache)
    if not davis_cache.exists():
        return lc
    davis = pl.read_parquet(davis_cache, columns=["loadnumber", *missing])
    return lc.drop([c for c in missing if c in lc.columns], strict=False).join(
        davis, on="loadnumber", how="left"
    )


def join_lc_carrier_cost(
    lc: pl.DataFrame,
    *,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> pl.DataFrame:
    """Attach realized carrier cost from features.parquet."""
    if COST_COL in lc.columns:
        return lc
    from dqt import resolve_data_dir

    if features_path is None:
        features_path = resolve_data_dir(data_dir) / "features.parquet"
    features_path = Path(features_path)
    if not features_path.exists():
        return lc
    costs = (
        pl.scan_parquet(features_path)
        .select(pl.col(ID_COL).alias("loadnumber"), pl.col(COST_COL))
        .collect()
    )
    return lc.join(costs, on="loadnumber", how="left")


def lc_pricing_accuracy_comparison(
    lc: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> pl.DataFrame:
    """Pricing accuracy for LC-isolated loads: available vs 48h pre-pickup.

    Scores ETP p50 and slider targets t25/t75 (target2/target4) against realized
    carrier cost. Nominal quantiles (0.50 / 0.25 / 0.75) drive attainment gap_pp.
    """
    frame = join_lc_carrier_cost(lc, features_path=features_path, data_dir=data_dir)
    frame = enrich_lc_davis_quotes(frame, davis_cache)
    if COST_COL not in frame.columns:
        return pl.DataFrame()

    rows: list[dict[str, Any]] = []
    for timing_key, timing_label in LC_PRICING_TIMINGS:
        suffix = "_avail" if timing_key == "available" else "_48hr"
        for quote_prefix, nominal, quote_label in LC_PRICING_QUOTES:
            col = f"{quote_prefix}{suffix}"
            if col not in frame.columns:
                continue
            sub = frame.filter(
                pl.col(COST_COL).is_finite() & pl.col(col).is_finite() & (pl.col(col) > 0)
            )
            n = sub.height
            if n == 0:
                continue
            actual = sub[COST_COL].to_numpy()
            predicted = sub[col].to_numpy()
            att = attainment(predicted, actual)
            rows.append(
                {
                    "timing": timing_key,
                    "timing_label": timing_label,
                    "quote": quote_prefix,
                    "quote_label": quote_label,
                    "nominal_quantile": nominal,
                    "n_loads": n,
                    "attainment": round(att, 4),
                    "gap_pp": round(gap_pp(att, nominal), 2),
                    "mae_usd": round(mae_usd(predicted, actual), 2),
                    "bias_usd": round(mean_error_usd(predicted, actual), 2),
                    "mean_actual_usd": round(float(actual.mean()), 2),
                    "mean_quote_usd": round(float(predicted.mean()), 2),
                }
            )
    return pl.DataFrame(rows)


def lc_pricing_accuracy_pivot(long: pl.DataFrame) -> pl.DataFrame:
    """Wide view — one row per quote, available vs 48h attainment side by side."""
    if long.is_empty():
        return long

    rows: list[dict[str, Any]] = []
    for quote in long["quote"].unique().to_list():
        sub = long.filter(pl.col("quote") == quote)
        label = sub["quote_label"][0]
        nominal = float(sub["nominal_quantile"][0])
        row: dict[str, Any] = {
            "quote": quote,
            "quote_label": label,
            "nominal_quantile": nominal,
        }
        for timing in ("available", "48hr"):
            seg = sub.filter(pl.col("timing") == timing)
            if seg.is_empty():
                continue
            r = seg.row(0, named=True)
            row[f"n_{timing}"] = r["n_loads"]
            row[f"att_{timing}"] = r["attainment"]
            row[f"gap_pp_{timing}"] = r["gap_pp"]
            row[f"mae_{timing}"] = r["mae_usd"]
            row[f"bias_{timing}"] = r["bias_usd"]
        rows.append(row)

    order = {q[0]: i for i, q in enumerate(LC_PRICING_QUOTES)}
    rows.sort(key=lambda r: order.get(r["quote"], 99))
    return pl.DataFrame(rows)


ETP_VOLATILITY_SEGMENTS: tuple[tuple[str, str, pl.Expr], ...] = (
    ("volatile", "Volatile ETP (≥10% avail→48h)", pl.col("is_tail")),
    ("stable", "Stable ETP (<10% avail→48h)", ~pl.col("is_tail")),
)

# Mutually exclusive LC problem-load confidence tiers (partition clock-driven loads).
LC_CONFIDENCE_SEGMENTS: tuple[tuple[str, str, pl.Expr], ...] = (
    (
        "high_confidence",
        "High confidence (clock-only, no index drift)",
        pl.col("leadtime_isolated_ind") == 1,
    ),
    (
        "index_co_occur",
        "Index co-occur (≥10% DAT/fuel/lag7 + clocks)",
        pl.col("index_clock_overlap_ind") == 1,
    ),
)

CREATE_BOOK_QUOTES: tuple[tuple[str, float, str, str], ...] = (
    ("etp50", 0.50, "ETP p50", "etp50"),
    ("target2", 0.25, "Target 2 (t25)", "target2"),
    ("target4", 0.75, "Target 4 (t75)", "target4"),
)
CREATE_BOOK_TIMINGS: tuple[tuple[str, str, str, tuple], ...] = (
    (
        "create",
        "Load creation (available)",
        "_avail",
        CREATE_BOOK_QUOTES,
    ),
    (
        "book",
        "At book",
        "_book",
        (
            ("etp50", 0.50, "ETP p50", "etp50"),
            ("target1", 0.25, "Target 1 @ book", "target1"),
            ("target3", 0.75, "Target 3 @ book", "target3"),
        ),
    ),
)


def _score_pricing_slice(
    sub: pl.DataFrame,
    quote_col: str,
    *,
    timing: str,
    timing_label: str,
    quote: str,
    quote_label: str,
    nominal: float,
    segment: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if quote_col not in sub.columns or COST_COL not in sub.columns:
        return None
    scored = sub.filter(
        pl.col(COST_COL).is_finite() & pl.col(quote_col).is_finite() & (pl.col(quote_col) > 0)
    )
    n = scored.height
    if n == 0:
        return None
    actual = scored[COST_COL].to_numpy()
    predicted = scored[quote_col].to_numpy()
    att = attainment(predicted, actual)
    row: dict[str, Any] = {
        "movement_category": LC_CATEGORY,
        "timing": timing,
        "timing_label": timing_label,
        "quote": quote,
        "quote_label": quote_label,
        "nominal_quantile": nominal,
        "n_loads": n,
        "attainment": round(att, 4),
        "gap_pp": round(gap_pp(att, nominal), 2),
        "mae_usd": round(mae_usd(predicted, actual), 2),
        "bias_usd": round(mean_error_usd(predicted, actual), 2),
        "mean_actual_usd": round(float(actual.mean()), 2),
        "mean_quote_usd": round(float(predicted.mean()), 2),
    }
    if segment:
        row.update(segment)
    return row


def _prepare_leadtime_pricing_frame(
    labeled: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
    problem_only: bool = False,
    with_archetypes: bool = False,
) -> pl.DataFrame:
    """LeadTime-labeled loads with cost, Davis quotes, optional book quotes and path shape."""
    if "primary_category" not in labeled.columns:
        return pl.DataFrame()

    frame = labeled.filter(pl.col("primary_category") == LC_CATEGORY)
    if problem_only and "is_tail" in frame.columns:
        frame = frame.filter(pl.col("is_tail"))
    if frame.is_empty():
        return pl.DataFrame()

    if with_archetypes and "path_archetype" not in frame.columns:
        if hist_path is None:
            from dqt import resolve_data_dir

            hist_path = resolve_data_dir(data_dir) / "etp" / HISTORY_CACHE
        frame = classify_path_archetypes(add_path_checkpoints(frame, hist_path=hist_path))

    frame = join_lc_carrier_cost(frame, features_path=features_path, data_dir=data_dir)
    frame = enrich_lc_davis_quotes(frame, davis_cache)
    if hist_path is None:
        from dqt import resolve_data_dir

        hist_path = resolve_data_dir(data_dir) / "etp" / HISTORY_CACHE
    frame = enrich_book_time_quotes(
        frame,
        hist_path=hist_path,
        survival_path=survival_path,
        cache_path=book_quotes_cache,
    )
    return frame


def _leadtime_pricing_rows(
    frame: pl.DataFrame,
    segments: list[tuple[str, str, pl.Expr]],
    *,
    segment_fields: tuple[str, str],
) -> list[dict[str, Any]]:
    """Score create/book quotes for each segment filter on a prepared LeadTime frame."""
    if frame.is_empty() or COST_COL not in frame.columns:
        return []

    key_field, label_field = segment_fields
    rows: list[dict[str, Any]] = []
    for seg_key, seg_label, seg_filter in segments:
        seg_df = frame.filter(seg_filter)
        if seg_df.is_empty():
            continue
        segment = {key_field: seg_key, label_field: seg_label}
        for timing_key, timing_label, suffix, quotes in CREATE_BOOK_TIMINGS:
            for quote_prefix, nominal, quote_label, col_stem in quotes:
                col = f"{col_stem}{suffix}"
                row = _score_pricing_slice(
                    seg_df,
                    col,
                    timing=timing_key,
                    timing_label=timing_label,
                    quote=quote_prefix,
                    quote_label=quote_label,
                    nominal=nominal,
                    segment=segment,
                )
                if row:
                    rows.append(row)
    return rows


def _join_book_time_quotes(df: pl.DataFrame, book: pl.DataFrame) -> pl.DataFrame:
    if book.is_empty():
        return df
    return df.drop(
        [c for c in book.columns if c != "loadnumber" and c in df.columns],
        strict=False,
    ).join(book, on="loadnumber", how="left")


def _book_quotes_for_loads(
    loadnumbers: pl.Series | pl.DataFrame,
    *,
    hist_path: Path,
    survival_path: Path,
) -> pl.DataFrame:
    """ETP/target quotes at or before booked_on_utc for the given load ids."""
    if isinstance(loadnumbers, pl.DataFrame):
        ids = loadnumbers.select("loadnumber").unique()
    else:
        ids = pl.DataFrame({"loadnumber": loadnumbers.unique()})
    if ids.is_empty() or not survival_path.exists():
        return pl.DataFrame()

    surv = pl.read_parquet(survival_path).join(ids, on="loadnumber", how="inner")
    if surv.is_empty():
        return pl.DataFrame()

    id_list = surv["loadnumber"].to_list()
    hist = (
        pl.scan_parquet(hist_path)
        .filter(pl.col("loadnumber").is_in(id_list))
        .select(
            "loadnumber",
            "snapshot_utc",
            "source",
            "etp50",
            "etp10",
            "target1",
            "target3",
        )
        .collect()
    )
    if hist.is_empty():
        return pl.DataFrame()

    joined = surv.select("loadnumber", "booked_on_utc").join(hist, on="loadnumber", how="inner")
    joined = joined.filter(pl.col("snapshot_utc") <= pl.col("booked_on_utc"))
    model = (
        joined.filter(pl.col("source") == "model")
        .sort("snapshot_utc")
        .group_by("loadnumber")
        .agg(
            pl.col("etp50").last().alias("etp50_book"),
            pl.col("etp10").last().alias("etp10_book"),
        )
    )
    audit = (
        joined.filter(pl.col("source") == "audit")
        .sort("snapshot_utc")
        .group_by("loadnumber")
        .agg(
            pl.col("target1").last().alias("target1_book"),
            pl.col("target3").last().alias("target3_book"),
        )
    )
    return model.join(audit, on="loadnumber", how="left")


def enrich_book_time_quotes(
    df: pl.DataFrame,
    *,
    hist_path: Path | str,
    survival_path: Path | str | None = None,
    cache_path: Path | str | None = None,
    force: bool = False,
) -> pl.DataFrame:
    """Last model/audit snapshot at or before ``booked_on_utc`` per load."""
    from dqt import is_fresh

    hist_path = Path(hist_path)
    if survival_path is None:
        survival_path = hist_path.parent / SURVIVAL_PER_LOAD_CACHE
    survival_path = Path(survival_path)

    cached: pl.DataFrame | None = None
    if cache_path is not None:
        cache_path = Path(cache_path)
        if not force and is_fresh(cache_path):
            cached = pl.read_parquet(cache_path)

    if cached is not None and not force:
        out = _join_book_time_quotes(df, cached)
        missing = (
            out.filter(pl.col("etp50_book").is_null())
            .select("loadnumber")
            .unique()
        )
        if missing.is_empty():
            return out
        supplement = _book_quotes_for_loads(
            missing, hist_path=hist_path, survival_path=survival_path
        )
        if supplement.is_empty():
            return out
        book = pl.concat([cached, supplement]).unique(subset=["loadnumber"], keep="last")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        book.write_parquet(cache_path, compression="zstd")
        return _join_book_time_quotes(df, book)

    book = _book_quotes_for_loads(df["loadnumber"], hist_path=hist_path, survival_path=survival_path)
    if book.is_empty():
        return df
    if cache_path is not None:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cached is not None and not force:
            book = pl.concat([cached, book]).unique(subset=["loadnumber"], keep="last")
        book.write_parquet(cache_path, compression="zstd")
    return _join_book_time_quotes(df, book)


def leadtime_pricing_accuracy_by_volatility(
    labeled: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> pl.DataFrame:
    """LeadTime-labeled loads: pricing accuracy at create vs book × volatile/stable ETP."""
    frame = _prepare_leadtime_pricing_frame(
        labeled,
        davis_cache=davis_cache,
        features_path=features_path,
        data_dir=data_dir,
        hist_path=hist_path,
        survival_path=survival_path,
        book_quotes_cache=book_quotes_cache,
    )
    if frame.is_empty() or "is_tail" not in frame.columns:
        return pl.DataFrame()
    rows = _leadtime_pricing_rows(
        frame,
        list(ETP_VOLATILITY_SEGMENTS),
        segment_fields=("etp_volatility", "volatility_label"),
    )
    return pl.DataFrame(rows)


def _lc_problem_loads(labeled: pl.DataFrame) -> pl.DataFrame:
    """Lead Time Change problem loads within a labeled cohort."""
    from dqt.etp_slider.movement.orders import LEGACY_LEADTIME_CATEGORY

    if labeled.is_empty() or "primary_category" not in labeled.columns:
        return pl.DataFrame()
    frame = labeled
    if "is_tail" in frame.columns:
        frame = frame.filter(pl.col("is_tail"))
    return frame.filter(
        pl.col("primary_category").replace(LEGACY_LEADTIME_CATEGORY, LC_CATEGORY) == LC_CATEGORY
    )


def _confidence_shift_row(
    sub: pl.DataFrame,
    *,
    segment: str,
    label: str,
    n_lc: int,
    pop_amt: float,
    pop_pct: float,
    is_estimate: bool = False,
) -> dict[str, Any]:
    n = sub.height
    avg_amt = float(sub["etp50_shift_amt"].mean() or 0) if n else None
    avg_pct = float(sub["etp50_shift_pct"].mean() or 0) if n else None
    row: dict[str, Any] = {
        "segment": segment,
        "label": label,
        "n_loads": n,
        "pct_of_lc": round(n / n_lc, 4) if n_lc else 0.0,
        "avg_shift_amt": round(avg_amt, 2) if avg_amt is not None else None,
        "avg_shift_pct": round(avg_pct, 4) if avg_pct is not None else None,
    }
    if is_estimate:
        row["is_estimate"] = True
        return row
    if avg_amt is not None:
        row["lift_amt_vs_pop"] = round(avg_amt - pop_amt, 2)
    if avg_pct is not None:
        row["lift_pct_vs_pop"] = round(avg_pct - pop_pct, 4)
    return row


def build_leadtime_confidence_breakdown(
    labeled: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> list[dict[str, Any]]:
    """LC problem loads: high-confidence vs confounder segments vs population baseline.

    Uses high-confidence (``leadtime_isolated_ind``) avg shift and pricing as a
    descriptive proxy for deterministic lead-time repricing; compares index-co-occur
    and residual buckets against the LC population average.
    """
    lc = _lc_problem_loads(labeled)
    if lc.is_empty():
        return []

    if "leadtime_isolated_ind" not in lc.columns and "index_change_ind" in lc.columns:
        lc = lc.with_columns(
            (pl.col("index_change_ind") == 0).cast(pl.Int8).alias("leadtime_isolated_ind"),
            (pl.col("index_change_ind") == 1).cast(pl.Int8).alias("index_clock_overlap_ind"),
        )
    elif "leadtime_isolated_ind" not in lc.columns:
        return []

    n_lc = lc.height
    pop_amt = float(lc["etp50_shift_amt"].mean() or 0)
    pop_pct = float(lc["etp50_shift_pct"].mean() or 0)
    pop_att_create: float | None = None
    pop_mae_create: float | None = None
    pop_att_book: float | None = None
    pop_mae_book: float | None = None

    pricing_by_seg: dict[str, dict[str, Any]] = {}
    if data_dir is not None:
        pricing_long = leadtime_pricing_accuracy_by_confidence(
            labeled,
            davis_cache=davis_cache,
            features_path=features_path,
            data_dir=data_dir,
            hist_path=hist_path,
            survival_path=survival_path,
            book_quotes_cache=book_quotes_cache,
        )
        if not pricing_long.is_empty():
            p50 = leadtime_confidence_pricing_pivot(pricing_long).filter(pl.col("quote") == "etp50")
            pricing_by_seg = {r["confidence_segment"]: r for r in p50.iter_rows(named=True)}
            pop_stats = pricing_by_seg.get("population")
            if pop_stats:
                pop_att_create = pop_stats.get("att_create")
                pop_mae_create = pop_stats.get("mae_create")
                pop_att_book = pop_stats.get("att_book")
                pop_mae_book = pop_stats.get("mae_book")

    def _merge_pricing(row: dict[str, Any]) -> dict[str, Any]:
        stats = pricing_by_seg.get(row["segment"])
        if not stats:
            return row
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
                row[key] = val
        if row.get("att_create") is not None and pop_att_create is not None:
            row["lift_att_create_vs_pop"] = round(float(row["att_create"]) - pop_att_create, 4)
        if row.get("mae_create") is not None and pop_mae_create is not None:
            row["lift_mae_create_vs_pop"] = round(float(row["mae_create"]) - pop_mae_create, 2)
        if row.get("att_book") is not None and pop_att_book is not None:
            row["lift_att_book_vs_pop"] = round(float(row["att_book"]) - pop_att_book, 4)
        if row.get("mae_book") is not None and pop_mae_book is not None:
            row["lift_mae_book_vs_pop"] = round(float(row["mae_book"]) - pop_mae_book, 2)
        return row

    rows: list[dict[str, Any]] = [
        _merge_pricing(
            _confidence_shift_row(
                lc,
                segment="population",
                label="All LC problem (population baseline)",
                n_lc=n_lc,
                pop_amt=pop_amt,
                pop_pct=pop_pct,
            )
        )
    ]

    covered = pl.lit(False)
    for seg_key, seg_label, seg_filter in LC_CONFIDENCE_SEGMENTS:
        sub = lc.filter(seg_filter)
        if sub.is_empty():
            continue
        covered = covered | seg_filter
        rows.append(
            _merge_pricing(
                _confidence_shift_row(
                    sub,
                    segment=seg_key,
                    label=seg_label,
                    n_lc=n_lc,
                    pop_amt=pop_amt,
                    pop_pct=pop_pct,
                )
            )
        )

    residual = lc.filter(~covered)
    if not residual.is_empty():
        rows.append(
            _merge_pricing(
                _confidence_shift_row(
                    residual,
                    segment="residual_lc",
                    label="Other LC (clocks, unclassified confounders)",
                    n_lc=n_lc,
                    pop_amt=pop_amt,
                    pop_pct=pop_pct,
                )
            )
        )

    hc_row = next((r for r in rows if r["segment"] == "high_confidence"), None)
    idx_row = next((r for r in rows if r["segment"] == "index_co_occur"), None)
    if hc_row and idx_row:
        est: dict[str, Any] = {
            "segment": "index_confound_lift",
            "label": "Implied index confound lift (co-occur − high confidence)",
            "is_estimate": True,
        }
        if hc_row.get("avg_shift_amt") is not None and idx_row.get("avg_shift_amt") is not None:
            est["lift_amt_vs_high_conf"] = round(
                float(idx_row["avg_shift_amt"]) - float(hc_row["avg_shift_amt"]), 2
            )
        if hc_row.get("avg_shift_pct") is not None and idx_row.get("avg_shift_pct") is not None:
            est["lift_pct_vs_high_conf"] = round(
                float(idx_row["avg_shift_pct"]) - float(hc_row["avg_shift_pct"]), 4
            )
        if (
            hc_row.get("att_create") is not None
            and idx_row.get("att_create") is not None
        ):
            est["lift_att_create_vs_high_conf"] = round(
                float(idx_row["att_create"]) - float(hc_row["att_create"]), 4
            )
        rows.append(est)

    return rows


def leadtime_pricing_accuracy_by_confidence(
    labeled: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
) -> pl.DataFrame:
    """Pricing accuracy at create/book for LC confidence vs confounder segments."""
    frame = _prepare_leadtime_pricing_frame(
        labeled,
        davis_cache=davis_cache,
        features_path=features_path,
        data_dir=data_dir,
        hist_path=hist_path,
        survival_path=survival_path,
        book_quotes_cache=book_quotes_cache,
        problem_only=True,
    )
    if frame.is_empty():
        return pl.DataFrame()

    segments: list[tuple[str, str, pl.Expr]] = [
        ("population", "All LC problem (baseline)", pl.lit(True)),
        *list(LC_CONFIDENCE_SEGMENTS),
    ]
    rows = _leadtime_pricing_rows(
        frame,
        segments,
        segment_fields=("confidence_segment", "confidence_label"),
    )
    return pl.DataFrame(rows)


def leadtime_confidence_pricing_pivot(long: pl.DataFrame) -> pl.DataFrame:
    """Wide view — one row per confidence segment × quote."""
    if long.is_empty():
        return long
    order = ["population", *[s[0] for s in LC_CONFIDENCE_SEGMENTS], "residual_lc"]
    return _leadtime_pricing_segment_pivot(
        long,
        segment_col="confidence_segment",
        label_col="confidence_label",
        segment_order=order,
        quote_order=[q[0] for q in CREATE_BOOK_QUOTES],
    )


def leadtime_pricing_accuracy_by_archetype(
    labeled: pl.DataFrame,
    *,
    davis_cache: Path | str | None = None,
    features_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    hist_path: Path | str | None = None,
    survival_path: Path | str | None = None,
    book_quotes_cache: Path | str | None = None,
    problem_only: bool = True,
) -> pl.DataFrame:
    """LeadTime loads: pricing accuracy at create vs book × path archetype (p50 focus)."""
    frame = _prepare_leadtime_pricing_frame(
        labeled,
        davis_cache=davis_cache,
        features_path=features_path,
        data_dir=data_dir,
        hist_path=hist_path,
        survival_path=survival_path,
        book_quotes_cache=book_quotes_cache,
        problem_only=problem_only,
        with_archetypes=True,
    )
    if frame.is_empty() or "path_archetype" not in frame.columns:
        return pl.DataFrame()

    segments: list[tuple[str, str, pl.Expr]] = []
    for arch in LEADTIME_ARCHETYPE_ORDER:
        if arch == "unknown":
            continue
        segments.append(
            (
                arch,
                LEADTIME_ARCHETYPE_LABELS.get(arch, arch),
                pl.col("path_archetype") == arch,
            )
        )
    for arch in sorted(frame["path_archetype"].drop_nulls().unique().to_list()):
        if arch in LEADTIME_ARCHETYPE_ORDER:
            continue
        segments.append((arch, LEADTIME_ARCHETYPE_LABELS.get(arch, arch), pl.col("path_archetype") == arch))

    rows = _leadtime_pricing_rows(
        frame,
        segments,
        segment_fields=("path_archetype", "archetype_label"),
    )
    return pl.DataFrame(rows)


def leadtime_pricing_accuracy_pivot(long: pl.DataFrame) -> pl.DataFrame:
    """Wide view — one row per (volatile/stable × quote) with create vs book side by side."""
    if long.is_empty():
        return long
    return _leadtime_pricing_segment_pivot(
        long,
        segment_col="etp_volatility",
        label_col="volatility_label",
        segment_order=[v[0] for v in ETP_VOLATILITY_SEGMENTS],
        quote_order=[q[0] for q in CREATE_BOOK_QUOTES],
    )


def leadtime_archetype_pricing_pivot(long: pl.DataFrame) -> pl.DataFrame:
    """Wide view — one row per (path archetype × quote) with create vs book side by side."""
    if long.is_empty():
        return long
    present = long["path_archetype"].unique().to_list() if "path_archetype" in long.columns else []
    segment_order = [a for a in LEADTIME_ARCHETYPE_ORDER if a in present]
    segment_order.extend(sorted(a for a in present if a not in segment_order and a != "unknown"))
    return _leadtime_pricing_segment_pivot(
        long,
        segment_col="path_archetype",
        label_col="archetype_label",
        segment_order=segment_order,
        quote_order=[q[0] for q in CREATE_BOOK_QUOTES],
    )


def _leadtime_pricing_segment_pivot(
    long: pl.DataFrame,
    *,
    segment_col: str,
    label_col: str,
    segment_order: list[str],
    quote_order: list[str],
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for seg in long[segment_col].unique().to_list():
        for quote in long["quote"].unique().to_list():
            sub = long.filter((pl.col(segment_col) == seg) & (pl.col("quote") == quote))
            if sub.is_empty():
                continue
            row: dict[str, Any] = {
                segment_col: seg,
                label_col: sub[label_col][0],
                "quote": quote,
                "quote_label": sub["quote_label"][0],
                "nominal_quantile": float(sub["nominal_quantile"][0]),
            }
            for timing in ("create", "book"):
                seg_timing = sub.filter(pl.col("timing") == timing)
                if seg_timing.is_empty():
                    continue
                r = seg_timing.row(0, named=True)
                row[f"n_{timing}"] = r["n_loads"]
                row[f"att_{timing}"] = r["attainment"]
                row[f"gap_pp_{timing}"] = r["gap_pp"]
                row[f"mae_{timing}"] = r["mae_usd"]
            rows.append(row)

    seg_order = {s: i for i, s in enumerate(segment_order)}
    quote_ord = {q: i for i, q in enumerate(quote_order)}
    rows.sort(
        key=lambda r: (
            seg_order.get(r[segment_col], 99),
            quote_ord.get(r["quote"], 99),
        )
    )
    return pl.DataFrame(rows)


def mde_enrichment_sql(ship_start: str, ship_end: str) -> str:
    template = MDE_ENRICHMENT_SQL.read_text()
    return template.format(ship_date_start=ship_start, ship_date_end=ship_end)


def load_mde_enrichment(
    ship_start: str = DEFAULT_AVAIL_START,
    ship_end: str = DEFAULT_AVAIL_END,
    *,
    cache_path: Path | str | None = None,
    loadnumbers: list[int] | None = None,
    force: bool = False,
) -> pl.DataFrame:
    """Cache-aware MDE / override enrichment from Snowflake."""
    from dqt import is_fresh

    if cache_path is None:
        suffix = f"{ship_start}_{ship_end}"
        cache_path = Path(f"data/etp/lc-mde-enrichment-{suffix}.parquet")
    cache_path = Path(cache_path)

    if not force and is_fresh(cache_path):
        return pl.read_parquet(cache_path)

    from arriveds.snowflake import query_sf

    if loadnumbers:
        frames: list[pl.DataFrame] = []
        batch_size = 5000
        for i in range(0, len(loadnumbers), batch_size):
            batch = loadnumbers[i : i + batch_size]
            ids = ",".join(str(x) for x in batch)
            sql = f"""
            WITH load_base AS (
                SELECT loadnumber FROM core_data.core.loads
                WHERE loadnumber IN ({ids})
            ),
            override_snap AS (
                SELECT m.loadnumber,
                    MAX(CASE WHEN PARSE_JSON(m.prediction):"total_charges_modifiers":"override_flag"::STRING
                        IN ('true','True','1','Y','yes') THEN 1 ELSE 0 END) AS override_flag_any,
                    MAX(PARSE_JSON(m.prediction):"total_charges_modifiers":"path_taken"::STRING) AS path_taken_last
                FROM events_raw.etp.etp_model_logs m
                INNER JOIN load_base lb ON lb.loadnumber = m.loadnumber
                WHERE m.etp_output IS NOT NULL
                GROUP BY m.loadnumber
            ),
            letp_latest AS (
                SELECT letp.loadnumber, letp.marketdisruptioneventlistid,
                    letp.isaffectedbymarketdisruptionevent, letp.istargeteligible, letp.isetpoverride
                FROM dapl_raw.accelerateprod.lod__loadelitetruckpurchasing letp
                INNER JOIN load_base lb ON lb.loadnumber = letp.loadnumber
                QUALIFY ROW_NUMBER() OVER (PARTITION BY letp.loadnumber ORDER BY letp.snowflakeupdatedon DESC NULLS LAST) = 1
            )
            SELECT lb.loadnumber,
                COALESCE(letp.marketdisruptioneventlistid IS NOT NULL, FALSE)::INT AS mde_ind,
                letp.marketdisruptioneventlistid AS mde_id,
                letp.isaffectedbymarketdisruptionevent AS mde_affected_ind,
                letp.isetpoverride,
                COALESCE(os.override_flag_any, 0) AS override_flag_any,
                os.path_taken_last
            FROM load_base lb
            LEFT JOIN letp_latest letp ON letp.loadnumber = lb.loadnumber
            LEFT JOIN override_snap os ON os.loadnumber = lb.loadnumber
            """
            frames.append(query_sf(sql))
        raw = pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
    else:
        sql = mde_enrichment_sql(ship_start, ship_end)
        raw = query_sf(sql)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    raw.write_parquet(cache_path, compression="zstd")
    return raw


def enrich_lc_mde(
    lc: pl.DataFrame,
    mde: pl.DataFrame,
) -> pl.DataFrame:
    """Join MDE / override fields onto LC cohort."""
    cols = [
        c
        for c in (
            "loadnumber",
            "mde_ind",
            "mde_id",
            "mde_affected_ind",
            "isetpoverride",
            "override_flag_any",
            "path_taken_last",
        )
        if c in mde.columns
    ]
    out = lc.join(mde.select(cols), on="loadnumber", how="left")
    if "mde_ind" in out.columns:
        out = out.with_columns(pl.col("mde_ind").fill_null(0).cast(pl.Int8))
    if "mde_affected_ind" in out.columns:
        out = out.with_columns(pl.col("mde_affected_ind").fill_null(0).cast(pl.Int8))
    if "override_flag_any" in out.columns:
        out = out.with_columns(pl.col("override_flag_any").fill_null(0).cast(pl.Int8))
    return out


def enrich_lc_mde_events(
    lc: pl.DataFrame,
    events: pl.DataFrame,
) -> pl.DataFrame:
    """Join lifecycle Market Movement MDE timeline events (applied_at) onto LC cohort."""
    from dqt.etp_slider.drift.mde_timeline import aggregate_mde_events_by_load

    agg = aggregate_mde_events_by_load(events)
    drop = [c for c in ("n_mde_timeline_events", "mde_timeline_ind") if c in lc.columns]
    if drop:
        lc = lc.drop(drop)
    if agg.is_empty():
        return lc.with_columns(
            pl.lit(0).cast(pl.UInt32).alias("n_mde_timeline_events"),
            pl.lit(0).cast(pl.Int8).alias("mde_timeline_ind"),
        )
    return lc.join(agg, on="loadnumber", how="left").with_columns(
        pl.col("n_mde_timeline_events").fill_null(0).cast(pl.UInt32),
        pl.col("mde_timeline_ind").fill_null(0).cast(pl.Int8),
    )


def load_mde_events_for_cohort(
    loadnumbers: list[int],
    *,
    cache_path: Path | str | None = None,
    force: bool = False,
) -> pl.DataFrame:
    """Batch Market Movement MDE apply events for LC loadnumbers."""
    from dqt.etp_slider.drift.mde_timeline import load_mde_timeline_events

    if not loadnumbers:
        return pl.DataFrame()
    return load_mde_timeline_events(loadnumbers, cache_path=cache_path, force=force)


def _column_as_ind(col: str) -> pl.Expr:
    """Coerce bool/int/string truthy columns to 0/1 indicators."""
    c = pl.col(col)
    as_str = c.cast(pl.Utf8, strict=False).fill_null("").str.to_lowercase()
    return (
        pl.when(c.is_null())
        .then(pl.lit(0))
        .when(as_str.is_in(["true", "1", "yes", "y"]))
        .then(pl.lit(1))
        .when(c.cast(pl.Int64, strict=False).fill_null(0) > 0)
        .then(pl.lit(1))
        .otherwise(pl.lit(0))
        .cast(pl.Int8)
    )


def build_leadtime_attribution_flags(lc: pl.DataFrame) -> pl.DataFrame:
    """Flag MDE/override contamination and pure lead-time attribution eligibility.

    ``leadtime_attribution_ind`` = LeadtimeChange problem load with no MDE signal
    (LETP record or lifecycle timeline event) and no model override flags.
    ``cliff_suspect_ind`` = late_cliff path plus MDE/override/pullback — review hint only.
    """
    letp_parts: list[pl.Expr] = []
    for col in ("mde_ind", "mde_affected_ind"):
        if col in lc.columns:
            letp_parts.append(pl.col(col).fill_null(0).cast(pl.Int8) == 1)
    mde_letp = pl.any_horizontal(letp_parts) if letp_parts else pl.lit(False)

    mde_timeline = (
        pl.col("mde_timeline_ind").fill_null(0).cast(pl.Int8) == 1
        if "mde_timeline_ind" in lc.columns
        else pl.lit(False)
    )

    override_parts: list[pl.Expr] = []
    for col in ("override_flag_any", "isetpoverride"):
        if col in lc.columns:
            override_parts.append(_column_as_ind(col) == 1)
    override_excluded = pl.any_horizontal(override_parts) if override_parts else pl.lit(False)

    mde_excluded = mde_letp | mde_timeline
    pullback = (
        pl.col("target_pullback_ind").fill_null(0).cast(pl.Int8) == 1
        if "target_pullback_ind" in lc.columns
        else pl.lit(False)
    )
    late_cliff = (
        pl.col("path_archetype") == "late_cliff"
        if "path_archetype" in lc.columns
        else pl.lit(False)
    )
    cliff_suspect = late_cliff & (mde_excluded | override_excluded | pullback)

    is_lc = pl.col("primary_category") == LC_CATEGORY if "primary_category" in lc.columns else pl.lit(True)
    attribution = is_lc & ~mde_excluded & ~override_excluded

    out = lc
    if "mde_timeline_ind" not in out.columns:
        out = out.with_columns(mde_timeline.cast(pl.Int8).alias("mde_timeline_ind"))

    return out.with_columns(
        mde_letp.cast(pl.Int8).alias("mde_letp_ind"),
        mde_excluded.cast(pl.Int8).alias("mde_excluded_ind"),
        override_excluded.cast(pl.Int8).alias("override_excluded_ind"),
        cliff_suspect.cast(pl.Int8).alias("cliff_suspect_ind"),
        attribution.cast(pl.Int8).alias("leadtime_attribution_ind"),
    )


def filter_leadtime_attribution_cohort(
    lc: pl.DataFrame,
    *,
    exclude_mde: bool = True,
    exclude_overrides: bool = True,
    exclude_pullback: bool = False,
    require_isolated: bool = False,
) -> pl.DataFrame:
    """Return LC loads eligible for pure lead-time clock attribution."""
    if "leadtime_attribution_ind" not in lc.columns:
        lc = build_leadtime_attribution_flags(lc)

    filt = pl.col("primary_category") == LC_CATEGORY if "primary_category" in lc.columns else pl.lit(True)
    if exclude_mde and "mde_excluded_ind" in lc.columns:
        filt = filt & (pl.col("mde_excluded_ind") == 0)
    if exclude_overrides and "override_excluded_ind" in lc.columns:
        filt = filt & (pl.col("override_excluded_ind") == 0)
    if exclude_pullback and "target_pullback_ind" in lc.columns:
        filt = filt & (pl.col("target_pullback_ind").fill_null(0) == 0)
    if require_isolated and "leadtime_isolated_ind" in lc.columns:
        filt = filt & (pl.col("leadtime_isolated_ind") == 1)
    return lc.filter(filt)


def build_leadtime_attribution_summary(lc: pl.DataFrame) -> dict[str, Any]:
    """Counts for LC cohort vs MDE/override-excluded attribution subset."""
    if "leadtime_attribution_ind" not in lc.columns:
        lc = build_leadtime_attribution_flags(lc)
    n = lc.height
    if n == 0:
        return {"n_lc": 0}

    attr = lc.filter(pl.col("leadtime_attribution_ind") == 1)
    excluded_mde = int(lc.filter(pl.col("mde_excluded_ind") == 1).height) if "mde_excluded_ind" in lc.columns else 0
    excluded_override = (
        int(lc.filter(pl.col("override_excluded_ind") == 1).height)
        if "override_excluded_ind" in lc.columns
        else 0
    )
    cliff_suspect = (
        int(lc.filter(pl.col("cliff_suspect_ind") == 1).height) if "cliff_suspect_ind" in lc.columns else 0
    )
    letp_only = (
        int(lc.filter((pl.col("mde_letp_ind") == 1) & (pl.col("mde_timeline_ind") == 0)).height)
        if {"mde_letp_ind", "mde_timeline_ind"}.issubset(lc.columns)
        else 0
    )
    timeline_only = (
        int(lc.filter((pl.col("mde_timeline_ind") == 1) & (pl.col("mde_letp_ind") == 0)).height)
        if {"mde_letp_ind", "mde_timeline_ind"}.issubset(lc.columns)
        else 0
    )

    by_archetype: list[dict[str, Any]] = []
    if "path_archetype" in lc.columns:
        tab = mde_archetype_cross_tab(lc)
        if not tab.is_empty():
            by_archetype = tab.to_dicts()

    return {
        "n_lc": n,
        "n_attribution": attr.height,
        "pct_attribution": round(attr.height / n, 4) if n else 0.0,
        "n_excluded_mde": excluded_mde,
        "n_excluded_override": excluded_override,
        "n_cliff_suspect": cliff_suspect,
        "n_mde_letp_only": letp_only,
        "n_mde_timeline_only": timeline_only,
        "avg_shift_amt_attribution": round(float(attr["etp50_shift_amt"].mean()), 2)
        if not attr.is_empty() and "etp50_shift_amt" in attr.columns
        else None,
        "avg_shift_amt_excluded_mde": round(
            float(lc.filter(pl.col("mde_excluded_ind") == 1)["etp50_shift_amt"].mean()), 2
        )
        if excluded_mde and "etp50_shift_amt" in lc.columns and "mde_excluded_ind" in lc.columns
        else None,
        "by_archetype": by_archetype,
    }


def mde_archetype_cross_tab(lc: pl.DataFrame) -> pl.DataFrame:
    """Path archetype × MDE/override/attribution rates within LC cohort."""
    if lc.is_empty() or "path_archetype" not in lc.columns:
        return pl.DataFrame()
    if "mde_excluded_ind" not in lc.columns:
        lc = build_leadtime_attribution_flags(lc)
    n = lc.height
    return (
        lc.group_by("path_archetype")
        .agg(
            pl.len().alias("n_loads"),
            (pl.len() / n).alias("pct_of_lc"),
            pl.col("mde_excluded_ind").sum().alias("n_mde_excluded"),
            pl.col("override_excluded_ind").sum().alias("n_override_excluded"),
            pl.col("cliff_suspect_ind").sum().alias("n_cliff_suspect"),
            pl.col("leadtime_attribution_ind").sum().alias("n_attribution"),
            pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
        )
        .with_columns(
            (pl.col("n_mde_excluded") / pl.col("n_loads")).alias("pct_mde_excluded"),
            (pl.col("n_attribution") / pl.col("n_loads")).alias("pct_attribution"),
        )
        .sort("n_loads", descending=True)
    )


def mde_cross_tab(lc: pl.DataFrame) -> pl.DataFrame:
    """MDE / override / attribution rates within LC cohort."""
    if "leadtime_attribution_ind" not in lc.columns:
        lc = build_leadtime_attribution_flags(lc)
    rows: list[dict[str, Any]] = []
    n = lc.height
    for col, label in (
        ("mde_ind", "mde_letp_record"),
        ("mde_affected_ind", "mde_affected"),
        ("mde_timeline_ind", "mde_timeline_event"),
        ("mde_excluded_ind", "mde_excluded"),
        ("override_excluded_ind", "override_excluded"),
        ("isetpoverride", "isetpoverride"),
        ("override_flag_any", "override_flag"),
        ("hard_ft_inc_ind", "hard_ft_secondary"),
        ("target_pullback_ind", "target_pullback"),
        ("cliff_suspect_ind", "cliff_suspect"),
        ("leadtime_attribution_ind", "leadtime_attribution"),
    ):
        if col not in lc.columns:
            continue
        if lc[col].dtype in (pl.Int8, pl.Int32, pl.Int64, pl.UInt32):
            k = int(lc.filter(pl.col(col) == 1).height)
        else:
            k = int(lc.filter(_column_as_ind(col) == 1).height)
        rows.append({"signal": label, "n_loads": k, "pct_of_lc": round(k / n, 4) if n else 0.0})
    return pl.DataFrame(rows)


def endpoint_category_shares(lc: pl.DataFrame) -> pl.DataFrame:
    """Proxy OLS cat-1/2/3 shares from Davis endpoint deltas (pooled LC cohort)."""
    n = lc.height
    if n == 0:
        return pl.DataFrame()

    cat1_cols = [c for c in ("dat_rate_delta", "fuel_cost_delta", "lag7_cpm_delta") if c in lc.columns]
    cat2_cols = [c for c in ("total_charges_delta", "hard_ft_delta") if c in lc.columns]

    rows: list[dict[str, Any]] = []
    rows.append({"category": "LeadtimeChange (clocks)", "metric": "clocks_moved_rate", "value": float(lc["clocks_moved_ind"].mean()) if "clocks_moved_ind" in lc.columns else None})
    rows.append({"category": "LeadtimeChange (clocks)", "metric": "clock_only_rate", "value": float(lc["clock_only_ind"].mean()) if "clock_only_ind" in lc.columns else None})
    for c in cat1_cols:
        rows.append({"category": "RandomChange (market)", "metric": f"avg_abs_{c}", "value": float(lc[c].fill_null(0).abs().mean())})
    for c in cat2_cols:
        rows.append({"category": "Shipment/Difficulty", "metric": f"avg_abs_{c}", "value": float(lc[c].fill_null(0).abs().mean())})
    rows.append({"category": "Endpoint shift", "metric": "avg_etp50_shift_amt", "value": float(lc["etp50_shift_amt"].mean())})
    rows.append({"category": "Endpoint shift", "metric": "avg_target3_shift_amt", "value": float(lc["target3_shift_amt"].mean()) if "target3_shift_amt" in lc.columns else None})
    return pl.DataFrame(rows)


def sarima_dial_context(
    lc: pl.DataFrame,
    *,
    dial_path: Path | str | None = None,
) -> pl.DataFrame:
    """Join avail-month mean dial forecast — contextual, not counterfactual."""
    if dial_path is None:
        dial_path = Path("data/sarima_dial_walkforward.parquet")
    dial_path = Path(dial_path)
    if not dial_path.exists() or "made_available_utc" not in lc.columns:
        return pl.DataFrame()

    dial = pl.read_parquet(dial_path)
    date_col = "date" if "date" in dial.columns else "booked_date"
    pred_col = "y_hat_sarima_cal" if "y_hat_sarima_cal" in dial.columns else "y_hat_sarima"
    if date_col not in dial.columns or pred_col not in dial.columns:
        return pl.DataFrame()

    lc_month = lc.with_columns(pl.col("made_available_utc").dt.strftime("%Y-%m").alias("avail_month"))
    dial_month = dial.with_columns(pl.col(date_col).dt.strftime("%Y-%m").alias("avail_month"))
    month_dial = dial_month.group_by("avail_month").agg(pl.col(pred_col).mean().alias("avg_sarima_dial"))
    month_lc = lc_month.group_by("avail_month").agg(
        pl.len().alias("n_lc"),
        pl.col("etp50_shift_amt").mean().alias("avg_lc_shift_amt"),
        pl.col("index_change_ind").mean().alias("index_change_rate")
        if "index_change_ind" in lc.columns
        else pl.lit(None).alias("index_change_rate"),
    )
    return month_lc.join(month_dial, on="avail_month", how="left").sort("avail_month")


def enrich_override_from_feature_history(
    lc: pl.DataFrame,
    *,
    data_dir: Path | str,
) -> pl.DataFrame:
    """Any override_flag in feature-history snapshots for LC loadnumbers."""
    cache = Path(data_dir) / "etp"
    feat_path = resolve_feature_snapshots_path(cache)
    if feat_path is None:
        return lc
    ids = lc["loadnumber"].to_list()
    feat = load_feature_snapshots(feat_path, ids)
    if feat is None or feat.is_empty() or "override_flag" not in feat.columns:
        return lc

    flags = (
        feat.group_by("loadnumber")
        .agg(
            pl.col("override_flag")
            .filter(pl.col("override_flag").is_not_null() & (pl.col("override_flag") != ""))
            .len()
            .alias("n_override_snaps")
        )
        .with_columns((pl.col("n_override_snaps") > 0).cast(pl.Int8).alias("override_flag_any"))
    )
    if "override_flag_any" in lc.columns:
        lc = lc.drop("override_flag_any")
    return lc.join(flags.select("loadnumber", "override_flag_any"), on="loadnumber", how="left").with_columns(
        pl.col("override_flag_any").fill_null(0).cast(pl.Int8)
    )


def export_lc_analysis(
    *,
    data_dir: Path | str = "data",
    out_dir: Path | str | None = None,
    avail_start: str = DEFAULT_AVAIL_START,
    avail_end: str = DEFAULT_AVAIL_END,
    davis_cache: Path | str | None = None,
    mde_cache: Path | str | None = None,
    skip_snowflake: bool = False,
) -> dict[str, Any]:
    """Run full LC analysis pipeline; write parquets + JSON summary."""
    data_dir = Path(data_dir)
    cache = data_dir / "etp"
    out_dir = Path(out_dir) if out_dir else cache
    out_dir.mkdir(parents=True, exist_ok=True)

    tail, lc = build_lc_cohort(
        data_dir=data_dir,
        avail_start=avail_start,
        avail_end=avail_end,
        davis_cache=davis_cache,
    )
    hist_path = cache / HISTORY_CACHE

    lc = add_path_checkpoints(lc, hist_path=hist_path)
    lc = classify_path_archetypes(lc)
    lc = detect_target_pullbacks(lc, hist_path=hist_path)
    lc = lc.with_columns(haul_band_expr().alias("haul_band"))

    mde_path = Path(mde_cache) if mde_cache else cache / f"lc-mde-enrichment-{avail_start}_{avail_end}.parquet"
    mde_events_path = cache / f"lc-mde-timeline-{avail_start}_{avail_end}.parquet"
    if mde_path.exists():
        lc = enrich_lc_mde(lc, pl.read_parquet(mde_path))
    elif not skip_snowflake:
        try:
            mde = load_mde_enrichment(
                avail_start,
                avail_end,
                cache_path=mde_path,
                loadnumbers=lc["loadnumber"].to_list(),
            )
            lc = enrich_lc_mde(lc, mde)
        except (OSError, ValueError, KeyError, pl.exceptions.ComputeError):
            pass

    if mde_events_path.exists():
        lc = enrich_lc_mde_events(lc, pl.read_parquet(mde_events_path))
    elif not skip_snowflake:
        try:
            events = load_mde_events_for_cohort(
                lc["loadnumber"].to_list(),
                cache_path=mde_events_path,
            )
            lc = enrich_lc_mde_events(lc, events)
        except (OSError, ValueError, KeyError, pl.exceptions.ComputeError):
            pass

    lc = enrich_override_from_feature_history(lc, data_dir=data_dir)
    lc = build_leadtime_attribution_flags(lc)

    profiles = profile_lc_cohort(lc)
    exemplars = build_lc_exemplars(lc)
    mde_tab = mde_cross_tab(lc)
    attribution_summary = build_leadtime_attribution_summary(lc)
    mde_arch_tab = mde_archetype_cross_tab(lc)
    endpoint_shares = endpoint_category_shares(lc)
    sarima_ctx = sarima_dial_context(lc, dial_path=data_dir / "sarima_dial_walkforward.parquet")
    index_impact = index_secondary_impact(lc)
    davis_for_quotes = Path(davis_cache) if davis_cache else cache / f"analysis-davis-raw-{avail_start}_{avail_end}.parquet"
    pricing_acc = lc_pricing_accuracy_comparison(
        lc,
        davis_cache=davis_for_quotes if davis_for_quotes.exists() else None,
        data_dir=data_dir,
    )
    pricing_pivot = lc_pricing_accuracy_pivot(pricing_acc)

    labeled_path = cache / f"executive-{avail_start}_{avail_end}" / "labeled-cohort.parquet"
    if labeled_path.exists():
        labeled = pl.read_parquet(labeled_path)
    else:
        from dqt.etp_slider.problem_loads.cohort import build_labeled_cohort

        labeled = build_labeled_cohort(
            per_load_path=cache / PER_LOAD_CACHE,
            hist_path=cache / HISTORY_CACHE,
            davis_path=davis_for_quotes if davis_for_quotes.exists() else None,
            avail_start=avail_start,
            avail_end=avail_end,
            pct_threshold=0.10,
            threshold_mode="scaled",
        )
    book_cache = cache / f"leadtime-book-quotes-{avail_start}_{avail_end}.parquet"
    lt_pricing = leadtime_pricing_accuracy_by_volatility(
        labeled,
        davis_cache=davis_for_quotes if davis_for_quotes.exists() else None,
        data_dir=data_dir,
        hist_path=cache / HISTORY_CACHE,
        survival_path=cache / SURVIVAL_PER_LOAD_CACHE,
        book_quotes_cache=book_cache,
    )
    lt_pricing_pivot = leadtime_pricing_accuracy_pivot(lt_pricing)
    lt_arch_pricing = leadtime_pricing_accuracy_by_archetype(
        labeled,
        davis_cache=davis_for_quotes if davis_for_quotes.exists() else None,
        data_dir=data_dir,
        hist_path=cache / HISTORY_CACHE,
        survival_path=cache / SURVIVAL_PER_LOAD_CACHE,
        book_quotes_cache=book_cache,
        problem_only=True,
    )
    lt_arch_pivot = leadtime_archetype_pricing_pivot(lt_arch_pricing)

    lc_path = out_dir / "lc-tail-2025.parquet"
    lc.write_parquet(lc_path, compression="zstd")

    from dqt.etp_slider.problem_loads.sarima_etp_comparison import (
        export_sarima_etp_comparison,
    )

    sarima_etp = export_sarima_etp_comparison(
        labeled,
        out_dir=out_dir,
        data_dir=data_dir,
        davis_cache=davis_for_quotes if davis_for_quotes.exists() else None,
        hist_path=cache / HISTORY_CACHE,
        survival_path=cache / SURVIVAL_PER_LOAD_CACHE,
        book_quotes_cache=book_cache,
        sarima_pp_path=data_dir / "sarima_pp_quantiles.parquet",
        dial_path=data_dir / "sarima_dial_walkforward.parquet",
        lc_path=lc_path,
    )

    (out_dir / "lc-tail-loadnumbers.txt").write_text(
        "\n".join(str(x) for x in lc["loadnumber"].to_list()) + "\n"
    )
    if not exemplars.is_empty():
        exemplars.write_csv(out_dir / "lc-tail-exemplars.csv")

    summary: dict[str, Any] = {
        "n_tail": tail.height,
        "n_leadtime_change": lc.height,
        "avail_start": avail_start,
        "avail_end": avail_end,
        "overview": profiles["overview"].to_dicts(),
        "by_path_archetype": profiles["by_path_archetype"].to_dicts() if not profiles["by_path_archetype"].is_empty() else [],
        "secondary_flags": profiles["secondary_flags"].to_dicts(),
        "index_impact": {
            k: v.to_dicts() if not v.is_empty() else []
            for k, v in index_impact.items()
        },
        "mde_cross_tab": mde_tab.to_dicts() if not mde_tab.is_empty() else [],
        "mde_archetype_cross_tab": mde_arch_tab.to_dicts() if not mde_arch_tab.is_empty() else [],
        "attribution_summary": attribution_summary,
        "endpoint_shares": endpoint_shares.to_dicts(),
        "sarima_context": sarima_ctx.to_dicts() if not sarima_ctx.is_empty() else [],
        "pricing_accuracy": pricing_acc.to_dicts() if not pricing_acc.is_empty() else [],
        "pricing_accuracy_pivot": pricing_pivot.to_dicts() if not pricing_pivot.is_empty() else [],
        "leadtime_pricing_accuracy": lt_pricing.to_dicts() if not lt_pricing.is_empty() else [],
        "leadtime_pricing_pivot": lt_pricing_pivot.to_dicts() if not lt_pricing_pivot.is_empty() else [],
        "leadtime_archetype_pricing": lt_arch_pricing.to_dicts() if not lt_arch_pricing.is_empty() else [],
        "leadtime_archetype_pricing_pivot": lt_arch_pivot.to_dicts() if not lt_arch_pivot.is_empty() else [],
        "sarima_etp_comparison_summary": sarima_etp.get("summary", {}),
    }
    summary_path = out_dir / "lc-analysis-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))

    for name, frame in profiles.items():
        if not frame.is_empty():
            frame.write_csv(out_dir / f"lc-profile-{name}.csv")
    for name, frame in index_impact.items():
        if not frame.is_empty():
            frame.write_csv(out_dir / f"lc-index-{name}.csv")
    if not pricing_acc.is_empty():
        pricing_acc.write_csv(out_dir / "lc-pricing-accuracy.csv")
        pricing_pivot.write_csv(out_dir / "lc-pricing-accuracy-pivot.csv")
    if not lt_pricing.is_empty():
        lt_pricing.write_csv(out_dir / "leadtime-pricing-accuracy.csv")
        lt_pricing_pivot.write_csv(out_dir / "leadtime-pricing-accuracy-pivot.csv")
    if not lt_arch_pricing.is_empty():
        lt_arch_pricing.write_csv(out_dir / "leadtime-archetype-pricing.csv")
        lt_arch_pivot.write_csv(out_dir / "leadtime-archetype-pricing-pivot.csv")

    return {
        "tail": tail,
        "lc": lc,
        "profiles": profiles,
        "index_impact": index_impact,
        "exemplars": exemplars,
        "summary": summary,
        "paths": {
            "lc_parquet": lc_path,
            "summary_json": summary_path,
            "exemplars_csv": out_dir / "lc-tail-exemplars.csv",
            **{f"sarima_etp_{k}": v for k, v in sarima_etp.get("paths", {}).items()},
        },
    }


def render_lc_readout(summary: dict[str, Any], *, parent_n: int = 278_458) -> str:
    """Generate exec memo markdown from analysis summary JSON."""
    n = summary.get("n_leadtime_change", 0)
    n_tail = summary.get("n_tail", 0)
    overview = {r["metric"]: r["value"] for r in summary.get("overview", [])}
    archetypes = summary.get("by_path_archetype", [])
    secondary = summary.get("secondary_flags", [])
    index_impact = summary.get("index_impact", {})
    mde = summary.get("mde_cross_tab", [])
    pricing = summary.get("pricing_accuracy_pivot", [])
    lt_pricing = summary.get("leadtime_pricing_pivot", [])
    lt_arch_pricing = summary.get("leadtime_archetype_pricing_pivot", [])
    sarima_etp = summary.get("sarima_etp_comparison_summary", {})

    def _pct(x: float | None) -> str:
        return f"{x * 100:.1f}%" if x is not None else "—"

    def _amt(x: float | None) -> str:
        return f"${x:,.0f}" if x is not None else "—"

    def _amt_signed(x: float | None) -> str:
        return f"${x:+,.0f}" if x is not None else "—"

    def _att(x: float | None) -> str:
        return f"{x * 100:.1f}%" if x is not None else "—"

    def _att_delta(create: float | None, book: float | None) -> str:
        if create is None or book is None:
            return "—"
        return f"{(book - create) * 100:+.1f}pp"

    clock_only = next((r for r in secondary if r.get("signal") == "clock_only"), {})
    index_flag = next((r for r in secondary if r.get("signal") == "index"), {})
    pullback = next((r for r in mde if r.get("signal") == "target_pullback"), {})

    idx_lift = (index_impact.get("lift_summary") or [{}])[0]
    idx_components = index_impact.get("by_index_component", [])
    idx_corrs = index_impact.get("correlations", [])

    comp_lines = "\n".join(
        f"| {r.get('index_component', '?')} | {r.get('n_fired', 0):,} | {_pct(r.get('pct_of_lc'))} | "
        f"{_amt(r.get('avg_shift_amt_fired'))} | {_amt(r.get('avg_shift_amt_not_fired'))} | "
        f"{_amt_signed(r.get('lift_amt'))} |"
        for r in idx_components
    )
    corr_lines = "\n".join(
        f"| {r.get('feature', '?')} | {r.get('corr_shift_amt', 0):+.3f} | {r.get('corr_shift_pct', 0):+.3f} |"
        for r in idx_corrs
    )

    arch_lines = "\n".join(
        f"| {r.get('path_archetype', '?')} | {r.get('n_loads', 0):,} | {_pct(r.get('pct_of_lc'))} | "
        f"{_pct(r.get('avg_shift_pct'))} | {_amt(r.get('avg_shift_amt'))} | "
        f"{_pct(r.get('pct_shift_positive'))} | {_pct(r.get('pct_shift_negative'))} |"
        for r in archetypes
    )
    pricing_lines = "\n".join(
        f"| {r.get('quote_label', '?')} | {int(r.get('n_available') or 0):,} | {_att(r.get('att_available'))} | "
        f"{r.get('gap_pp_available', 0):+.1f} | {_amt(r.get('mae_available'))} | "
        f"{int(r.get('n_48hr') or 0):,} | {_att(r.get('att_48hr'))} | {r.get('gap_pp_48hr', 0):+.1f} | "
        f"{_amt(r.get('mae_48hr'))} |"
        for r in pricing
    )
    lt_p50_lines = "\n".join(
        f"| {r.get('volatility_label', '?')} | {int(r.get('n_create') or 0):,} | {_att(r.get('att_create'))} | "
        f"{r.get('gap_pp_create', 0):+.1f} | {_amt(r.get('mae_create'))} | "
        f"{int(r.get('n_book') or 0):,} | {_att(r.get('att_book'))} | {r.get('gap_pp_book', 0):+.1f} | "
        f"{_amt(r.get('mae_book'))} |"
        for r in lt_pricing
        if r.get("quote") == "etp50"
    )
    lt_full_lines = "\n".join(
        f"| {r.get('volatility_label', '?')} | {r.get('quote_label', '?')} | "
        f"{int(r.get('n_create') or 0):,} | {_att(r.get('att_create'))} | {_amt(r.get('mae_create'))} | "
        f"{int(r.get('n_book') or 0):,} | {_att(r.get('att_book'))} | {_amt(r.get('mae_book'))} |"
        for r in lt_pricing
    )
    lt_arch_p50_lines = "\n".join(
        f"| {r.get('archetype_label', r.get('path_archetype', '?'))} | "
        f"{int(r.get('n_create') or 0):,} | {_att(r.get('att_create'))} | {r.get('gap_pp_create', 0):+.1f} | "
        f"{_amt(r.get('mae_create'))} | {int(r.get('n_book') or 0):,} | {_att(r.get('att_book'))} | "
        f"{r.get('gap_pp_book', 0):+.1f} | {_amt(r.get('mae_book'))} | "
        f"{_att_delta(r.get('att_create'), r.get('att_book'))} |"
        for r in lt_arch_pricing
        if r.get("quote") == "etp50"
    )

    sarima_seg_lines = ""
    sarima_cov = sarima_etp.get("coverage", {})
    for seg in sarima_etp.get("segments", []):
        if seg.get("segment") not in {
            "all_volatile",
            "leadtime_clock_only",
            "path_steady",
            "path_late_cliff",
            "path_mid_plateau",
        }:
            continue
        sarima_seg_lines += (
            f"| {seg.get('label', seg.get('segment', '?'))} "
            f"| {int(seg.get('n_loads', 0)):,} "
            f"| {_amt(seg.get('avg_mae_etp_book'))} "
            f"| {_amt(seg.get('avg_mae_sarima_book'))} "
            f"| {_pct(seg.get('share_sarima_mae_lt_etp_book'))} |\n"
        )

    return f"""# Lead Time Change Problem Loads — Operations Readout

**Cohort:** 7–14 day lead, available {summary.get('avail_start')} → {summary.get('avail_end')}  
**Problem-load rule:** shift ≥ 10% × ETP50 at available (scaled)  
**Generated from:** `lc-analysis-summary.json`

---

## Headline

**{n:,} loads** ({n / parent_n * 100:.1f}% of {parent_n:,} long-lead cohort) are problem loads where **lead-time clocks** are the primary explanation for a ≥10% ETP50 rise from available to 48 hours before pickup. They represent **{n / n_tail * 100:.1f}%** of all {n_tail:,} problem loads in this window.

| Metric | Value |
|--------|------:|
| Avg ETP50 shift | {_pct(overview.get('avg_shift_pct'))} / {_amt(overview.get('avg_shift_amt'))} |
| Median ETP50 shift | {_pct(overview.get('med_shift_pct'))} / {_amt(overview.get('med_shift_amt'))} |
| Clock-only (pure lead-time tick) | {_pct(clock_only.get('pct_of_lc'))} of LC cohort |
| Target pullback after model spike | {_pct(pullback.get('pct_of_lc'))} of LC cohort |

---

## What operations feels vs what the model is doing

Most of these loads are **stable shipments** (charges and difficulty flags did not win the primary label). The ETP50 climb is driven by **book_2_pkup** and **avail_2_book** advancing as pickup approaches — expected repricing on 7–14 day freight.

The pain points cluster in two areas:

1. **Late-board cliffs** — indexed ETP50 jumps in the last 3–4 days before pickup (see path archetypes below).
2. **Target vs model gaps** — audit targets sometimes spike then get **manually pulled back** while model ETP50 stays elevated (~{pullback.get('pct_of_lc', 0) * 100:.0f}% of LC loads show at least one pullback hour).

---

## Path archetypes

| Archetype | Loads | Share of LC | Avg % shift | Avg $ shift | % ↑ | % ↓ |
|-----------|------:|------------:|------------:|------------:|----:|----:|
{arch_lines or '| — | — | — | — |'}

- **early_burst** — large move in first days after posting (initial calibration).
- **mid_plateau** — flat mid-board then late move.
- **late_cliff** — step-up entering final 72–96h (often feels “sudden” to brokers).
- **steady** — gradual climb without a single cliff.

### Did lead-time repricing help? (ETP p50 by path shape)

Problem **LeadtimeChange** loads only (same 35k cohort as above). **Create** = quote at available; **Book** = last snapshot ≤ `booked_on_utc`. Realized cost = carrier charges. **Δ Att** = book minus create attainment (positive = repricing moved toward 50% target).

| Path shape | n @ create | Att p50 | Gap pp | MAE p50 | n @ book | Att p50 | Gap pp | MAE p50 | Δ Att |
|------------|----------:|--------:|-------:|--------:|---------:|--------:|-------:|--------:|------:|
{lt_arch_p50_lines or '| — | — | — | — | — | — | — | — | — | — |'}

**Read:** Every archetype shows the same pattern as the headline — **under-quoted at create**, **materially better by book** as clocks advance. `early_burst` loads tend to start with the largest gap (big early ETP move not yet aligned to realized cost); `late_cliff` loads often show the largest book-time MAE residual (sudden final-board step may overshoot or undershoot carrier outcome).

---

## Secondary index drift (RandomChange co-flag)

**{_pct(index_flag.get('pct_of_lc'))}** of LC loads also trigger ``index_change_ind`` (lag7 CPM / fuel / DAT endpoint delta ≥ threshold). Clocks still win the primary label because of waterfall priority — this is **co-occurring market drift**, not a reclassification to RandomChange.

| Segment | Loads | Share | Avg $ shift |
|---------|------:|------:|------------:|
| Index co-occur | {idx_lift.get('n_with_index', 0):,} | {_pct(idx_lift.get('index_event_rate'))} | {_amt(idx_lift.get('avg_shift_with_index'))} |
| No index drift | {idx_lift.get('n_no_index', 0):,} | {_pct(1 - (idx_lift.get('index_event_rate') or 0))} | {_amt(idx_lift.get('avg_shift_no_index'))} |

**Lift with index vs without:** {_amt_signed(idx_lift.get('lift_amt_index_vs_no'))}/load (descriptive; weak correlation below → not a separate causal bucket).

### Which index moved?

| Component | Loads flagged | % of LC | Avg $ (flagged) | Avg $ (not flagged) | Lift |
|-----------|-------------:|--------:|----------------:|--------------------:|-----:|
{comp_lines or '| — | — | — | — | — | — |'}

**lag7 CPM** drives almost all flags on 7–14 day boards — expected slow market drift during long lead time.

### Correlation with ETP50 shift (endpoint, not step OLS)

| Feature | r vs $ shift | r vs % shift |
|---------|-------------:|-------------:|
{corr_lines or '| — | — | — |'}

**Read:** correlations near zero → index drift **does not explain** the LC problem-load move load-by-load; clocks remain the dominant story. SARIMA dial smoothing targets this index layer globally, not per-load lead-time cliffs.

---

## Is lead-time ETP movement accurate vs realized cost?

**Question:** As ETP reprices from load creation to book, do quotes bracket what carriers actually got paid?

Scope: all **Lead Time Change**–labeled shipments in the 7–14 day window (not just the 35k volatile problem subset). Split by **ETP volatility** (volatile = ≥10% avail→48h shift; stable = below threshold). Realized cost = `carrier_shipment_charges_total`.

### ETP p50 — create vs book

| ETP segment | n @ create | Att p50 | Gap pp | MAE p50 | n @ book | Att p50 | Gap pp | MAE p50 |
|-------------|----------:|--------:|-------:|--------:|---------:|--------:|-------:|--------:|
{lt_p50_lines or '| — | — | — | — | — | — | — | — | — |'}

**Read:** Volatile lead-time loads are **severely under-quoted at create** (p50 att 7% vs 50% target) and **recover to ~44% by book** as clocks advance — MAE falls from $284 → $155. Stable lead-time loads follow the same pattern at lower magnitude (27% → 41%; MAE $168 → $133). The ETP repricing path tracks realized carrier cost; volatile loads are the calibration gap ops feels on the board.

### Full grid (p50, t25, t75 × create vs book)

| ETP segment | Quote | n @ create | Att @ create | MAE @ create | n @ book | Att @ book | MAE @ book |
|-------------|-------|----------:|-------------:|-------------:|---------:|-----------:|-----------:|
{lt_full_lines or '| — | — | — | — | — | — | — | — |'}

- **Create** = first available snapshot; **Book** = last model/audit snapshot ≤ `booked_on_utc`.
- **t25 / t75 @ create** = Target 2 / Target 4 from Davis audit endpoints.
- **t25 / t75 @ book** = Target 1 / Target 3 at book (Target 2/4 not in history telemetry).

### LC problem-load subset — create vs 48h pre-pickup

Problem-only slice (35k volatile LC loads) at the drift-report endpoint (48h before pickup):

| Quote | n @ avail | Att @ avail | Gap pp | MAE @ avail | n @ 48h | Att @ 48h | Gap pp | MAE @ 48h |
|-------|----------:|------------:|-------:|------------:|--------:|----------:|-------:|----------:|
{pricing_lines or '| — | — | — | — | — | — | — | — | — |'}

---

## MDE and overrides

| Signal | Loads | % of LC |
|--------|------:|--------:|
{chr(10).join(f"| {r.get('signal', '?')} | {r.get('n_loads', 0):,} | {_pct(r.get('pct_of_lc'))} |" for r in mde) or '| — | — | — |'}

**DifficultyOverride as primary label** is rare in the full problem-load tail (~350 loads). MDE and charge overrides should be read as **co-occurring signals**, not the main driver of the 35k LC cohort.

---

## Would SARIMA have helped?

**Short answer: No — not for the clock-driven majority.**

SARIMA in this repo smooths the **global market dial** on booked loads. It does not observe per-load lead-time clocks (`book_2_pkup`, `avail_2_book`). For loads labeled LeadtimeChange:

- **Clock-only rate:** {_pct(clock_only.get('pct_of_lc'))} — expected model-by-design repricing.
- **Secondary index/charge flags** did not win priority but may co-occur on a subset.

### SARIMA pp50 vs ETP p50 @ book (volatile cohort)

Book-time counterfactual: `sarima_pp_50` from the global dial vs realized carrier cost, compared to ETP p50 at book on the same loads. **ETP shift columns elsewhere in this memo remain avail→48hr.**

Coverage: {int(sarima_cov.get('n_with_sarima', 0)):,} / {int(sarima_cov.get('n_loads', 0)):,} volatile loads with SARIMA pp50 ({_pct(sarima_cov.get('pct_with_sarima'))}).

| Segment | Loads | MAE ETP @ book | MAE SARIMA @ book | Share SARIMA closer |
|---------|------:|---------------:|------------------:|--------------------:|
{sarima_seg_lines or '| — | — | — | — | — |'}

**Read:** Negative delta (SARIMA closer) is rare on clock-driven path shapes if ETP @ book already tracks realized cost. SARIMA only plausibly helps where index co-occur fired — see full export `data/etp/sarima-vs-etp-volatile-summary.json`.

**Actionable levers for ops:** target-setting workflow (pullback audit on `late_cliff` loads), broker education on median vs average drift, and separating ShipmentChange loads (charge jumps) from lead-time repricing.

---

## FAQ

**Is +10% a lot?**  
Scaled gate = 10% of ETP50 at available (e.g. $50 on a $500 load). LC avg {_pct(overview.get('avg_shift_pct'))} vs ~5% endpoint drift on the full parent cohort.

**Should we smooth cadence?**  
Smoothing the global dial (SARIMA) will not remove clock repricing. Consider target cadence and when manual overrides fire.

**Are these bad loads?**  
Problem load = large observed shift, not proof the shift was inappropriate.

---

## Recommended actions

1. **Target workflow audit** on `late_cliff` + `target_pullback` exemplars (`lc-tail-exemplars.csv`).
2. **Broker comms** — median load flat early; mean driven by movers; not uniform 1–2%/day on every load.
3. **Keep ShipmentChange separate** — 9.6k charge-driven problem loads need different ops playbooks than lead-time clocks.

---

## Artifacts

| File | Description |
|------|-------------|
| `data/etp/lc-tail-2025.parquet` | Full LC cohort with archetypes + enrichment |
| `data/etp/lc-tail-exemplars.csv` | Stratified example loads |
| `data/etp/lc-analysis-summary.json` | Machine-readable stats for slides |
| `data/etp/leadtime-pricing-accuracy.csv` | LeadTime × volatile/stable × create/book (long) |
| `data/etp/leadtime-pricing-accuracy-pivot.csv` | Create vs book pivot |
| `data/etp/leadtime-archetype-pricing.csv` | LeadTime problem loads × path shape × create/book (long) |
| `data/etp/leadtime-archetype-pricing-pivot.csv` | Path shape × create vs book pivot |
| `data/etp/lc-pricing-accuracy.csv` | LC problem subset — avail vs 48h |
| `data/etp/lc-pricing-accuracy-pivot.csv` | Available vs 48h side-by-side |
| `data/etp/sarima-vs-etp-volatile.csv` | Per-load ETP vs SARIMA pp50 @ book on volatile cohort |
| `data/etp/sarima-vs-etp-volatile-summary.json` | Segment MAE comparison (path shape, attribution) |
| `notebooks/etp-slider/story/leadtime-change-ops.ipynb` | Reproducible analysis notebook |
"""


__all__ = [
    "DEFAULT_AVAIL_END",
    "DEFAULT_AVAIL_START",
    "LC_CATEGORY",
    "LC_CONFIDENCE_SEGMENTS",
    "LEADTIME_ARCHETYPE_LABELS",
    "LEADTIME_ARCHETYPE_ORDER",
    "add_path_checkpoints",
    "archetype_segment_summary",
    "build_lc_cohort",
    "build_lc_exemplars",
    "build_leadtime_archetype_breakdown",
    "build_leadtime_attribution_flags",
    "build_leadtime_attribution_summary",
    "build_leadtime_confidence_breakdown",
    "classify_path_archetypes",
    "detect_target_pullbacks",
    "endpoint_category_shares",
    "enrich_book_time_quotes",
    "enrich_lc_davis_quotes",
    "enrich_lc_mde",
    "enrich_lc_mde_events",
    "enrich_path_archetypes_from_indexed",
    "export_lc_analysis",
    "filter_leadtime_attribution_cohort",
    "index_secondary_impact",
    "join_lc_carrier_cost",
    "lc_pricing_accuracy_comparison",
    "lc_pricing_accuracy_pivot",
    "leadtime_archetype_pricing_pivot",
    "leadtime_confidence_pricing_pivot",
    "leadtime_pricing_accuracy_by_archetype",
    "leadtime_pricing_accuracy_by_confidence",
    "leadtime_pricing_accuracy_by_volatility",
    "leadtime_pricing_accuracy_pivot",
    "load_mde_enrichment",
    "load_mde_events_for_cohort",
    "mde_archetype_cross_tab",
    "mde_cross_tab",
    "profile_lc_cohort",
    "render_lc_readout",
    "sarima_dial_context",
    "secondary_flags_summary",
]

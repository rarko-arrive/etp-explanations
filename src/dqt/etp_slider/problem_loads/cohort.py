"""Labeled long-lead cohort for executive comparisons (parent / problem / stable)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import polars as pl

from dqt.etp_slider.drift.report import CATEGORY_LABELS, CATEGORY_ORDER
from dqt.etp_slider.movement.orders import (
    LEGACY_LEADTIME_CATEGORY,
    normalize_movement_category,
)
from dqt.etp_slider.problem_loads.leadtime import PATH_MARKS as ARCHETYPE_PATH_MARKS
from dqt.etp_slider.problem_loads.tail import (
    LEAD_MAX_DAYS_DEFAULT,
    LEAD_MIN_DAYS_DEFAULT,
    TAIL_PCT_DEFAULT,
    TailThresholdMode,
    assign_movement_category,
    enrich_davis_flags,
    filter_lead_window,
    flag_tail_loads,
    format_tail_threshold,
    per_load_checkpoint_shifts,
)
from dqt.etp_slider.sql import (
    SHIP_DATE_END,
    SHIP_DATE_START,
    SQL_DIR,
    render_etp_slider_sql,
)

ShiftSegment = Literal["parent", "problem", "stable"]


def _normalize_category_col(df: pl.DataFrame) -> pl.DataFrame:
    if "primary_category" not in df.columns:
        return df
    return df.with_columns(
        pl.col("primary_category")
        .map_elements(normalize_movement_category, return_dtype=pl.Utf8)
        .alias("primary_category")
    )


def build_labeled_cohort(
    *,
    per_load: pl.DataFrame | None = None,
    per_load_path: Path | str | None = None,
    hist: pl.DataFrame | None = None,
    hist_path: Path | str | None = None,
    davis_path: Path | str | None = None,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    avail_start: date | str | None = None,
    avail_end: date | str | None = None,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = None,
    threshold_mode: TailThresholdMode = "scaled",
    with_checkpoints: bool = True,
) -> pl.DataFrame:
    """Full lead-window cohort with ``is_tail``, movement labels, and shift segment."""
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
    labeled = flag_tail_loads(
        cohort,
        pct_threshold=pct_threshold,
        amt_threshold=amt_threshold,
        mode=threshold_mode,
    )

    if davis_path is not None:
        davis = enrich_davis_flags(pl.read_parquet(davis_path))
        flag_cols = [c for c in davis.columns if c != "loadnumber"]
        labeled = labeled.join(davis.select("loadnumber", *flag_cols), on="loadnumber", how="left")
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
            if col in labeled.columns:
                labeled = labeled.with_columns(pl.col(col).fill_null(0).cast(pl.Int8))
        from dqt.etp_slider.drift.report import (
            load_feature_snapshots,
            resolve_feature_snapshots_path,
        )
        from dqt.etp_slider.movement.feature_path import apply_feature_path_upgrades

        feat_path = resolve_feature_snapshots_path(Path(davis_path).parent)
        if feat_path is not None and "clocks_moved_ind" in labeled.columns:
            # Path scan is expensive; volatile (tail) loads drive report attribution.
            scan_mask = (pl.col("clocks_moved_ind") == 1) & pl.col("is_tail")
            scan_ids = labeled.filter(scan_mask)["loadnumber"].to_list()
            if scan_ids:
                feat = load_feature_snapshots(feat_path, scan_ids)
                labeled = apply_feature_path_upgrades(labeled, feat)
        labeled = assign_movement_category(labeled)
    else:
        labeled = labeled.with_columns(pl.lit("Unclassified").alias("primary_category"))

    labeled = _normalize_category_col(labeled).with_columns(
        pl.when(pl.col("is_tail")).then(pl.lit("problem")).otherwise(pl.lit("stable")).alias("shift_segment"),
        pl.col("primary_category").replace(CATEGORY_LABELS).alias("category_label"),
    )

    if with_checkpoints and (hist is not None or hist_path is not None):
        if hist is None:
            hist = pl.read_parquet(hist_path)
        checkpoints = per_load_checkpoint_shifts(hist, labeled.select("loadnumber"), marks=ARCHETYPE_PATH_MARKS)
        labeled = labeled.join(checkpoints, on="loadnumber", how="left")

    return labeled


def shift_magnitude_stats(df: pl.DataFrame) -> dict[str, Any]:
    """Mean / median / spread on ETP50 avail→48hr shift."""
    if df.is_empty():
        return {"n_loads": 0}
    amt = df["etp50_shift_amt"].drop_nulls()
    pct = df["etp50_shift_pct"].drop_nulls()
    out: dict[str, Any] = {"n_loads": df.height}
    if amt.len():
        out.update(
            {
                "avg_shift_amt": float(amt.mean()),
                "med_shift_amt": float(amt.median()),
                "p75_shift_amt": float(amt.quantile(0.75)),
                "p90_shift_amt": float(amt.quantile(0.90)),
            }
        )
    if pct.len():
        out.update(
            {
                "avg_shift_pct": float(pct.mean()),
                "med_shift_pct": float(pct.median()),
                "p75_shift_pct": float(pct.quantile(0.75)),
                "p90_shift_pct": float(pct.quantile(0.90)),
            }
        )
    return out


def segment_overview(labeled: pl.DataFrame) -> pl.DataFrame:
    """Parent vs ≥10% problem vs stable — counts and shift magnitude."""
    n_parent = labeled.height
    rows: list[dict[str, Any]] = []
    for segment, filt in (
        ("parent", pl.lit(True)),
        ("problem", pl.col("is_tail")),
        ("stable", ~pl.col("is_tail")),
    ):
        sub = labeled.filter(filt)
        stats = shift_magnitude_stats(sub)
        rows.append(
            {
                "segment": segment,
                "n_loads": stats.get("n_loads", 0),
                "pct_of_parent": round(stats.get("n_loads", 0) / n_parent, 4) if n_parent else 0.0,
                **{k: v for k, v in stats.items() if k != "n_loads"},
            }
        )
    return pl.DataFrame(rows)


def category_comparison(labeled: pl.DataFrame) -> pl.DataFrame:
    """Problem loads by movement category vs stable baseline and parent cohort."""
    n_parent = labeled.height
    n_problem = int(labeled.filter(pl.col("is_tail")).height)
    stable = labeled.filter(~pl.col("is_tail"))
    stable_avg_amt = float(stable["etp50_shift_amt"].mean() or 0) if stable.height else 0.0
    stable_avg_pct = float(stable["etp50_shift_pct"].mean() or 0) if stable.height else 0.0
    parent_avg_amt = float(labeled["etp50_shift_amt"].mean() or 0)

    problem = labeled.filter(pl.col("is_tail"))
    stats = problem.group_by("primary_category").agg(
        pl.len().alias("n_loads"),
        pl.col("etp50_shift_amt").mean().alias("avg_shift_amt"),
        pl.col("etp50_shift_amt").median().alias("med_shift_amt"),
        pl.col("etp50_shift_pct").mean().alias("avg_shift_pct"),
        pl.col("etp50_shift_pct").median().alias("med_shift_pct"),
        pl.col("etp50_shift_amt").quantile(0.75).alias("p75_shift_amt"),
    )
    by_cat = {r["primary_category"]: r for r in stats.iter_rows(named=True)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in CATEGORY_ORDER:
        if cat not in by_cat:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        avg_amt = float(row["avg_shift_amt"] or 0)
        avg_pct = float(row["avg_shift_pct"] or 0)
        rows.append(
            {
                "primary_category": cat,
                "category_label": CATEGORY_LABELS.get(cat, cat),
                "n_loads": k,
                "pct_of_problem": round(k / n_problem, 4) if n_problem else 0.0,
                "pct_of_parent": round(k / n_parent, 4) if n_parent else 0.0,
                "avg_shift_amt": round(avg_amt, 2),
                "med_shift_amt": round(float(row["med_shift_amt"] or 0), 2),
                "p75_shift_amt": round(float(row["p75_shift_amt"] or 0), 2),
                "avg_shift_pct": round(avg_pct, 4),
                "med_shift_pct": round(float(row["med_shift_pct"] or 0), 4),
                "lift_amt_vs_stable": round(avg_amt - stable_avg_amt, 2),
                "lift_pct_vs_stable": round(avg_pct - stable_avg_pct, 4),
                "lift_amt_vs_parent": round(avg_amt - parent_avg_amt, 2),
            }
        )
        seen.add(cat)
    for cat in sorted(by_cat):
        if cat in seen:
            continue
        row = by_cat[cat]
        k = int(row["n_loads"])
        avg_amt = float(row["avg_shift_amt"] or 0)
        rows.append(
            {
                "primary_category": cat,
                "category_label": CATEGORY_LABELS.get(cat, cat),
                "n_loads": k,
                "pct_of_problem": round(k / n_problem, 4) if n_problem else 0.0,
                "pct_of_parent": round(k / n_parent, 4) if n_parent else 0.0,
                "avg_shift_amt": round(avg_amt, 2),
                "med_shift_amt": round(float(row["med_shift_amt"] or 0), 2),
                "p75_shift_amt": round(float(row["p75_shift_amt"] or 0), 2),
                "avg_shift_pct": round(float(row["avg_shift_pct"] or 0), 4),
                "med_shift_pct": round(float(row["med_shift_pct"] or 0), 4),
                "lift_amt_vs_stable": round(avg_amt - stable_avg_amt, 2),
                "lift_pct_vs_stable": round(float(row["avg_shift_pct"] or 0) - stable_avg_pct, 4),
                "lift_amt_vs_parent": round(avg_amt - parent_avg_amt, 2),
            }
        )
    return pl.DataFrame(rows)


def segment_by_category_overview(labeled: pl.DataFrame) -> pl.DataFrame:
    """Segment × movement category — same stats as :func:`segment_overview`, per label."""
    if labeled.is_empty() or "primary_category" not in labeled.columns:
        return pl.DataFrame()

    n_parent = labeled.height
    cat_sizes = {
        r["primary_category"]: int(r["n_category"])
        for r in labeled.group_by("primary_category").len().rename({"len": "n_category"}).iter_rows(named=True)
    }
    present = set(cat_sizes)
    categories = [c for c in CATEGORY_ORDER if c in present]
    categories.extend(sorted(present - set(categories)))

    rows: list[dict[str, Any]] = []
    for cat in categories:
        cat_df = labeled.filter(pl.col("primary_category") == cat)
        n_cat = cat_sizes.get(cat, 0)
        for segment, filt in (
            ("parent", pl.lit(True)),
            ("problem", pl.col("is_tail")),
            ("stable", ~pl.col("is_tail")),
        ):
            sub = cat_df.filter(filt)
            stats = shift_magnitude_stats(sub)
            n = int(stats.get("n_loads", 0))
            rows.append(
                {
                    "segment": segment,
                    "primary_category": cat,
                    "category_label": CATEGORY_LABELS.get(cat, cat),
                    "n_loads": n,
                    "pct_of_parent": round(n / n_parent, 4) if n_parent else 0.0,
                    "pct_of_category": round(n / n_cat, 4) if n_cat else 0.0,
                    **{k: v for k, v in stats.items() if k != "n_loads"},
                }
            )
    return pl.DataFrame(rows)


def segment_by_category_pivot(labeled: pl.DataFrame) -> pl.DataFrame:
    """One row per category — parent / problem / stable counts and avg $ side by side."""
    long = segment_by_category_overview(labeled)
    if long.is_empty():
        return long

    rows: list[dict[str, Any]] = []
    for cat in long["primary_category"].unique().to_list():
        sub = long.filter(pl.col("primary_category") == cat)
        label = sub["category_label"][0]
        row: dict[str, Any] = {"primary_category": cat, "category_label": label}
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

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    rows.sort(key=lambda r: order.get(r["primary_category"], 99))
    return pl.DataFrame(rows)


def executive_summary(
    labeled: pl.DataFrame,
    *,
    avail_start: str | None = None,
    avail_end: str | None = None,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = None,
    threshold_mode: TailThresholdMode = "scaled",
) -> dict[str, Any]:
    """Leadership tables + JSON-serializable headline stats."""
    overview = segment_overview(labeled)
    by_category = category_comparison(labeled)
    by_segment_category = segment_by_category_overview(labeled)
    by_segment_category_pivot = segment_by_category_pivot(labeled)
    n_parent = labeled.height
    n_problem = int(labeled.filter(pl.col("is_tail")).height)
    rule = format_tail_threshold(
        pct_threshold=pct_threshold,
        amt_threshold=amt_threshold,
        mode=threshold_mode,
    )
    parent_row = overview.filter(pl.col("segment") == "parent").to_dicts()[0]
    problem_row = overview.filter(pl.col("segment") == "problem").to_dicts()[0]
    stable_row = overview.filter(pl.col("segment") == "stable").to_dicts()[0]
    lc = by_category.filter(pl.col("primary_category") == "LeadtimeChange")
    lc_n = int(lc["n_loads"][0]) if lc.height else 0
    lc_avg = float(lc["avg_shift_amt"][0]) if lc.height else 0.0

    headlines = {
        "cohort_rule": f"{lead_min:.0f}–{lead_max:.0f} day lead, avail {avail_start} → {avail_end}",
        "problem_rule": rule,
        "n_parent": n_parent,
        "n_problem": n_problem,
        "pct_problem_of_parent": round(n_problem / n_parent, 4) if n_parent else 0.0,
        "parent_avg_shift_pct": parent_row.get("avg_shift_pct"),
        "parent_avg_shift_amt": parent_row.get("avg_shift_amt"),
        "problem_avg_shift_pct": problem_row.get("avg_shift_pct"),
        "problem_avg_shift_amt": problem_row.get("avg_shift_amt"),
        "stable_avg_shift_pct": stable_row.get("avg_shift_pct"),
        "stable_avg_shift_amt": stable_row.get("avg_shift_amt"),
        "leadtime_change_n": lc_n,
        "leadtime_change_avg_amt": lc_avg,
    }
    return {
        "headlines": headlines,
        "overview": overview,
        "by_category": by_category,
        "by_segment_category": by_segment_category,
        "by_segment_category_pivot": by_segment_category_pivot,
    }


def render_executive_markdown(summary: dict[str, Any]) -> str:
    """One-page exec readout from :func:`executive_summary`."""
    h = summary["headlines"]
    overview: pl.DataFrame = summary["overview"]
    by_cat: pl.DataFrame = summary["by_category"]
    pivot: pl.DataFrame = summary.get("by_segment_category_pivot", pl.DataFrame())

    def _pct(x: float | None) -> str:
        return f"{x * 100:.1f}%" if x is not None else "—"

    def _amt(x: float | None) -> str:
        return f"${x:,.0f}" if x is not None else "—"

    cat_lines = "\n".join(
        f"| {r['category_label']} | {r['n_loads']:,} | {_pct(r['pct_of_parent'])} | "
        f"{_pct(r['pct_of_problem'])} | {_amt(r['avg_shift_amt'])} | {_pct(r['avg_shift_pct'])} | "
        f"{_amt(r['lift_amt_vs_stable'])} |"
        for r in by_cat.iter_rows(named=True)
    )
    seg_lines = "\n".join(
        f"| {r['segment']} | {r['n_loads']:,} | {_pct(r['pct_of_parent'])} | "
        f"{_amt(r.get('avg_shift_amt'))} | {_pct(r.get('avg_shift_pct'))} | "
        f"{_amt(r.get('med_shift_amt'))} | {_pct(r.get('med_shift_pct'))} | "
        f"{_amt(r.get('p75_shift_amt'))} |"
        for r in overview.iter_rows(named=True)
    )
    pivot_lines = (
        "\n".join(
            f"| {r['category_label']} | {int(r.get('n_parent', 0)):,} | {_amt(r.get('avg_amt_parent'))} | "
            f"{int(r.get('n_problem', 0)):,} | {_amt(r.get('avg_amt_problem'))} | "
            f"{int(r.get('n_stable', 0)):,} | {_amt(r.get('avg_amt_stable'))} | {_pct(r.get('pct_category_problem'))} |"
            for r in pivot.iter_rows(named=True)
        )
        if not pivot.is_empty()
        else ""
    )
    return f"""# ETP Drift — Executive Summary

**Cohort:** {h["cohort_rule"]}
**Problem-load gate:** {h["problem_rule"]}

## Headline

- **{h["n_parent"]:,}** long-lead shipments in window; **{h["n_problem"]:,}** ({_pct(h["pct_problem_of_parent"])}) exceed the problem-load threshold.
- **Whole cohort** avg ETP50 drift: {_pct(h["parent_avg_shift_pct"])} / {_amt(h["parent_avg_shift_amt"])}.
- **Stable** (&lt; threshold): {_pct(h["stable_avg_shift_pct"])} / {_amt(h["stable_avg_shift_amt"])}.
- **Problem** (≥ threshold): {_pct(h["problem_avg_shift_pct"])} / {_amt(h["problem_avg_shift_amt"])}.
- **Lead Time Change** (primary label on problem loads): **{h["leadtime_change_n"]:,}** loads, avg {_amt(h["leadtime_change_avg_amt"])}.

## Segment comparison

| Segment | Loads | Share | Avg $ | Avg % | Med $ | Med % | P75 $ |
|---------|------:|------:|------:|------:|------:|------:|------:|
{seg_lines}

## By movement category × segment

| Category | Parent n | Parent avg $ | Problem n | Problem avg $ | Stable n | Stable avg $ | % of label ≥10% |
|----------|--------:|-------------:|----------:|--------------:|---------:|-------------:|----------------:|
{pivot_lines or "| — | — | — | — | — | — | — | — |"}

*Within each movement label, **% of label ≥10%** = share of that label's loads that exceed the problem threshold.*

## Problem loads by movement category

| Category | Loads | % parent | % problem | Avg $ | Avg % | Lift $ vs stable |
|----------|------:|---------:|----------:|------:|------:|-----------------:|
{cat_lines}

*Lift $ vs stable* = category avg shift minus stable-segment avg (descriptive, not causal).
"""


def export_executive_artifacts(
    labeled: pl.DataFrame,
    out_dir: Path | str,
    *,
    avail_start: str | None = None,
    avail_end: str | None = None,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    pct_threshold: float | None = TAIL_PCT_DEFAULT,
    amt_threshold: float | None = None,
    threshold_mode: TailThresholdMode = "scaled",
    write_labeled_parquet: bool = True,
    write_problem_only: bool = True,
) -> dict[str, Any]:
    """Write labeled cohort parquet + CSV/JSON exec tables."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = executive_summary(
        labeled,
        avail_start=avail_start,
        avail_end=avail_end,
        lead_min=lead_min,
        lead_max=lead_max,
        pct_threshold=pct_threshold,
        amt_threshold=amt_threshold,
        threshold_mode=threshold_mode,
    )
    paths: dict[str, Path] = {}
    if write_labeled_parquet:
        p = out_dir / "labeled-cohort.parquet"
        labeled.write_parquet(p, compression="zstd")
        paths["labeled_parquet"] = p
    if write_problem_only:
        p = out_dir / "problem-loads.parquet"
        labeled.filter(pl.col("is_tail")).write_parquet(p, compression="zstd")
        paths["problem_parquet"] = p
    summary["overview"].write_csv(out_dir / "exec-segment-overview.csv")
    summary["by_category"].write_csv(out_dir / "exec-by-category.csv")
    summary["by_segment_category"].write_csv(out_dir / "exec-segment-by-category.csv")
    summary["by_segment_category_pivot"].write_csv(out_dir / "exec-segment-category-pivot.csv")
    paths["segment_csv"] = out_dir / "exec-segment-overview.csv"
    paths["category_csv"] = out_dir / "exec-by-category.csv"
    paths["segment_category_csv"] = out_dir / "exec-segment-by-category.csv"
    paths["segment_category_pivot_csv"] = out_dir / "exec-segment-category-pivot.csv"
    json_path = out_dir / "exec-summary.json"
    json_path.write_text(
        json.dumps(
            {
                "headlines": summary["headlines"],
                "overview": summary["overview"].to_dicts(),
                "by_category": summary["by_category"].to_dicts(),
                "by_segment_category": summary["by_segment_category"].to_dicts(),
                "by_segment_category_pivot": summary["by_segment_category_pivot"].to_dicts(),
            },
            indent=2,
            default=str,
        )
    )
    paths["summary_json"] = json_path
    md_path = out_dir / "exec-readout.md"
    md_path.write_text(render_executive_markdown(summary))
    paths["readout_md"] = md_path
    return {"summary": summary, "paths": paths}


_LEADTIME_PRIMARY = frozenset({"LeadtimeChange", LEGACY_LEADTIME_CATEGORY})


def _filter_avail_window(
    per_load: pl.DataFrame,
    *,
    avail_start: date | str | None,
    avail_end: date | str | None,
) -> pl.DataFrame:
    out = per_load
    if avail_start is not None:
        start = date.fromisoformat(avail_start) if isinstance(avail_start, str) else avail_start
        out = out.filter(pl.col("made_available_utc").dt.date() >= pl.lit(start))
    if avail_end is not None:
        end = date.fromisoformat(avail_end) if isinstance(avail_end, str) else avail_end
        out = out.filter(pl.col("made_available_utc").dt.date() <= pl.lit(end))
    return out


COHORT_UNIVERSE_SQL = SQL_DIR / "cohort-universe-counts.sql"


def cohort_universe_cache_path(
    data_dir: Path | str,
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> Path:
    slug = f"{ship_date_start}_{ship_date_end}"
    return Path(data_dir) / "etp" / f"cohort-universe-counts-{slug}.parquet"


def fetch_cohort_universe_counts(
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> pl.DataFrame:
    """Pull move counts by ``order_status_group`` from Snowflake."""
    from arriveds.snowflake import query_sf

    sql = render_etp_slider_sql(
        COHORT_UNIVERSE_SQL.read_text(),
        ship_date_start=ship_date_start,
        ship_date_end=ship_date_end,
    )
    return query_sf(sql)


def summarize_cohort_universe_counts(
    counts: pl.DataFrame,
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> dict[str, Any]:
    """Aggregate status-group rows into funnel-friendly headline stats."""
    ship_start = date.fromisoformat(ship_date_start)
    ship_end = date.fromisoformat(ship_date_end)
    ship_days = (ship_end - ship_start).days + 1
    by_status = counts.sort("n_loads", descending=True).to_dicts()
    n_all = int(counts["n_loads"].sum())
    n_covered = int(counts.filter(pl.col("order_status_group").str.to_lowercase() == "covered")["n_loads"].sum())
    daily_all = round(n_all / ship_days) if ship_days else 0
    daily_covered = round(n_covered / ship_days) if ship_days else 0
    return {
        "ship_date_start": ship_date_start,
        "ship_date_end": ship_date_end,
        "ship_days": ship_days,
        "n_all_moves": n_all,
        "n_covered": n_covered,
        "daily_all_moves": daily_all,
        "daily_covered": daily_covered,
        "by_status": by_status,
    }


def load_cohort_universe_summary(
    path: Path | str | None = None,
    *,
    data_dir: Path | str | None = None,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> dict[str, Any] | None:
    """Load cached universe summary, or None if missing."""
    if path is None:
        if data_dir is None:
            return None
        path = cohort_universe_cache_path(data_dir, ship_date_start=ship_date_start, ship_date_end=ship_date_end)
    path = Path(path)
    if not path.exists():
        return None
    counts = pl.read_parquet(path)
    return summarize_cohort_universe_counts(counts, ship_date_start=ship_date_start, ship_date_end=ship_date_end)


def write_cohort_universe_cache(
    counts: pl.DataFrame,
    path: Path | str,
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> dict[str, Any]:
    """Write status-group counts parquet and return summary dict."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts.write_parquet(path)
    return summarize_cohort_universe_counts(counts, ship_date_start=ship_date_start, ship_date_end=ship_date_end)


def build_cohort_funnel(
    labeled: pl.DataFrame,
    per_load: pl.DataFrame,
    *,
    avail_start: date | str | None = None,
    avail_end: date | str | None = None,
    lead_min: float = LEAD_MIN_DAYS_DEFAULT,
    lead_max: float = LEAD_MAX_DAYS_DEFAULT,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
    company_moves_per_day: tuple[int, int] = (5000, 10000),
    universe_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executive funnel: company moves → mart → report cohort → problem loads."""
    ship_start = date.fromisoformat(ship_date_start or SHIP_DATE_START)
    ship_end = date.fromisoformat(ship_date_end or SHIP_DATE_END)
    ship_days = (ship_end - ship_start).days + 1
    hi_h = lead_max * 24

    mart = _filter_avail_window(per_load, avail_start=avail_start, avail_end=avail_end)
    n_mart = mart.height
    n_report = labeled.height
    problem = labeled.filter(pl.col("is_tail"))
    n_problem = problem.height

    lo, hi = company_moves_per_day
    n_company_lo = lo * ship_days
    n_company_hi = hi * ship_days
    n_company_mid = (n_company_lo + n_company_hi) // 2
    use_universe = bool(universe_summary and universe_summary.get("n_all_moves"))
    n_company = int(universe_summary["n_all_moves"]) if use_universe else n_company_mid
    daily_company = (
        int(universe_summary["daily_all_moves"])
        if use_universe
        else round(n_company_mid / ship_days)
        if ship_days
        else 0
    )
    n_covered = int(universe_summary.get("n_covered") or 0) if use_universe else None
    daily_covered = int(universe_summary.get("daily_covered") or 0) if use_universe else None

    def _pct(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    def _stage(
        stage_id: str,
        label: str,
        n: int | None,
        *,
        note: str = "",
        pct_of_mart: float | None = None,
        pct_of_prior: float | None = None,
        is_estimate: bool = False,
        n_low: int | None = None,
        n_high: int | None = None,
        avg_shift_amt: float | None = None,
        avg_shift_pct: float | None = None,
        avg_etp50_avail: float | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": stage_id,
            "label": label,
            "note": note,
            "is_estimate": is_estimate,
        }
        if is_estimate:
            row["n_low"] = n_low
            row["n_high"] = n_high
            row["n"] = n
        else:
            row["n"] = n
        if pct_of_mart is not None:
            row["pct_of_mart"] = pct_of_mart
        if pct_of_prior is not None:
            row["pct_of_prior"] = pct_of_prior
        if avg_shift_amt is not None:
            row["avg_shift_amt"] = avg_shift_amt
        if avg_shift_pct is not None:
            row["avg_shift_pct"] = avg_shift_pct
        if avg_etp50_avail is not None:
            row["avg_etp50_avail"] = avg_etp50_avail
        return row

    def _leadtime_slice_stats(sub: pl.DataFrame) -> dict[str, Any]:
        if sub.is_empty():
            return {"n": 0}
        stats = shift_magnitude_stats(sub)
        avg_etp = (
            float(sub["etp50_avail"].mean())
            if "etp50_avail" in sub.columns and sub["etp50_avail"].drop_nulls().len()
            else None
        )
        return {
            "n": sub.height,
            "avg_shift_amt": round(float(stats.get("avg_shift_amt") or 0), 2),
            "avg_shift_pct": round(float(stats.get("avg_shift_pct") or 0), 4),
            "avg_etp50_avail": round(avg_etp, 2) if avg_etp is not None else None,
        }

    lc_all = problem.filter(pl.col("primary_category").is_in(_LEADTIME_PRIMARY))
    if "leadtime_isolated_ind" in lc_all.columns:
        lc_hc_df = lc_all.filter(pl.col("leadtime_isolated_ind") == 1)
    elif "index_change_ind" in lc_all.columns:
        lc_hc_df = lc_all.filter(pl.col("index_change_ind").fill_null(0) == 0)
    else:
        lc_hc_df = lc_all
    lc_idx_df = (
        lc_all.filter(pl.col("index_change_ind") == 1)
        if "index_change_ind" in lc_all.columns
        else lc_all.filter(pl.lit(False))
    )
    lc_hc = _leadtime_slice_stats(lc_hc_df)
    lc_idx = _leadtime_slice_stats(lc_idx_df)
    n_lc = int(lc_all.height)

    def _shift_note(prefix: str, pack: dict[str, Any]) -> str:
        parts = [prefix]
        if pack.get("avg_shift_amt") is not None:
            pct = pack.get("avg_shift_pct")
            pct_s = f" (+{pct * 100:.1f}%)" if pct is not None else ""
            parts.append(f"avg shift ${pack['avg_shift_amt']:,.0f}{pct_s}")
        if pack.get("avg_etp50_avail") is not None:
            parts.append(f"ETP50 @ avail ${pack['avg_etp50_avail']:,.0f}")
        return " · ".join(parts)

    stages = [
        _stage(
            "company",
            "All company moves" if use_universe else "Company moves (est.)",
            n_company,
            note=(
                f"~{daily_company:,}/day · {ship_days} ship days · core.loads by ship_date"
                if use_universe
                else f"~{lo:,}–{hi:,}/day · {ship_days} ship days (estimate)"
            ),
            is_estimate=not use_universe,
            n_low=None if use_universe else n_company_lo,
            n_high=None if use_universe else n_company_hi,
        ),
        _stage(
            "mart",
            "Mart population",
            n_mart,
            note="Covered TL, clean ETP history, stable appointments",
            pct_of_prior=_pct(n_mart, n_company),
        ),
        _stage(
            "report",
            f"Report cohort ({lead_min:.0f}–{lead_max:.0f}d lead)",
            n_report,
            note=f"~{round(n_report / ship_days):,}/day" if ship_days else "",
            pct_of_mart=_pct(n_report, n_mart),
            pct_of_prior=_pct(n_report, n_mart),
        ),
        _stage(
            "problem",
            "Volatile loads",
            n_problem,
            note="ETP50 avail→48hr shift ≥ problem threshold",
            pct_of_mart=_pct(n_problem, n_mart),
            pct_of_prior=_pct(n_problem, n_report),
        ),
        _stage(
            "lc_high_confidence",
            "Lead Time — high confidence",
            lc_hc.get("n", 0),
            note=_shift_note("Clock-only, no ≥10% index drift", lc_hc),
            pct_of_mart=_pct(lc_hc.get("n", 0), n_mart),
            pct_of_prior=_pct(lc_hc.get("n", 0), n_problem),
            avg_shift_amt=lc_hc.get("avg_shift_amt"),
            avg_shift_pct=lc_hc.get("avg_shift_pct"),
            avg_etp50_avail=lc_hc.get("avg_etp50_avail"),
        ),
        _stage(
            "lc_index_cooccur",
            "Lead Time + index co-occur",
            lc_idx.get("n", 0),
            note=_shift_note("Primary LC + DAT/fuel/lag7 ≥10% endpoint drift", lc_idx),
            pct_of_mart=_pct(lc_idx.get("n", 0), n_mart),
            pct_of_prior=_pct(lc_idx.get("n", 0), n_problem),
            avg_shift_amt=lc_idx.get("avg_shift_amt"),
            avg_shift_pct=lc_idx.get("avg_shift_pct"),
            avg_etp50_avail=lc_idx.get("avg_etp50_avail"),
        ),
    ]

    hrs = mart["booking_window_hrs"]
    band_lt7 = int(mart.filter(hrs < 7 * 24).height)
    band_7_14 = int(mart.filter(hrs.is_between(7 * 24, hi_h, closed="both")).height)
    band_gt14 = int(mart.filter(hrs > hi_h).height)
    mart_lead_bands = [
        {"band": "<7d", "n": band_lt7, "pct_of_mart": _pct(band_lt7, n_mart)},
        {"band": f"{lead_min:.0f}–{lead_max:.0f}d", "n": band_7_14, "pct_of_mart": _pct(band_7_14, n_mart)},
        {"band": f">{lead_max:.0f}d", "n": band_gt14, "pct_of_mart": _pct(band_gt14, n_mart)},
    ]

    return {
        "ship_days": ship_days,
        "ship_window": f"{ship_start.isoformat()} → {ship_end.isoformat()}",
        "stages": stages,
        "leadtime_slices": {
            "n_lc_primary": n_lc,
            "high_confidence": lc_hc,
            "index_cooccur": lc_idx,
        },
        "mart_lead_bands": mart_lead_bands,
        "bar_scale_n": n_mart,
        "universe_summary": (
            {
                "n_all_moves": n_company,
                "n_covered": n_covered,
                "daily_all_moves": daily_company,
                "daily_covered": daily_covered,
                "pct_mart_of_all_moves": _pct(n_mart, n_company),
                "pct_mart_of_covered": _pct(n_mart, n_covered) if n_covered else None,
                "data_backed": True,
            }
            if use_universe
            else None
        ),
    }


__all__ = [
    "build_cohort_funnel",
    "build_labeled_cohort",
    "category_comparison",
    "cohort_universe_cache_path",
    "executive_summary",
    "export_executive_artifacts",
    "fetch_cohort_universe_counts",
    "load_cohort_universe_summary",
    "render_executive_markdown",
    "segment_by_category_overview",
    "segment_by_category_pivot",
    "segment_overview",
    "shift_magnitude_stats",
    "summarize_cohort_universe_counts",
    "write_cohort_universe_cache",
]

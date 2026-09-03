"""Survival drift cohort — Braden apples-to-apples replication.

Uses ``available_on_last`` as Available, requires ≥24h unbooked after final
Available, drops loads at each checkpoint once booked, and picks the **last**
snapshot at/before each hours-to-pickup mark (vs mart minimum-hbp ≥ mark).

Snowflake pulls default to **one ship-month per query** under
``etp_lake/survival/{history,endpoints}/ship_month=YYYY-MM/`` (MacBook-safe).
Reports read those partitions (or legacy flat parquets) and filter history to
the report cohort loadnumbers only.
"""

from __future__ import annotations

import gc
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from loguru import logger

from dqt import display_path, repo_root
from dqt.etp_slider.drift.report import (
    DEFAULT_LEAD_MAX_DAYS,
    DEFAULT_LEAD_MIN_DAYS,
    MARK_HRS,
    METRICS,
    build_cohort_meta,
    build_curve,
    build_summary,
    render_drift_html,
)
from dqt.etp_slider.sql import (
    SHIP_DATE_END,
    SHIP_DATE_START,
    SURVIVAL_HISTORY_CACHE,
    SURVIVAL_PER_LOAD_CACHE,
    survival_slider_sql,
)

if TYPE_CHECKING:
    from dqt.etp_lake import LakePaths

SURVIVAL_MIN_UNBOOKED_HRS = 24
FIRST_CHECKPOINT_HRS = 168


def _lake_utils() -> tuple[type, Any, Any, Any]:
    from dqt.etp_lake import (
        LakePaths,
        _clear_hive_dir,
        _month_windows,
        _write_parquet_zstd,
    )

    return LakePaths, _clear_hive_dir, _month_windows, _write_parquet_zstd


def months_for_survival_read(
    avail_start: date | None,
    avail_end: date | None,
    *,
    lead_max_days: float = DEFAULT_LEAD_MAX_DAYS,
) -> list[str] | None:
    """Ship-month partitions to scan for an avail window (+1 month back for ship/avail skew)."""
    if avail_start is None or avail_end is None:
        return None
    # Loads can ship in month M but post Available in month M+1 — include prior month.
    if avail_start.month == 1:
        start = date(avail_start.year - 1, 12, 1)
    else:
        start = date(avail_start.year, avail_start.month - 1, 1)
    end_ext = avail_end + timedelta(days=int(lead_max_days))
    _, _, _month_windows, _ = _lake_utils()
    return [
        ship_month
        for _ws, _we, ship_month in _month_windows(
            start.isoformat(), end_ext.isoformat()
        )
    ]


def infer_ship_window_from_lake(lake: LakePaths) -> tuple[str | None, str | None]:
    """Derive ship-date span from survival hive partitions on disk."""
    months = sorted(
        p.name.split("=", 1)[-1]
        for p in lake.read_survival_endpoints.glob("ship_month=*")
        if p.is_dir()
    )
    if not months:
        return None, None
    _, _ = (int(x) for x in months[0].split("-"))
    y1, m1 = (int(x) for x in months[-1].split("-"))
    last_day = monthrange(y1, m1)[1]
    return f"{months[0]}-01", f"{months[-1]}-{last_day:02d}"


def _partition_paths(base: Path, ship_months: list[str] | None) -> list[Path]:
    if ship_months:
        paths = [base / f"ship_month={m}" / "part-0.parquet" for m in ship_months]
        return [p for p in paths if p.exists()]
    return sorted(base.glob("ship_month=*/*.parquet"))


def survival_partitions_exist(lake: LakePaths) -> bool:
    return bool(_partition_paths(lake.read_survival_endpoints, None))


def load_survival_per_load(
    *,
    lake: LakePaths | None = None,
    per_load_path: Path | None = None,
    ship_months: list[str] | None = None,
) -> pl.DataFrame:
    """Load survival endpoints (one row per load, deduped)."""
    if lake is not None and survival_partitions_exist(lake):
        paths = _partition_paths(lake.read_survival_endpoints, ship_months)
        if not paths:
            raise FileNotFoundError(
                f"no survival endpoint partitions under {lake.read_survival_endpoints}"
            )
        return (
            pl.concat([pl.read_parquet(p) for p in paths], how="vertical_relaxed")
            .unique("loadnumber", keep="first")
        )
    if per_load_path is None or not per_load_path.exists():
        raise FileNotFoundError(
            "survival per_load missing — run pull_survival_caches or pass per_load_path"
        )
    return pl.read_parquet(per_load_path)


def load_survival_history(
    cohort_ids: pl.DataFrame,
    *,
    lake: LakePaths | None = None,
    hist_path: Path | None = None,
    ship_months: list[str] | None = None,
) -> pl.DataFrame:
    """Load model+audit history for cohort loadnumbers only."""
    ids = cohort_ids.lazy()
    if lake is not None and _partition_paths(lake.read_survival_history, None):
        paths = _partition_paths(lake.read_survival_history, ship_months)
        if not paths:
            raise FileNotFoundError(
                f"no survival history partitions under {lake.read_survival_history}"
            )
        lf = pl.concat([pl.scan_parquet(p) for p in paths], how="vertical_relaxed")
        return lf.join(ids, on="loadnumber", how="inner").collect()
    if hist_path is None or not hist_path.exists():
        raise FileNotFoundError(
            "survival history missing — run pull_survival_caches or pass hist_path"
        )
    return (
        pl.scan_parquet(hist_path)
        .join(ids, on="loadnumber", how="inner")
        .collect()
    )


def pull_survival_caches(
    cache_dir: Path,
    *,
    lake_root: Path | None = None,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
    ship_month: str | None = None,
    force: bool = False,
    chunked: bool = True,
    write_flat: bool = False,
) -> dict[str, Any]:
    """Pull survival data from Snowflake.

    Default ``chunked=True`` runs **one query per ship-month** into
    ``etp_lake/survival/{history,endpoints}/ship_month=YYYY-MM/part-0.parquet``.
    Set ``chunked=False`` for a legacy single-query flat pull (high SF + RAM load).
    """
    from arriveds.snowflake import query_sf

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tails = {
        "endpoints": "etp-slider-survival-per-load-select.sql",
        "history": "etp-slider-survival-history-select.sql",
    }
    if lake_root is None:
        lake_root = cache_dir.parent / "etp_lake"
    hist_dir = Path(lake_root) / "survival" / "history"
    end_dir = Path(lake_root) / "survival" / "endpoints"
    summary: dict[str, Any] = {
        "chunked": chunked,
        "history_dir": str(hist_dir),
        "endpoints_dir": str(end_dir),
        "months": {},
    }

    if ship_month is not None:
        y, m = (int(x) for x in ship_month.split("-"))
        last_day = monthrange(y, m)[1]
        windows = [(f"{ship_month}-01", f"{ship_month}-{last_day:02d}", ship_month)]
    else:
        _, _, _month_windows, _ = _lake_utils()
        windows = _month_windows(ship_date_start, ship_date_end)

    if chunked:
        _, _clear_hive_dir, _, _write_parquet_zstd = _lake_utils()
        if force:
            _clear_hive_dir(hist_dir)
            _clear_hive_dir(end_dir)
        hist_dir.mkdir(parents=True, exist_ok=True)
        end_dir.mkdir(parents=True, exist_ok=True)

        for win_start, win_end, month in windows:
            month_summary: dict[str, int] = {}
            for label, tail in tails.items():
                out_dir = end_dir if label == "endpoints" else hist_dir
                part_path = out_dir / f"ship_month={month}" / "part-0.parquet"
                if part_path.exists() and not force:
                    logger.info("skip existing {}", display_path(part_path))
                    month_summary[label] = pl.read_parquet(part_path).height
                    continue
                logger.info(
                    "pulling survival {} {} [{}, {}]",
                    label,
                    month,
                    win_start,
                    win_end,
                )
                sql = survival_slider_sql(
                    tail,
                    ship_date_start=win_start,
                    ship_date_end=win_end,
                )
                df = query_sf(sql)
                frame = df if isinstance(df, pl.DataFrame) else pl.from_pandas(df)
                _write_parquet_zstd(frame, part_path)
                month_summary[label] = frame.height
                logger.info(
                    "wrote {:,} rows → {}",
                    frame.height,
                    display_path(part_path),
                )
                del df, frame
                gc.collect()
            summary["months"][month] = month_summary

        if write_flat:
            summary["flat"] = _materialize_survival_flat(cache_dir, hist_dir, end_dir)
        return summary

    # Legacy flat pull (discouraged)
    from dqt import is_fresh
    from dqt.etp_lake import CACHE_HOURS

    written: dict[str, Path] = {}
    targets = {
        "per_load": (cache_dir / SURVIVAL_PER_LOAD_CACHE, tails["endpoints"]),
        "history": (cache_dir / SURVIVAL_HISTORY_CACHE, tails["history"]),
    }
    for name, (path, tail) in targets.items():
        if path.exists() and not force and is_fresh(path, hours=CACHE_HOURS):
            written[name] = path
            continue
        logger.warning(
            "flat survival pull [{}, {}] — prefer chunked=True",
            ship_date_start,
            ship_date_end,
        )
        sql = survival_slider_sql(
            tail,
            ship_date_start=ship_date_start,
            ship_date_end=ship_date_end,
        )
        df = query_sf(sql)
        frame = df if isinstance(df, pl.DataFrame) else pl.from_pandas(df)
        _, _, _, _write_parquet_zstd = _lake_utils()
        _write_parquet_zstd(frame, path)
        written[name] = path
        logger.info("wrote {:,} rows → {}", frame.height, display_path(path))
    summary["flat"] = written
    return summary


def _materialize_survival_flat(
    cache_dir: Path,
    hist_dir: Path,
    end_dir: Path,
) -> dict[str, str]:
    """Optional flat parquets from lake partitions (for backward compat)."""
    hist_paths = sorted(hist_dir.glob("ship_month=*/*.parquet"))
    end_paths = sorted(end_dir.glob("ship_month=*/*.parquet"))
    hist_out = cache_dir / SURVIVAL_HISTORY_CACHE
    end_out = cache_dir / SURVIVAL_PER_LOAD_CACHE
    if hist_paths:
        pl.concat([pl.read_parquet(p) for p in hist_paths], how="vertical_relaxed").write_parquet(
            hist_out, compression="zstd"
        )
    if end_paths:
        pl.concat([pl.read_parquet(p) for p in end_paths], how="vertical_relaxed").unique(
            "loadnumber", keep="first"
        ).write_parquet(end_out, compression="zstd")
    return {"history": str(hist_out), "endpoints": str(end_out)}


def survival_cohort_ids(
    per_load: pl.DataFrame,
    *,
    min_days: float = DEFAULT_LEAD_MIN_DAYS,
    max_days: float = DEFAULT_LEAD_MAX_DAYS,
    avail_start: date | None = None,
    avail_end: date | None = None,
    min_unbooked_hrs: int = SURVIVAL_MIN_UNBOOKED_HRS,
) -> pl.DataFrame:
    """Loads in lead window, unbooked ≥ min_unbooked_hrs after final Available."""
    required = {"booking_window_hrs", "made_available_utc", "booked_on_utc"}
    missing = required - set(per_load.columns)
    if missing:
        raise ValueError(f"survival per_load missing columns: {sorted(missing)}")

    lo_h = min_days * 24
    hi_h = max_days * 24
    filt = per_load.filter(
        pl.col("booking_window_hrs").is_between(lo_h, hi_h, closed="both"),
        (pl.col("booked_on_utc") - pl.col("made_available_utc")).dt.total_hours()
        >= min_unbooked_hrs,
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


def _mark_ts_expr(mark_hrs: int) -> pl.Expr:
    return pl.col("pickup_appt_latest_utc") - pl.duration(hours=mark_hrs)


def _eligible_at_mark(loads: pl.DataFrame, mark_hrs: int) -> pl.DataFrame:
    """Loadnumbers still unbooked at the checkpoint instant."""
    if mark_hrs == 999:
        return loads.select("loadnumber").unique()
    return (
        loads.with_columns(_mark_ts_expr(mark_hrs).alias("_mark_ts"))
        .filter(pl.col("booked_on_utc") > pl.col("_mark_ts"))
        .select("loadnumber")
        .unique()
    )


def _at_mark_survival(
    df: pl.DataFrame,
    col: str,
    mark_hrs: int,
    loads: pl.DataFrame,
) -> pl.DataFrame:
    """Last snapshot at/before checkpoint (Braden rule)."""
    sub = df.filter(pl.col(col).is_not_null()).join(
        loads.select("loadnumber", "made_available_utc", "pickup_appt_latest_utc"),
        on="loadnumber",
        how="inner",
    )
    if mark_hrs == 999:
        sub = sub.with_columns(
            _mark_ts_expr(FIRST_CHECKPOINT_HRS).alias("_cutoff")
        ).filter(
            pl.col("snapshot_utc") >= pl.col("made_available_utc"),
            pl.col("snapshot_utc") < pl.col("_cutoff"),
        )
        return (
            sub.sort(["loadnumber", "snapshot_utc"])
            .group_by("loadnumber")
            .agg(pl.col(col).first().alias(col))
        )

    sub = sub.with_columns(_mark_ts_expr(mark_hrs).alias("_mark_ts")).filter(
        pl.col("snapshot_utc") >= pl.col("made_available_utc"),
        pl.col("snapshot_utc") <= pl.col("_mark_ts"),
    )
    return (
        sub.sort(["loadnumber", "snapshot_utc"], descending=[False, True])
        .group_by("loadnumber")
        .agg(pl.col(col).first().alias(col))
    )


def _indexed_at_marks_survival(
    hist: pl.DataFrame,
    per_load: pl.DataFrame,
    cohort_ids: pl.DataFrame,
) -> dict[str, pl.DataFrame]:
    """Indexed values among loads still unbooked at each checkpoint."""
    loads = per_load.join(cohort_ids, on="loadnumber", how="inner")
    h = hist.join(cohort_ids, on="loadnumber", how="inner")
    model = h.filter(pl.col("source") == "model")
    audit = h.filter(pl.col("source") == "audit")
    out: dict[str, pl.DataFrame] = {}

    for col, src, key in METRICS:
        df = model if src == "model" else audit
        base = _at_mark_survival(df, col, 999, loads)
        base = base.rename({col: f"{col}_base"}).filter(pl.col(f"{col}_base") > 0)

        parts: list[pl.DataFrame] = []
        for mark in MARK_HRS:
            eligible = _eligible_at_mark(loads, mark)
            snap = _at_mark_survival(df, col, mark, loads)
            snap = snap.join(eligible, on="loadnumber", how="inner").join(
                base, on="loadnumber", how="inner"
            )
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


def _survival_meta_extras(
    meta: dict[str, Any],
    *,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
    ship_months: list[str] | None = None,
    data_source: str | None = None,
) -> None:
    meta["cohort_mode"] = "survival"
    meta["attrition_flat"] = False
    meta["attrition_title"] = "Cohort attrition — who's still on the board?"
    meta["attrition_y_label"] = "Loads still unbooked"
    meta["show_explorer"] = False
    if ship_date_start:
        meta["pull_ship_start"] = ship_date_start
    if ship_date_end:
        meta["pull_ship_end"] = ship_date_end
    if ship_months:
        meta["pull_ship_months"] = ship_months
    if data_source:
        meta["data_source"] = data_source


def build_survival_report_payload(
    hist: pl.DataFrame,
    per_load: pl.DataFrame,
    cohort_ids: pl.DataFrame,
    *,
    lead_min_days: float,
    lead_max_days: float,
    avail_start: date | None,
    avail_end: date | None,
) -> dict[str, Any]:
    indexed = _indexed_at_marks_survival(hist, per_load, cohort_ids)
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
    _survival_meta_extras(meta)
    from dqt.etp_slider.drift.report import _enrich_summary_meta

    _enrich_summary_meta(meta, summary)
    return {"summary": summary, "curve": curve, "meta": meta}


def build_survival_drift_report(
    *,
    out_path: Path,
    lake: LakePaths | None = None,
    hist_path: Path | None = None,
    per_load_path: Path | None = None,
    lead_min_days: float = DEFAULT_LEAD_MIN_DAYS,
    lead_max_days: float = DEFAULT_LEAD_MAX_DAYS,
    avail_start: date | None = None,
    avail_end: date | None = None,
    ship_date_start: str | None = None,
    ship_date_end: str | None = None,
    report_variant: str | None = None,
) -> dict[str, Any]:
    """End-to-end survival drift HTML (Braden cohort definition)."""
    use_lake = lake is not None and survival_partitions_exist(lake)
    ship_months = months_for_survival_read(
        avail_start, avail_end, lead_max_days=lead_max_days
    )
    if use_lake and not ship_date_start:
        ship_date_start, ship_date_end = infer_ship_window_from_lake(lake)  # type: ignore[arg-type]

    per_load = load_survival_per_load(
        lake=lake if use_lake else None,
        per_load_path=per_load_path,
        ship_months=ship_months,
    )
    cohort_ids = survival_cohort_ids(
        per_load,
        min_days=lead_min_days,
        max_days=lead_max_days,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    if cohort_ids.is_empty():
        raise ValueError("survival cohort empty — widen filters or pull more ship-months")

    hist = load_survival_history(
        cohort_ids,
        lake=lake if use_lake else None,
        hist_path=hist_path,
        ship_months=ship_months,
    )
    payload = build_survival_report_payload(
        hist,
        per_load,
        cohort_ids,
        lead_min_days=lead_min_days,
        lead_max_days=lead_max_days,
        avail_start=avail_start,
        avail_end=avail_end,
    )
    # Enrich with pull provenance (overwrites base survival meta fields)
    _survival_meta_extras(
        payload["meta"],
        ship_date_start=ship_date_start,
        ship_date_end=ship_date_end,
        ship_months=ship_months,
        data_source="lake_partitions" if use_lake else "flat_parquet",
    )
    from dqt.etp_slider.drift.report import apply_report_variant_meta

    apply_report_variant_meta(payload["meta"], report_variant)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_drift_html(payload))
    return {"out_path": str(out_path), "meta": payload["meta"]}


def resolve_survival_lake(
    data_dir: str | Path | None = None,
    *,
    root: Path | None = None,
) -> LakePaths:
    LakePaths, _, _, _ = _lake_utils()
    return LakePaths.resolve(data_dir=data_dir, root=root or repo_root())


__all__ = [
    "SURVIVAL_MIN_UNBOOKED_HRS",
    "build_survival_drift_report",
    "build_survival_report_payload",
    "load_survival_history",
    "load_survival_per_load",
    "months_for_survival_read",
    "pull_survival_caches",
    "resolve_survival_lake",
    "survival_cohort_ids",
    "survival_partitions_exist",
]

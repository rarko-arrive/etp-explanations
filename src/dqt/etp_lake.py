"""Offline ETP feature lake: paths, partition/stats builders, DuckDB catalog."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from loguru import logger

from dqt import CACHE_HOURS, display_path, is_fresh, repo_root, resolve_data_dir
from dqt.etp_mart import (
    DEFAULT_ANCHOR_HSA,
    DEFAULT_ANCHOR_TOLERANCE_H,
    FEATURE_HISTORY_CACHE,
    consecutive_feature_deltas,
    decomp_by_day,
    decompose_frame,
    load_feature_catalog,
    write_feature_catalog,
)
from dqt.etp_slider.sql import (
    HISTORY_CACHE,
    PER_LOAD_CACHE,
    SHIP_DATE_END,
    SHIP_DATE_START,
)
from dqt.score.constants import ID_COL

LAKE_DIRNAME = "etp_lake"
SNAPSHOT_COLS = (
    "loadnumber",
    "snapshot_utc",
    "source",
    "etp10",
    "etp50",
    "target1",
    "target3",
    "hours_before_pickup",
    "hours_since_available",
)
UNKNOWN_SHIP_MONTH = "unknown"

ETP_AGG = [
    pl.len().alias("n_calls"),
    pl.col("snapshot_utc").min().alias("first_call"),
    pl.col("snapshot_utc").max().alias("last_call"),
    pl.col("etp50").mean().alias("etp50_mean"),
    pl.col("etp10").mean().alias("etp10_mean"),
    pl.col("etp50").std().alias("etp50_std"),
    pl.col("etp10").std().alias("etp10_std"),
    pl.col("etp50").min().alias("etp50_min"),
    pl.col("etp10").min().alias("etp10_min"),
    pl.col("etp50").max().alias("etp50_max"),
    pl.col("etp10").max().alias("etp10_max"),
    pl.col("etp50").median().alias("etp50_median"),
    pl.col("etp10").median().alias("etp10_median"),
]

AUDIT_AGG = [
    pl.len().alias("n_calls"),
    pl.col("snapshot_utc").min().alias("first_call"),
    pl.col("snapshot_utc").max().alias("last_call"),
    pl.col("target1").mean().alias("t1_mean"),
    pl.col("target1").std().alias("t1_std"),
    pl.col("target1").min().alias("t1_min"),
    pl.col("target1").max().alias("t1_max"),
    pl.col("target1").median().alias("t1_median"),
    pl.col("target3").mean().alias("t3_mean"),
    pl.col("target3").std().alias("t3_std"),
    pl.col("target3").min().alias("t3_min"),
    pl.col("target3").max().alias("t3_max"),
    pl.col("target3").median().alias("t3_median"),
]

ALL_STAGES = ("pull", "partition", "stats", "leadtime", "mart", "impact", "funnel", "catalog")
DEFAULT_MIN_OLS_ROWS = 100  # match dqt.etp_impact; explain-only installs omit that module


def _require_impact():
    try:
        from dqt.etp_impact import (
            build_feature_impact,
            impact_paths,
            read_feature_impact,
        )
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "dqt.etp_impact is not bundled in etp-explanations "
            "(only needed for lake build / feature_impact API)"
        ) from exc
    return build_feature_impact, impact_paths, read_feature_impact


def _require_funnel():
    try:
        from dqt.etp_funnel import build_funnel_attrs, funnel_paths, read_funnel
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "dqt.etp_funnel is not bundled in etp-explanations "
            "(only needed for lake build / funnel API)"
        ) from exc
    return build_funnel_attrs, funnel_paths, read_funnel


def lake_root(data_dir: str | Path | None = None, *, root: Path | None = None) -> Path:
    """``$DQT_DATA_DIR/etp_lake``."""
    return resolve_data_dir(data_dir, root=root) / LAKE_DIRNAME


def resolve_lake_mirror(*, root: Path | None = None) -> Path | None:
    """Return a local-disk lake mirror when ``DQT_USE_LAKE_MIRROR`` is enabled.

    Set ``DQT_USE_LAKE_MIRROR=1`` and optionally ``DQT_LAKE_MIRROR`` (default
    ``~/dqt/etp_lake`` on local OS disk). The mirror must already exist (see
    ``scripts/vm_mirror_lake.sh``). When unset or missing, returns ``None``.
    """
    use = (os.environ.get("DQT_USE_LAKE_MIRROR") or "").strip().lower()
    if use not in ("1", "true", "yes", "on"):
        return None
    default_mirror = Path.home() / "dqt" / "etp_lake"
    raw = (os.environ.get("DQT_LAKE_MIRROR") or os.fspath(default_mirror)).strip()
    expanded = os.path.expanduser(os.path.expandvars(raw))
    p = Path(expanded)
    if not p.is_absolute():
        p = (root or repo_root()) / p
    p = p.resolve()
    if not p.exists():
        logger.warning(
            "DQT_USE_LAKE_MIRROR set but mirror missing at {} — using project lake",
            p,
        )
        return None
    return p


def default_cache_dir(*, root: Path | None = None) -> Path:
    """Legacy flat slider cache (``data/etp``)."""
    return (root or repo_root()) / "data" / "etp"


@dataclass(frozen=True)
class LakePaths:
    """Resolved paths under an etp_lake root.

    ``root`` is the project SoT under ``$DQT_DATA_DIR/etp_lake`` (rsync target,
    all writes). When ``mirror_root`` is set, ``read_*`` paths point at the
    local-disk mirror for query throughput; ``duckdb_path`` stays on ``root``.
    """

    root: Path
    data_dir: Path
    cache_dir: Path
    mirror_root: Path | None = None

    @classmethod
    def resolve(
        cls,
        data_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        *,
        root: Path | None = None,
    ) -> LakePaths:
        base = resolve_data_dir(data_dir, root=root)
        cache = Path(cache_dir) if cache_dir is not None else default_cache_dir(root=root)
        if not cache.is_absolute():
            cache = (root or repo_root()) / cache
        lake_root = base / LAKE_DIRNAME
        mirror = resolve_lake_mirror(root=root)
        if mirror is not None:
            logger.info(
                "lake mirror reads → {}  writes → {}",
                display_path(mirror, root=root),
                display_path(lake_root, root=root),
            )
        return cls(root=lake_root, data_dir=base, cache_dir=cache, mirror_root=mirror)

    @property
    def read_root(self) -> Path:
        return self.mirror_root if self.mirror_root is not None else self.root

    @property
    def using_mirror(self) -> bool:
        return self.mirror_root is not None

    @property
    def read_snapshots(self) -> Path:
        return self.read_root / "snapshots"

    @property
    def read_endpoints(self) -> Path:
        return self.read_root / "endpoints"

    @property
    def read_aggregates(self) -> Path:
        return self.read_root / "aggregates"

    @property
    def read_leadtime(self) -> Path:
        return self.read_aggregates / "leadtime"

    @property
    def read_mart(self) -> Path:
        return self.read_root / "mart"

    @property
    def read_feature_snapshots(self) -> Path:
        return self.read_mart / "feature_snapshots"

    @property
    def read_feature_deltas(self) -> Path:
        return self.read_mart / "feature_deltas"

    @property
    def read_decomp_by_day_path(self) -> Path:
        return self.read_mart / "decomp_by_day.parquet"

    @property
    def read_etp_stats_path(self) -> Path:
        return self.read_aggregates / "etp_stats.parquet"

    @property
    def read_per_load_lake(self) -> Path:
        return self.read_endpoints / "per_load.parquet"

    def read_snapshots_glob(self) -> str:
        return str(self.read_snapshots / "ship_month=*" / "*.parquet")

    def read_feature_snapshots_glob(self) -> str:
        return str(self.read_feature_snapshots / "ship_month=*" / "*.parquet")

    def read_feature_deltas_glob(self) -> str:
        return str(self.read_feature_deltas / "ship_month=*" / "*.parquet")

    @property
    def meta(self) -> Path:
        return self.root / "meta"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def endpoints(self) -> Path:
        return self.root / "endpoints"

    @property
    def aggregates(self) -> Path:
        return self.root / "aggregates"

    @property
    def leadtime(self) -> Path:
        return self.aggregates / "leadtime"

    @property
    def catalog_dir(self) -> Path:
        return self.root / "catalog"

    @property
    def duckdb_path(self) -> Path:
        return self.catalog_dir / "etp_lake.duckdb"

    @property
    def manifest_path(self) -> Path:
        return self.meta / "manifest.json"

    @property
    def schema_path(self) -> Path:
        return self.meta / "schema_snapshots.json"

    @property
    def etp_stats_path(self) -> Path:
        return self.aggregates / "etp_stats.parquet"

    @property
    def per_load_lake(self) -> Path:
        return self.endpoints / "per_load.parquet"

    @property
    def history_cache(self) -> Path:
        return self.cache_dir / HISTORY_CACHE

    @property
    def per_load_cache(self) -> Path:
        return self.cache_dir / PER_LOAD_CACHE

    @property
    def mart(self) -> Path:
        return self.root / "mart"

    @property
    def feature_catalog_path(self) -> Path:
        return self.mart / "feature_catalog.json"

    @property
    def feature_snapshots(self) -> Path:
        return self.mart / "feature_snapshots"

    @property
    def feature_deltas(self) -> Path:
        return self.mart / "feature_deltas"

    @property
    def decomp_by_day_path(self) -> Path:
        return self.mart / "decomp_by_day.parquet"

    @property
    def feature_history_cache(self) -> Path:
        return self.cache_dir / FEATURE_HISTORY_CACHE

    def snapshots_glob(self) -> str:
        return str(self.snapshots / "ship_month=*" / "*.parquet")

    def feature_snapshots_glob(self) -> str:
        return str(self.feature_snapshots / "ship_month=*" / "*.parquet")

    def feature_deltas_glob(self) -> str:
        return str(self.feature_deltas / "ship_month=*" / "*.parquet")

    @property
    def feature_impact_dir(self) -> Path:
        return self.mart / "feature_impact"

    @property
    def read_funnel_dir(self) -> Path:
        return self.read_mart / "funnel"

    @property
    def funnel_dir(self) -> Path:
        return self.mart / "funnel"

    @property
    def funnel_load_attrs_path(self) -> Path:
        return self.funnel_dir / "funnel_load_attrs.parquet"

    @property
    def funnel_rollups_path(self) -> Path:
        return self.funnel_dir / "funnel_rollups.parquet"

    @property
    def funnel_sankey_edges_path(self) -> Path:
        return self.funnel_dir / "funnel_sankey_edges.parquet"

    @property
    def survival(self) -> Path:
        return self.root / "survival"

    @property
    def survival_history(self) -> Path:
        return self.survival / "history"

    @property
    def survival_endpoints(self) -> Path:
        return self.survival / "endpoints"

    @property
    def read_survival_history(self) -> Path:
        return self.read_root / "survival" / "history"

    @property
    def read_survival_endpoints(self) -> Path:
        return self.read_root / "survival" / "endpoints"

    def survival_history_glob(self) -> str:
        return str(self.read_survival_history / "ship_month=*" / "*.parquet")

    def survival_endpoints_glob(self) -> str:
        return str(self.read_survival_endpoints / "ship_month=*" / "*.parquet")


def parse_stages(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    if raw is None:
        return ALL_STAGES
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in raw if str(p).strip()]
    unknown = [p for p in parts if p not in ALL_STAGES]
    if unknown:
        raise ValueError(f"unknown stages {unknown}; expected subset of {ALL_STAGES}")
    return tuple(parts)


def ship_month_from_pickup(expr: pl.Expr) -> pl.Expr:
    """YYYY-MM from pickup timestamp; null → unknown."""
    return (
        expr.cast(pl.Datetime(time_unit="us"), strict=False)
        .dt.strftime("%Y-%m")
        .fill_null(UNKNOWN_SHIP_MONTH)
    )


def load_ship_months(per_load: pl.LazyFrame | pl.DataFrame) -> pl.LazyFrame:
    """Map loadnumber → ship_month from per-load pickup time."""
    lf = per_load.lazy() if isinstance(per_load, pl.DataFrame) else per_load
    cols = lf.collect_schema().names()
    if "pickup_appt_latest_utc" not in cols:
        return (
            lf.select(pl.col(ID_COL))
            .unique()
            .with_columns(pl.lit(UNKNOWN_SHIP_MONTH).alias("ship_month"))
        )
    return (
        lf.select(ID_COL, "pickup_appt_latest_utc")
        .unique(subset=[ID_COL])
        .with_columns(
            ship_month_from_pickup(pl.col("pickup_appt_latest_utc")).alias("ship_month")
        )
        .select(ID_COL, "ship_month")
    )


def attach_ship_month(
    history: pl.LazyFrame | pl.DataFrame,
    per_load: pl.LazyFrame | pl.DataFrame,
) -> pl.LazyFrame:
    hist = history.lazy() if isinstance(history, pl.DataFrame) else history
    months = load_ship_months(per_load)
    return hist.join(months, on=ID_COL, how="left").with_columns(
        pl.col("ship_month").fill_null(UNKNOWN_SHIP_MONTH)
    )


def compute_etp_stats(
    history: pl.LazyFrame | pl.DataFrame,
    *,
    batch_size: int = 10_000,
) -> pl.DataFrame:
    """Per-load model + audit aggregates (outer join on loadnumber)."""
    hist = history.lazy() if isinstance(history, pl.DataFrame) else history
    model = hist.filter(pl.col("source") == "model")
    audit = hist.filter(pl.col("source") == "audit")

    load_ids = hist.select(ID_COL).unique().collect().get_column(ID_COL).to_list()
    if not load_ids:
        return pl.DataFrame()

    etp_parts: list[pl.DataFrame] = []
    audit_parts: list[pl.DataFrame] = []
    for i in range(0, len(load_ids), batch_size):
        batch = load_ids[i : i + batch_size]
        etp_parts.append(
            model.filter(pl.col(ID_COL).is_in(batch)).group_by(ID_COL).agg(ETP_AGG).collect()
        )
        audit_parts.append(
            audit.filter(pl.col(ID_COL).is_in(batch))
            .group_by(ID_COL)
            .agg(AUDIT_AGG)
            .collect()
        )

    etp_stats = pl.concat(etp_parts) if etp_parts else pl.DataFrame()
    audit_stats = pl.concat(audit_parts) if audit_parts else pl.DataFrame()
    if etp_stats.is_empty() and audit_stats.is_empty():
        return pl.DataFrame()
    if etp_stats.is_empty():
        return audit_stats
    if audit_stats.is_empty():
        return etp_stats
    return etp_stats.join(audit_stats, on=ID_COL, how="full", coalesce=True)


def _write_parquet_zstd(df: pl.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(path)


def partition_snapshots(
    history: pl.LazyFrame | pl.DataFrame,
    per_load: pl.LazyFrame | pl.DataFrame,
    out_dir: Path,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Write hive-partitioned snapshots; returns ship_month → row counts.

    Processes one ship_month at a time so the full history need not fit in RAM.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and force:
        for old in out_dir.glob("ship_month=*"):
            if old.is_dir():
                for f in old.glob("*.parquet"):
                    f.unlink()
                try:
                    old.rmdir()
                except OSError:
                    pass

    hist = history.lazy() if isinstance(history, pl.DataFrame) else history
    months = load_ship_months(per_load).collect()
    # Loads in history with no per-load row → unknown
    hist_ids = hist.select(ID_COL).unique().collect()
    covered = months.select(ID_COL)
    missing = hist_ids.join(covered, on=ID_COL, how="anti")
    if missing.height:
        months = pl.concat(
            [
                months,
                missing.with_columns(pl.lit(UNKNOWN_SHIP_MONTH).alias("ship_month")),
            ]
        )

    counts: dict[str, int] = {}
    for key_s in sorted(months.get_column("ship_month").unique().to_list()):
        ids = months.filter(pl.col("ship_month") == key_s).select(ID_COL)
        part = (
            hist.join(ids.lazy(), on=ID_COL, how="inner")
            .with_columns(pl.lit(key_s).alias("ship_month"))
            .collect()
        )
        if part.is_empty():
            continue
        part_dir = out_dir / f"ship_month={key_s}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / "part-0.parquet"
        _write_parquet_zstd(part, out)
        counts[key_s] = part.height
        logger.info("wrote {} rows → {}", part.height, out)
    return counts


def write_schema_snapshots(schema: pl.Schema, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = {name: str(dtype) for name, dtype in schema.items()}
    path.write_text(json.dumps(mapping, indent=2) + "\n")


def _git_sha(root: Path | None = None) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root or repo_root(),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "built_at": datetime.now(tz=UTC).isoformat(),
        "git_sha": _git_sha(),
        **payload,
    }
    path.write_text(json.dumps(body, indent=2, default=str) + "\n")


def pull_slider_caches(
    cache_dir: Path,
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
    force: bool = False,
    cache_hours: float = CACHE_HOURS,
) -> dict[str, Path]:
    """Pull history + per-load from Snowflake into flat cache parquets."""
    from arriveds.snowflake import query_sf

    from dqt.etp_slider.sql import slider_sql

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "history": (cache_dir / HISTORY_CACHE, "etp-slider-history-select.sql"),
        "per_load": (cache_dir / PER_LOAD_CACHE, "etp-slider-per-load-select.sql"),
    }
    written: dict[str, Path] = {}
    for name, (path, tail) in targets.items():
        if path.exists() and not force and is_fresh(path, hours=cache_hours):
            logger.info("{} fresh — skipping pull", display_path(path))
            written[name] = path
            continue
        sql = slider_sql(
            tail,
            ship_date_start=ship_date_start,
            ship_date_end=ship_date_end,
        )
        logger.info("pulling {} [{}, {}]", name, ship_date_start, ship_date_end)
        df = query_sf(sql)
        if isinstance(df, pl.DataFrame):
            frame = df
        else:
            frame = pl.from_pandas(df)
        tmp = path.with_suffix(".tmp.parquet")
        frame.write_parquet(tmp, compression="zstd")
        tmp.replace(path)
        logger.info("wrote {:,} rows → {}", frame.height, display_path(path))
        written[name] = path
    return written


def _month_windows(ship_date_start: str, ship_date_end: str) -> list[tuple[str, str, str]]:
    """Inclusive (month_start, month_end, ship_month) covering the ship-date window."""
    from calendar import monthrange
    from datetime import date, timedelta

    y, m, d = (int(x) for x in ship_date_start.split("-"))
    ey, em, ed = (int(x) for x in ship_date_end.split("-"))
    start = date(y, m, d)
    end = date(ey, em, ed)
    cur = date(start.year, start.month, 1)
    out: list[tuple[str, str, str]] = []
    while cur <= end:
        last = date(cur.year, cur.month, monthrange(cur.year, cur.month)[1])
        win_start = max(cur, start)
        win_end = min(last, end)
        out.append((win_start.isoformat(), win_end.isoformat(), cur.strftime("%Y-%m")))
        cur = last + timedelta(days=1)
    return out


def _clear_hive_dir(out_dir: Path) -> None:
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return
    for old in out_dir.glob("ship_month=*"):
        if old.is_dir():
            for f in old.glob("*.parquet"):
                f.unlink()
            try:
                old.rmdir()
            except OSError:
                pass


def pull_feature_history(
    cache_dir: Path,
    *,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
    force: bool = False,
    cache_hours: float = CACHE_HOURS,
    root: Path | None = None,
    feature_snapshots_dir: Path | None = None,
    chunked: bool = True,
) -> dict[str, Any]:
    """Pull curated feature history.

    Default ``chunked=True`` pulls **one ship-month at a time** and writes
    directly under ``feature_snapshots_dir`` (MacBook-safe). Avoids materializing
    the full multi-year history in RAM.
    """
    import gc

    from arriveds.snowflake import query_sf

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    sql_path = (root or repo_root()) / "SQL" / "etp-slider" / "etp-feature-history.sql"
    summary: dict[str, Any] = {"chunked": chunked, "months": {}}

    if chunked and feature_snapshots_dir is not None:
        out_dir = Path(feature_snapshots_dir)
        if any(out_dir.glob("ship_month=*/*.parquet")) and not force:
            logger.info(
                "{} exists — skipping chunked feature pull (--force to rebuild)",
                display_path(out_dir),
            )
            return {"skipped": True, **summary}

        if force:
            _clear_hive_dir(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        for win_start, win_end, ship_month in _month_windows(
            ship_date_start, ship_date_end
        ):
            part_dir = out_dir / f"ship_month={ship_month}"
            part_path = part_dir / "part-0.parquet"
            if part_path.exists() and not force:
                logger.info("skip existing {}", display_path(part_path))
                continue
            logger.info(
                "pulling feature history {} [{}, {}]",
                ship_month,
                win_start,
                win_end,
            )
            df = query_sf(
                    sql_path,
                    params={
                        "ship_date_start": win_start,
                        "ship_date_end": win_end,
                    },
                )
            frame = df if isinstance(df, pl.DataFrame) else pl.from_pandas(df)
            if "ship_month" not in frame.columns:
                frame = frame.with_columns(pl.lit(ship_month).alias("ship_month"))
            _write_parquet_zstd(frame, part_path)
            n = frame.height
            summary["months"][ship_month] = n
            logger.info("wrote {:,} rows → {}", n, display_path(part_path))
            del df, frame
            gc.collect()
        return summary

    # Legacy flat cache (high memory — prefer chunked)
    path = cache_dir / FEATURE_HISTORY_CACHE
    if path.exists() and not force and is_fresh(path, hours=cache_hours):
        logger.info("{} fresh — skipping feature-history pull", display_path(path))
        return {"path": str(path), "skipped": True}

    logger.warning(
        "flat feature-history pull [{}, {}] — prefer chunked=True for MacBook RAM",
        ship_date_start,
        ship_date_end,
    )
    df = query_sf(
        sql_path,
        params={
            "ship_date_start": ship_date_start,
            "ship_date_end": ship_date_end,
        },
    )
    frame = df if isinstance(df, pl.DataFrame) else pl.from_pandas(df)
    tmp = path.with_suffix(".tmp.parquet")
    frame.write_parquet(tmp, compression="zstd")
    tmp.replace(path)
    logger.info("wrote {:,} rows → {}", frame.height, display_path(path))
    return {"path": str(path), "n_rows": frame.height}


def build_analytics_mart(
    paths: LakePaths,
    *,
    force: bool = False,
    skip_pull: bool = False,
    ship_date_start: str = SHIP_DATE_START,
    ship_date_end: str = SHIP_DATE_END,
) -> dict[str, Any]:
    """Write feature_catalog, feature_snapshots (chunked), deltas, decomp_by_day.

    Processes **one ship_month at a time** for deltas so peak RAM stays near
    one month of feature history (~MacBook-safe).
    """
    import gc

    paths.mart.mkdir(parents=True, exist_ok=True)
    write_feature_catalog(paths.feature_catalog_path)

    assets = repo_root() / "documentation" / "assets" / "etp_feature_catalog.json"
    if assets.exists():
        # Keep assets in sync when present; regenerate from code if stale
        write_feature_catalog(assets)
        paths.feature_catalog_path.write_text(assets.read_text())

    summary: dict[str, Any] = {"catalog": str(paths.feature_catalog_path)}

    if not skip_pull:
        pull_summary = pull_feature_history(
            paths.cache_dir,
            ship_date_start=ship_date_start,
            ship_date_end=ship_date_end,
            force=force,
            feature_snapshots_dir=paths.feature_snapshots,
            chunked=True,
        )
        summary["pull"] = pull_summary
    elif not any(paths.feature_snapshots.glob("ship_month=*/*.parquet")):
        # Offline path: partition flat cache month-by-month if present
        if paths.feature_history_cache.exists():
            logger.info(
                "skip-pull: partitioning flat cache → {}",
                display_path(paths.feature_snapshots),
            )
            hist = pl.scan_parquet(paths.feature_history_cache)
            if paths.per_load_cache.exists():
                per = pl.scan_parquet(paths.per_load_cache)
            else:
                per = hist.select(ID_COL).unique()
            counts = partition_snapshots(
                hist, per, paths.feature_snapshots, force=force
            )
            summary["feature_snapshot_counts"] = counts
            del hist, per
            gc.collect()
        else:
            logger.warning(
                "mart: no feature_snapshots and no flat cache {}; catalog only",
                paths.feature_history_cache,
            )
            return summary

    snap_dirs = sorted(paths.feature_snapshots.glob("ship_month=*"))
    if not snap_dirs:
        logger.warning("mart: no feature_snapshots partitions under {}", paths.feature_snapshots)
        return summary

    if force:
        _clear_hive_dir(paths.feature_deltas)
    paths.feature_deltas.mkdir(parents=True, exist_ok=True)

    total_delta = 0
    decomp_parts: list[pl.DataFrame] = []
    decomp_hl_parts: list[pl.DataFrame] = []
    catalog = load_feature_catalog(paths.feature_catalog_path)

    for part_dir in snap_dirs:
        ship_month = part_dir.name.split("=", 1)[-1]
        part_files = list(part_dir.glob("*.parquet"))
        if not part_files:
            continue
        logger.info("mart deltas for {}", ship_month)
        snaps = pl.read_parquet(part_files[0])
        if "ship_month" not in snaps.columns:
            snaps = snaps.with_columns(pl.lit(ship_month).alias("ship_month"))
        deltas = consecutive_feature_deltas(snaps)
        if deltas.is_empty():
            del snaps, deltas
            gc.collect()
            continue
        out = paths.feature_deltas / f"ship_month={ship_month}" / "part-0.parquet"
        _write_parquet_zstd(deltas, out)
        total_delta += deltas.height
        decomp_parts.append(
            decomp_by_day(deltas, by="prebook_day", catalog=catalog, min_rows=1)
        )
        if "is_hyperlocal" in deltas.columns:
            decomp_hl_parts.append(
                decomp_by_day(
                    deltas,
                    by="prebook_day",
                    catalog=catalog,
                    min_rows=1,
                    stratum_col="is_hyperlocal",
                )
            )
        summary.setdefault("feature_snapshot_counts", {})[ship_month] = snaps.height
        del snaps, deltas
        gc.collect()

    summary["n_delta_pairs"] = total_delta
    if decomp_parts:
        decomp = pl.concat([d for d in decomp_parts if not d.is_empty()], how="diagonal_relaxed")
        _write_parquet_zstd(decomp, paths.decomp_by_day_path)
        summary["n_decomp_rows"] = decomp.height
    if decomp_hl_parts:
        decomp_hl = pl.concat(
            [d for d in decomp_hl_parts if not d.is_empty()], how="diagonal_relaxed"
        )
        hl_path = paths.mart / "decomp_by_day_hyperlocal.parquet"
        _write_parquet_zstd(decomp_hl, hl_path)
        summary["n_decomp_hyperlocal_rows"] = decomp_hl.height
        summary["decomp_hyperlocal"] = str(hl_path)
    return summary


def copy_endpoints(per_load_src: Path, dest: Path, *, force: bool = False) -> Path:
    dest = Path(dest)
    if dest.exists() and not force and is_fresh(dest):
        logger.info("{} exists — skipping endpoints copy", display_path(dest))
        return dest
    df = pl.read_parquet(per_load_src)
    _write_parquet_zstd(df, dest)
    return dest


def build_duckdb_catalog(
    paths: LakePaths,
    *,
    force: bool = False,
) -> Path:
    """Create/replace DuckDB views over lake + external parquet layers."""
    import duckdb

    db_path = paths.duckdb_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and force:
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    try:
        snap_glob = paths.read_snapshots_glob()
        if any(paths.read_snapshots.glob("ship_month=*/*.parquet")):
            con.execute(
                f"""
                CREATE OR REPLACE VIEW snapshots AS
                SELECT * FROM read_parquet('{snap_glob}', hive_partitioning=true)
                """
            )
        elif paths.history_cache.exists():
            con.execute(
                f"""
                CREATE OR REPLACE VIEW snapshots AS
                SELECT *, '{UNKNOWN_SHIP_MONTH}' AS ship_month
                FROM read_parquet('{paths.history_cache}')
                """
            )

        per = (
            paths.read_per_load_lake
            if paths.read_per_load_lake.exists()
            else paths.per_load_cache
        )
        if per.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW endpoints AS SELECT * FROM read_parquet('{per}')"
            )

        surv_hist_glob = paths.survival_history_glob()
        if any(paths.read_survival_history.glob("ship_month=*/*.parquet")):
            con.execute(
                f"""
                CREATE OR REPLACE VIEW survival_snapshots AS
                SELECT * FROM read_parquet('{surv_hist_glob}', hive_partitioning=true)
                """
            )
        surv_end_glob = paths.survival_endpoints_glob()
        if any(paths.read_survival_endpoints.glob("ship_month=*/*.parquet")):
            con.execute(
                f"""
                CREATE OR REPLACE VIEW survival_endpoints AS
                SELECT * FROM read_parquet('{surv_end_glob}', hive_partitioning=true)
                """
            )

        if paths.read_etp_stats_path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW etp_stats AS "
                f"SELECT * FROM read_parquet('{paths.read_etp_stats_path}')"
            )

        features = paths.data_dir / "features.parquet"
        if features.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW features AS SELECT * FROM read_parquet('{features}')"
            )

        dial = paths.data_dir / "dqt_alt_percentiles.parquet"
        if not dial.exists():
            dial = paths.cache_dir / "dqt_alt_percentiles.parquet"
        if dial.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW dial_history AS SELECT * FROM read_parquet('{dial}')"
            )

        for name in (
            "hybrid_quantiles",
            "sarima_pp_quantiles",
            "sarima_hybrid_quantiles",
            "sarima_blend_quantiles",
            "sarima_tail_quantiles",
        ):
            pred = paths.data_dir / f"{name}.parquet"
            if pred.exists():
                con.execute(
                    f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{pred}')"
                )

        if any(paths.read_leadtime.glob("*.parquet")):
            for p in sorted(paths.read_leadtime.glob("*.parquet")):
                view = "leadtime_" + p.stem.replace("-", "_")
                con.execute(
                    f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{p}')"
                )

        feat_glob = paths.read_feature_snapshots_glob()
        if any(paths.read_feature_snapshots.glob("ship_month=*/*.parquet")):
            con.execute(
                f"""
                CREATE OR REPLACE VIEW feature_snapshots AS
                SELECT * FROM read_parquet('{feat_glob}', hive_partitioning=true)
                """
            )
        elif paths.feature_history_cache.exists():
            con.execute(
                f"""
                CREATE OR REPLACE VIEW feature_snapshots AS
                SELECT * FROM read_parquet('{paths.feature_history_cache}')
                """
            )

        delta_parts = list(paths.read_feature_deltas.glob("**/*.parquet"))
        if delta_parts:
            delta_glob = str(paths.read_feature_deltas / "**" / "*.parquet")
            if any(paths.read_feature_deltas.glob("ship_month=*/*.parquet")):
                con.execute(
                    f"""
                    CREATE OR REPLACE VIEW feature_deltas AS
                    SELECT * FROM read_parquet(
                        '{paths.read_feature_deltas_glob()}', hive_partitioning=true
                    )
                    """
                )
            else:
                con.execute(
                    f"CREATE OR REPLACE VIEW feature_deltas AS "
                    f"SELECT * FROM read_parquet('{delta_glob}', union_by_name=true)"
                )

        if paths.read_decomp_by_day_path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW decomp_by_day AS "
                f"SELECT * FROM read_parquet('{paths.read_decomp_by_day_path}')"
            )
        hl_decomp = paths.read_mart / "decomp_by_day_hyperlocal.parquet"
        if hl_decomp.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW decomp_by_day_hyperlocal AS "
                f"SELECT * FROM read_parquet('{hl_decomp}')"
            )

        build_feature_impact, impact_paths, read_feature_impact = _require_impact()
        impact = impact_paths(paths.read_mart)
        impact_view_map = {
            "impact_betas_by_prebook_day": impact["betas"],
            "impact_category_by_day": impact["category_by_day"],
            "impact_category_by_day_hyperlocal": impact["category_by_day_hyperlocal"],
            "impact_cat2_events": impact["cat2_events"],
            "impact_hypothesis_tests": impact["hypothesis_tests"],
            "impact_hypothesis_summary": impact["hypothesis_summary"],
            "impact_climb_by_prebook_day": impact["climb_by_prebook_day"],
            "impact_climb_by_prebook_day_hyperlocal": impact["climb_by_prebook_day_hyperlocal"],
            "impact_climb_totals_by_day": impact["climb_totals_by_day"],
        }
        for view_name, parquet_path in impact_view_map.items():
            if parquet_path.exists():
                con.execute(
                    f"CREATE OR REPLACE VIEW {view_name} AS "
                    f"SELECT * FROM read_parquet('{parquet_path}')"
                )
        step_contrib_glob = str(impact["step_contrib"] / "ship_month=*" / "*.parquet")
        if any(impact["step_contrib"].glob("ship_month=*/*.parquet")):
            con.execute(
                f"""
                CREATE OR REPLACE VIEW impact_step_contrib AS
                SELECT * FROM read_parquet('{step_contrib_glob}', hive_partitioning=true)
                """
            )

        build_funnel_attrs, funnel_paths, read_funnel = _require_funnel()
        funnel = funnel_paths(paths.read_mart)
        funnel_view_map = {
            "funnel_load_attrs": funnel["load_attrs"],
            "funnel_rollups": funnel["rollups"],
            "funnel_sankey_edges": funnel["sankey_edges"],
        }
        for view_name, parquet_path in funnel_view_map.items():
            if parquet_path.exists():
                con.execute(
                    f"CREATE OR REPLACE VIEW {view_name} AS "
                    f"SELECT * FROM read_parquet('{parquet_path}')"
                )
    finally:
        con.close()
    if paths.using_mirror:
        logger.info(
            "wrote DuckDB catalog {} (views → mirror {})",
            display_path(db_path),
            display_path(paths.read_root),
        )
    else:
        logger.info("wrote DuckDB catalog {}", display_path(db_path))
    return db_path


class EtpLake:
    """Scientist-facing handle for the offline ETP lake."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        *,
        root: Path | None = None,
    ) -> None:
        self.paths = LakePaths.resolve(data_dir, cache_dir, root=root)

    def connect(self):
        """Open DuckDB connection to the lake catalog (builds views if missing)."""
        import duckdb

        if not self.paths.duckdb_path.exists():
            build_duckdb_catalog(self.paths, force=True)
        return duckdb.connect(str(self.paths.duckdb_path), read_only=True)

    def scan_snapshots(self) -> pl.LazyFrame:
        glob = list(self.paths.read_snapshots.glob("ship_month=*/*.parquet"))
        if glob:
            return pl.scan_parquet(
                str(self.paths.read_snapshots / "ship_month=*" / "*.parquet")
            )
        if self.paths.history_cache.exists():
            return pl.scan_parquet(self.paths.history_cache)
        raise FileNotFoundError(
            f"No snapshots under {self.paths.read_snapshots} and no cache at "
            f"{self.paths.history_cache}"
        )

    def history(
        self,
        loadnumber: int | None = None,
        *,
        ship_month: str | None = None,
        source: str | None = None,
    ) -> pl.DataFrame:
        lf = self.scan_snapshots()
        if ship_month is not None:
            month_glob = list(
                self.paths.read_snapshots.glob(f"ship_month={ship_month}/*.parquet")
            )
            if month_glob:
                lf = pl.scan_parquet(
                    str(
                        self.paths.read_snapshots
                        / f"ship_month={ship_month}"
                        / "*.parquet"
                    )
                )
            elif "ship_month" in lf.collect_schema().names():
                lf = lf.filter(pl.col("ship_month") == ship_month)
        if loadnumber is not None:
            lf = lf.filter(pl.col(ID_COL) == loadnumber)
        if source is not None:
            lf = lf.filter(pl.col("source") == source)
        return lf.sort(["loadnumber", "snapshot_utc", "source"]).collect()

    def io_state(self, loadnumber: int) -> dict[str, Any]:
        """Booking features (if present) + latest model/audit slider snaps."""
        out: dict[str, Any] = {ID_COL: loadnumber}
        features_path = self.paths.data_dir / "features.parquet"
        if features_path.exists():
            feat = (
                pl.scan_parquet(features_path)
                .filter(pl.col(ID_COL) == loadnumber)
                .collect()
            )
            if feat.height:
                out["features"] = feat.row(0, named=True)
        hist = self.history(loadnumber=loadnumber)
        if hist.height:
            model = hist.filter(pl.col("source") == "model").sort("snapshot_utc")
            audit = hist.filter(pl.col("source") == "audit").sort("snapshot_utc")
            if model.height:
                out["latest_model"] = model.row(-1, named=True)
                out["n_model_snaps"] = model.height
            if audit.height:
                out["latest_audit"] = audit.row(-1, named=True)
                out["n_audit_snaps"] = audit.height
            out["history"] = hist
        per = (
            self.paths.per_load_lake
            if self.paths.per_load_lake.exists()
            else self.paths.per_load_cache
        )
        if per.exists():
            ep = pl.scan_parquet(per).filter(pl.col(ID_COL) == loadnumber).collect()
            if ep.height:
                out["endpoint"] = ep.row(0, named=True)
        return out

    def scan_feature_snapshots(self) -> pl.LazyFrame:
        glob = list(self.paths.read_feature_snapshots.glob("ship_month=*/*.parquet"))
        if glob:
            return pl.scan_parquet(self.paths.read_feature_snapshots_glob())
        if self.paths.feature_history_cache.exists():
            return pl.scan_parquet(self.paths.feature_history_cache)
        raise FileNotFoundError(
            f"No feature_snapshots under {self.paths.read_feature_snapshots} and no cache at "
            f"{self.paths.feature_history_cache}"
        )

    def feature_history(
        self,
        loadnumber: int | None = None,
        *,
        ship_month: str | None = None,
    ) -> pl.DataFrame:
        """Raw prediction+input path — no analytics anchor baked in."""
        lf = self.scan_feature_snapshots()
        if ship_month is not None:
            month_glob = list(
                self.paths.read_feature_snapshots.glob(
                    f"ship_month={ship_month}/*.parquet"
                )
            )
            if month_glob:
                lf = pl.scan_parquet(
                    str(
                        self.paths.read_feature_snapshots
                        / f"ship_month={ship_month}"
                        / "*.parquet"
                    )
                )
            elif "ship_month" in lf.collect_schema().names():
                lf = lf.filter(pl.col("ship_month") == ship_month)
        if loadnumber is not None:
            lf = lf.filter(pl.col(ID_COL) == loadnumber)
        return lf.sort([ID_COL, "snapshot_utc"]).collect()

    def decompose(
        self,
        loadnumber: int | None = None,
        *,
        ship_month: str | None = None,
        anchor_hsa: int = DEFAULT_ANCHOR_HSA,
        tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
        by: str = "prebook_day",
        is_hyperlocal: bool | None = None,
    ) -> dict[str, Any]:
        """Report-time ΔETP vs Δinputs; anchor is a parameter, not mart schema.

        Pass ``is_hyperlocal=True/False`` to restrict to hyperlocal model calls.
        Require ``loadnumber`` and/or ``ship_month`` so we never collect the full
        multi-year mart into RAM.
        """
        if loadnumber is None and ship_month is None:
            raise ValueError(
                "decompose requires loadnumber and/or ship_month "
                "(refusing to collect full mart into memory)"
            )
        snaps = self.feature_history(loadnumber=loadnumber, ship_month=ship_month)
        catalog = load_feature_catalog(self.paths.feature_catalog_path)
        return decompose_frame(
            snaps,
            anchor_hsa=anchor_hsa,
            tolerance_h=tolerance_h,
            by=by,  # type: ignore[arg-type]
            catalog=catalog,
            is_hyperlocal=is_hyperlocal,
        )

    def feature_catalog(self) -> dict[str, Any]:
        return load_feature_catalog(self.paths.feature_catalog_path)

    def feature_impact(
        self,
        loadnumber: int | None = None,
        *,
        ship_month: str | None = None,
        anchor_hsa: int = DEFAULT_ANCHOR_HSA,
        tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
        by: str = "prebook_day",
        is_hyperlocal: bool | None = None,
        build: bool = False,
        force: bool = False,
        min_rows: int = DEFAULT_MIN_OLS_ROWS,
        write_step_contrib: bool = True,
    ) -> dict[str, Any]:
        """Layer B/C feature-impact tables scoped by ship_month and/or loadnumber.

        Reads pre-built parquet under ``mart/feature_impact/`` by default.
        Pass ``build=True`` (or ``force=True``) to rebuild from mart deltas first.
        """
        if loadnumber is None and ship_month is None:
            raise ValueError(
                "feature_impact requires loadnumber and/or ship_month "
                "(refusing to collect full mart into memory)"
            )
        if by not in ("prebook_day", "lead_day"):
            raise ValueError(f"by must be 'prebook_day' or 'lead_day', got {by!r}")

        build_summary: dict[str, Any] | None = None
        if build or force:
            build_feature_impact, _, _ = _require_impact()
            if not self.paths.feature_deltas.exists():
                raise FileNotFoundError(
                    f"missing feature_deltas: {self.paths.feature_deltas} "
                    "(run mart stage first)"
                )
            build_summary = build_feature_impact(
                self.paths.mart,
                feature_deltas_dir=self.paths.feature_deltas,
                feature_snapshots_dir=self.paths.feature_snapshots,
                force=force,
                ship_month=ship_month,
                min_rows=min_rows,
                write_step_contrib=write_step_contrib,
                anchor_hsa=anchor_hsa,
                tolerance_h=tolerance_h,
            )

        _, _, read_feature_impact = _require_impact()
        out = read_feature_impact(
            self.paths.read_mart,
            ship_month=ship_month,
            loadnumber=loadnumber,
            by=by,  # type: ignore[arg-type]
            is_hyperlocal=is_hyperlocal,
        )
        out["anchor_hsa"] = anchor_hsa
        out["tolerance_h"] = tolerance_h
        if build_summary is not None:
            out["build_summary"] = build_summary
        return out

    def plot_etp_timeline(
        self,
        loadnumber: int,
        *,
        show: bool = True,
        title: str | None = None,
        mde_cache: Path | str | None = None,
        query_mde: bool = False,
        data_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Model + audit display lifecycle with checkpoint inflections and MDE overlay."""
        from dqt.etp_timeline import plot_etp_timeline

        return plot_etp_timeline(
            self,
            loadnumber,
            show=show,
            title=title,
            mde_cache=mde_cache,
            query_mde=query_mde,
            data_dir=data_dir or self.paths.data_dir,
        )

    def build_etp_timeline(
        self,
        loadnumber: int,
        *,
        mde_cache: Path | str | None = None,
        query_mde: bool = False,
    ) -> dict[str, Any]:
        """Lifecycle data only (no plot) — model, audit, checkpoints, MDE."""
        from dqt.etp_timeline import build_load_timeline

        return build_load_timeline(
            self, loadnumber, mde_cache=mde_cache, query_mde=query_mde
        )

    def funnel(
        self,
        loadnumber: int | None = None,
        *,
        ship_month: str | None = None,
        build: bool = False,
        force: bool = False,
        anchor_hsa: int = DEFAULT_ANCHOR_HSA,
        tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
    ) -> dict[str, Any]:
        """Shipment funnel load attrs + rollups + Sankey edges.

        Reads pre-built parquet under ``mart/funnel/`` by default.
        Pass ``build=True`` (or ``force=True``) to rebuild from mart first.
        """
        build_funnel_attrs, funnel_paths, read_funnel = _require_funnel()
        if loadnumber is None and ship_month is None and not build and not force:
            paths = funnel_paths(self.paths.read_mart)
            if not paths["load_attrs"].exists():
                raise ValueError(
                    "funnel requires loadnumber and/or ship_month when parquet missing "
                    "(refusing to collect full mart into memory)"
                )

        build_summary: dict[str, Any] | None = None
        if build or force:
            if not any(self.paths.feature_snapshots.glob("ship_month=*/*.parquet")):
                raise FileNotFoundError(
                    f"missing feature_snapshots: {self.paths.feature_snapshots} "
                    "(run mart stage first)"
                )
            per_load = (
                self.paths.per_load_lake
                if self.paths.per_load_lake.exists()
                else self.paths.per_load_cache
            )
            build_summary = build_funnel_attrs(
                self.paths.mart,
                feature_snapshots_dir=self.paths.feature_snapshots,
                feature_deltas_dir=self.paths.feature_deltas
                if self.paths.feature_deltas.exists()
                else None,
                per_load_path=per_load if per_load.exists() else None,
                stages_path=funnel_paths(self.paths.mart)["stages_cache"],
                force=force,
                ship_month=ship_month,
                anchor_hsa=anchor_hsa,
                tolerance_h=tolerance_h,
            )

        out = read_funnel(
            self.paths.read_mart,
            ship_month=ship_month,
            loadnumber=loadnumber,
        )
        out["anchor_hsa"] = anchor_hsa
        out["tolerance_h"] = tolerance_h
        if build_summary is not None:
            out["build_summary"] = build_summary
        return out


def run_impact_stage(
    paths: LakePaths,
    *,
    force: bool = False,
    ship_month: str | None = None,
    min_rows: int = DEFAULT_MIN_OLS_ROWS,
    write_step_contrib: bool = True,
    anchor_hsa: int = DEFAULT_ANCHOR_HSA,
    tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
) -> dict[str, Any]:
    """Build Layer B/C feature-impact tables from mart deltas."""
    build_feature_impact, _, _ = _require_impact()
    if not paths.feature_deltas.exists():
        raise FileNotFoundError(
            f"missing feature_deltas: {paths.feature_deltas} (run mart stage first)"
        )
    return build_feature_impact(
        paths.mart,
        feature_deltas_dir=paths.feature_deltas,
        feature_snapshots_dir=paths.feature_snapshots,
        force=force,
        ship_month=ship_month,
        min_rows=min_rows,
        write_step_contrib=write_step_contrib,
        anchor_hsa=anchor_hsa,
        tolerance_h=tolerance_h,
    )


def run_funnel_stage(
    paths: LakePaths,
    *,
    force: bool = False,
    ship_month: str | None = None,
    anchor_hsa: int = DEFAULT_ANCHOR_HSA,
    tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
) -> dict[str, Any]:
    """Build funnel load attrs + rollups from mart partitions."""
    build_funnel_attrs, funnel_paths, _read_funnel = _require_funnel()
    if not any(paths.feature_snapshots.glob("ship_month=*/*.parquet")):
        raise FileNotFoundError(
            f"missing feature_snapshots: {paths.feature_snapshots} (run mart stage first)"
        )
    per_load = (
        paths.per_load_lake if paths.per_load_lake.exists() else paths.per_load_cache
    )
    return build_funnel_attrs(
        paths.mart,
        feature_snapshots_dir=paths.feature_snapshots,
        feature_deltas_dir=paths.feature_deltas if paths.feature_deltas.exists() else None,
        per_load_path=per_load if per_load.exists() else None,
        stages_path=funnel_paths(paths.mart)["stages_cache"],
        force=force,
        ship_month=ship_month,
        anchor_hsa=anchor_hsa,
        tolerance_h=tolerance_h,
    )


def run_leadtime_stage(
    hist_path: Path,
    per_path: Path,
    out_dir: Path,
    *,
    anchor_hsa: int = DEFAULT_ANCHOR_HSA,
    tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
) -> dict[str, Any]:
    """Invoke leadtime analysis writing into ``out_dir``."""
    import importlib.util
    import sys

    script = repo_root() / "scripts" / "etp_slider_leadtime_analysis.py"
    spec = importlib.util.spec_from_file_location("etp_slider_leadtime_analysis", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return mod.main(
        hist_path=hist_path,
        per_path=per_path,
        out_dir=out_dir,
        anchor_hsa=anchor_hsa,
        tolerance_h=tolerance_h,
    )


__all__ = [
    "ALL_STAGES",
    "AUDIT_AGG",
    "ETP_AGG",
    "LAKE_DIRNAME",
    "SNAPSHOT_COLS",
    "UNKNOWN_SHIP_MONTH",
    "EtpLake",
    "LakePaths",
    "attach_ship_month",
    "build_analytics_mart",
    "build_duckdb_catalog",
    "compute_etp_stats",
    "copy_endpoints",
    "default_cache_dir",
    "lake_root",
    "load_ship_months",
    "parse_stages",
    "partition_snapshots",
    "pull_feature_history",
    "pull_slider_caches",
    "resolve_lake_mirror",
    "run_funnel_stage",
    "run_impact_stage",
    "run_leadtime_stage",
    "ship_month_from_pickup",
    "write_manifest",
    "write_schema_snapshots",
]

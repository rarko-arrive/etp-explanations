"""Market Movement MDE timeline for problem-load explorer overlays."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

MARK_HRS: tuple[int, ...] = (999, 168, 144, 120, 96, 72, 48, 24)
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

from dqt import repo_root

MDE_TIMELINE_SQL = repo_root() / "SQL" / "etp-slider" / "explorer-mde-timeline.sql"

# v1: strict Market Movement label only (see SQL filter).
MARKET_MOVEMENT_MDE_LABEL = "Market Movement"
MARKET_MOVEMENT_MDE_RE = re.compile(r"^market\s+movement$", re.IGNORECASE)


def is_market_movement_mde(description: str | None) -> bool:
    if not description:
        return False
    return bool(MARKET_MOVEMENT_MDE_RE.match(description.strip()))


def hours_before_pickup(applied_at: datetime, pickup_utc: datetime) -> float | None:
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=UTC)
    if pickup_utc.tzinfo is None:
        pickup_utc = pickup_utc.replace(tzinfo=UTC)
    return (pickup_utc - applied_at).total_seconds() / 3600.0


def snap_to_mark(hbp: float, marks: tuple[int, ...] | list[int] = MARK_HRS) -> int:
    """Pick checkpoint mark closest to hours-before-pickup (for chart x-axis)."""
    return min(marks, key=lambda m: abs(m - hbp))


def load_mde_timeline_events(
    loadnumbers: list[int],
    *,
    cache_path: Path | str | None = None,
    force: bool = False,
    batch_size: int = 5000,
) -> pl.DataFrame:
    """Load Market Movement MDE apply times for embedded explorer loads."""
    if not loadnumbers:
        return pl.DataFrame()

    if cache_path is not None:
        cache_path = Path(cache_path)
        if not force and cache_path.is_file():
            cached = pl.read_parquet(cache_path)
            return cached.filter(pl.col("loadnumber").is_in(loadnumbers))

    from arriveds.snowflake import query_sf

    template = MDE_TIMELINE_SQL.read_text()
    frames: list[pl.DataFrame] = []
    for i in range(0, len(loadnumbers), batch_size):
        batch = loadnumbers[i : i + batch_size]
        ids = ",".join(str(x) for x in batch)
        sql = template.format(loadnumber_in_list=ids)
        frames.append(query_sf(sql))

    raw = pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
    if raw.is_empty():
        return raw

    if "mde_description" in raw.columns:
        raw = raw.filter(pl.col("mde_description").map_elements(is_market_movement_mde, return_dtype=pl.Boolean))

    if cache_path is not None:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.is_file() and not force:
            prior = pl.read_parquet(cache_path)
            raw = pl.concat([prior, raw], how="diagonal").unique(
                ["loadnumber", "mde_id", "applied_at_utc"], keep="last"
            )
        raw.write_parquet(cache_path, compression="zstd")

    return raw.filter(pl.col("loadnumber").is_in(loadnumbers))


def aggregate_mde_events_by_load(events: pl.DataFrame) -> pl.DataFrame:
    """One row per load with timeline MDE event counts (Market Movement in v1 SQL)."""
    schema = {
        "loadnumber": pl.Int64,
        "n_mde_timeline_events": pl.UInt32,
        "mde_timeline_ind": pl.Int8,
    }
    if events.is_empty() or "loadnumber" not in events.columns:
        return pl.DataFrame(schema=schema)
    return (
        events.group_by("loadnumber")
        .agg(pl.len().alias("n_mde_timeline_events"))
        .with_columns((pl.col("n_mde_timeline_events") > 0).cast(pl.Int8).alias("mde_timeline_ind"))
    )


def build_mde_explorer_overlay(
    events: pl.DataFrame,
    timing: pl.DataFrame,
    loadnumbers: list[int],
    *,
    marks_by_load: dict[str, list[int]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Chart annotations + catalog meta for Market Movement MDE overlays."""
    annotations: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, dict[str, Any]] = {}

    if events.is_empty() or timing.is_empty() or not loadnumbers:
        return annotations, meta

    timing_cols = [c for c in ("loadnumber", "pickup_appt_latest_utc") if c in timing.columns]
    if len(timing_cols) < 2:
        return annotations, meta

    timing_map = {
        int(r["loadnumber"]): r["pickup_appt_latest_utc"] for r in timing.select(timing_cols).iter_rows(named=True)
    }

    for ln in loadnumbers:
        pickup = timing_map.get(int(ln))
        if pickup is None:
            continue
        sub = events.filter(pl.col("loadnumber") == ln).sort("applied_at_utc")
        if sub.is_empty():
            continue

        marks = tuple(marks_by_load.get(str(ln), list(MARK_HRS))) if marks_by_load else MARK_HRS
        load_events: list[dict[str, Any]] = []
        ann_rows: list[dict[str, Any]] = []

        for row in sub.iter_rows(named=True):
            applied = row.get("applied_at_utc")
            if applied is None:
                continue
            if isinstance(applied, str):
                applied = datetime.fromisoformat(applied)
            hbp = hours_before_pickup(applied, pickup)
            if hbp is None or hbp < 0:
                continue
            mark = snap_to_mark(hbp, marks)
            desc = str(row.get("mde_description") or MARKET_MOVEMENT_MDE_LABEL)
            mark_label = MARK_LABELS.get(mark, str(mark))
            applied_iso = applied.isoformat() if hasattr(applied, "isoformat") else str(applied)
            evt = {
                "mde_id": int(row["mde_id"]) if row.get("mde_id") is not None else None,
                "description": desc,
                "applied_at_utc": applied_iso,
                "mark_hrs": mark,
                "mark_label": mark_label,
                "hours_before_pickup": round(hbp, 1),
            }
            load_events.append(evt)
            ann_rows.append(
                {
                    "mark_hrs": mark,
                    "label": mark_label,
                    "text": f"MDE: {desc}",
                    "category": "MDE",
                    "applied_at_utc": applied_iso,
                }
            )

        if load_events:
            key = str(ln)
            meta[key] = {
                "applied": True,
                "n_events": len(load_events),
                "events": load_events,
                "primary_description": load_events[0]["description"],
            }
            annotations[key] = ann_rows

    return annotations, meta


__all__ = [
    "MARKET_MOVEMENT_MDE_LABEL",
    "aggregate_mde_events_by_load",
    "build_mde_explorer_overlay",
    "hours_before_pickup",
    "is_market_movement_mde",
    "load_mde_timeline_events",
    "snap_to_mark",
]

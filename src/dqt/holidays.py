"""Freight-relevant US holiday calendar for temporal miss analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from dqt.score.constants import ID_COL


@dataclass(frozen=True, slots=True)
class HolidaySpec:
    name: str
    kind: str
    rule: str
    month: int | None = None
    day: int | None = None
    nth: int | None = None
    weekday: int | None = None
    enabled: bool = True
    pre_days: int = 3
    post_days: int = 3
    observe_weekend: bool = True


def _observe(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, nth: int, weekday: int) -> date:
    if nth == -1:
        d = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=nth - 1)


def _statutory_date(spec: HolidaySpec, year: int) -> date:
    if spec.rule == "fixed":
        assert spec.month is not None and spec.day is not None
        return date(year, spec.month, spec.day)
    assert spec.month is not None and spec.nth is not None and spec.weekday is not None
    return _nth_weekday(year, spec.month, spec.nth, spec.weekday)


DEFAULT_HOLIDAYS: tuple[HolidaySpec, ...] = (
    HolidaySpec(name="New Year's Day", kind="federal", rule="fixed", month=1, day=1),
    HolidaySpec(name="Memorial Day", kind="federal", rule="nth_weekday", month=5, nth=-1, weekday=0),
    HolidaySpec(name="Independence Day", kind="federal", rule="fixed", month=7, day=4),
    HolidaySpec(name="Labor Day", kind="federal", rule="nth_weekday", month=9, nth=1, weekday=0),
    HolidaySpec(name="Thanksgiving", kind="federal", rule="nth_weekday", month=11, nth=4, weekday=3),
    HolidaySpec(name="Christmas Day", kind="federal", rule="fixed", month=12, day=25),
)


def resolve_occurrences(
    *,
    year_start: int,
    year_end: int,
    specs: tuple[HolidaySpec, ...] | None = None,
) -> pl.DataFrame:
    """One row per holiday occurrence with pre/post analysis windows."""
    specs = specs or DEFAULT_HOLIDAYS
    rows: list[dict[str, object]] = []
    for spec in specs:
        if not spec.enabled:
            continue
        for year in range(year_start, year_end + 1):
            statutory = _statutory_date(spec, year)
            observed = _observe(statutory) if spec.observe_weekend else statutory
            rows.append(
                {
                    "name": spec.name,
                    "kind": spec.kind,
                    "observed_date": observed,
                    "window_start": observed - timedelta(days=spec.pre_days),
                    "window_end": observed + timedelta(days=spec.post_days),
                }
            )
    if not rows:
        return pl.DataFrame(
            schema={
                "name": pl.Utf8,
                "kind": pl.Utf8,
                "observed_date": pl.Date,
                "window_start": pl.Date,
                "window_end": pl.Date,
            }
        )
    return pl.DataFrame(rows).sort(["observed_date", "name"])


def tag_holiday_windows(
    df: pl.DataFrame,
    date_col: str,
    *,
    year_start: int | None = None,
    year_end: int | None = None,
) -> pl.DataFrame:
    """Add ``holiday_name`` and ``in_holiday_window`` from ``date_col``."""
    if df.is_empty() or date_col not in df.columns:
        return df.with_columns(
            pl.lit("").alias("holiday_name"),
            pl.lit(False).alias("in_holiday_window"),
        )

    tagged = df.with_columns(pl.col(date_col).cast(pl.Datetime).dt.date().alias("_event_date"))
    dates = tagged.select("_event_date").drop_nulls()
    if dates.is_empty():
        return tagged.with_columns(
            pl.lit("").alias("holiday_name"),
            pl.lit(False).alias("in_holiday_window"),
        ).drop("_event_date")

    ys = year_start or int(dates["_event_date"].min().year)  # type: ignore[union-attr]
    ye = year_end or int(dates["_event_date"].max().year)  # type: ignore[union-attr]
    occ = resolve_occurrences(year_start=ys, year_end=ye)
    if occ.is_empty():
        return tagged.with_columns(
            pl.lit("").alias("holiday_name"),
            pl.lit(False).alias("in_holiday_window"),
        ).drop("_event_date")

    join_keys = [ID_COL] if ID_COL in tagged.columns else ["_event_date"]
    probe = tagged.select([*join_keys, "_event_date"])
    hits = (
        probe.join(occ, how="cross")
        .filter(
            (pl.col("_event_date") >= pl.col("window_start"))
            & (pl.col("_event_date") <= pl.col("window_end"))
        )
        .sort([*join_keys, "observed_date"])
        .group_by(join_keys)
        .agg(
            pl.col("name").first().alias("holiday_name"),
            pl.lit(True).alias("in_holiday_window"),
        )
    )
    out = tagged.join(hits, on=join_keys, how="left").with_columns(
        pl.col("holiday_name").fill_null(""),
        pl.col("in_holiday_window").fill_null(False),
    )
    return out.drop("_event_date")


__all__ = ["DEFAULT_HOLIDAYS", "HolidaySpec", "resolve_occurrences", "tag_holiday_windows"]

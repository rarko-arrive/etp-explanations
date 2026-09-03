"""ETP shipment lifecycle explanation — material change ledger and HC outlier cohort."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import polars as pl

from dqt import resolve_data_dir
from dqt.etp_lifecycle.lightning import build_lightning_tracking
from dqt.etp_mart import consecutive_feature_deltas
from dqt.etp_slider.movement.orders import enrich_movement_flags
from dqt.etp_slider.paths import resolve_etp_cache_dir
from dqt.etp_slider.problem_loads.leadtime import (
    _book_quotes_for_loads,
    _join_book_time_quotes,
    _prepare_leadtime_pricing_frame,
    _score_pricing_slice,
    join_lc_carrier_cost,
)
from dqt.etp_slider.sql import HISTORY_CACHE, SURVIVAL_PER_LOAD_CACHE
from dqt.etp_timeline import build_load_timeline
from dqt.score.constants import COST_COL, ID_COL

if TYPE_CHECKING:
    from dqt.etp_lake import EtpLake

ETP_IMPACT_MIN_USD = 25.0
DISPLAY_GAP_MIN_USD = 40.0
TARGET_MIN_USD = 25.0
CLOCK_MIN_H = 0.5
KNN_CO_MOVE_MIN_USD = 20.0
CHARGE_MIN_USD = 50.0

DEFAULT_AVAIL_START = "2025-01-01"
DEFAULT_AVAIL_END = "2026-08-28"
DEFAULT_EXEC_DIR = f"executive-{DEFAULT_AVAIL_START}_{DEFAULT_AVAIL_END}"
DEFAULT_DAVIS = f"analysis-davis-raw-{DEFAULT_AVAIL_START}_{DEFAULT_AVAIL_END}.parquet"
HC_OUTLIER_REL = "lifecycle/hc-outlier-index.parquet"

COHORT_HC = "hc_leadtime_isolated"
COHORT_VOLATILE_LANE = "volatile_lane_1"
COHORT_MANUAL = "manual"

CohortName = Literal["hc_leadtime_isolated", "volatile_lane_1", "manual"]

LEDGER_COLUMNS: tuple[str, ...] = (
    "ts",
    "hours_before_pickup",
    "hours_since_available",
    "metric",
    "delta_usd",
    "delta_other",
    "category",
    "driver_text",
    "confidence",
    "evidence_cols",
    "is_pricing_driver",
)


def _empty_ledger() -> pl.DataFrame:
    return pl.DataFrame({c: [] for c in LEDGER_COLUMNS}).cast(
        {
            "hours_before_pickup": pl.Int64,
            "hours_since_available": pl.Int64,
            "delta_usd": pl.Float64,
            "delta_other": pl.Float64,
            "is_pricing_driver": pl.Boolean,
        }
    )


def _ledger_row(
    *,
    ts: Any = None,
    hours_before_pickup: int | None = None,
    hours_since_available: int | None = None,
    metric: str,
    delta_usd: float | None = None,
    delta_other: float | None = None,
    category: str,
    driver_text: str,
    confidence: str = "medium",
    evidence_cols: list[str] | None = None,
    is_pricing_driver: bool | None = None,
) -> dict[str, Any]:
    if is_pricing_driver is None:
        is_pricing_driver = category not in ("DisplayAdjust",)
    return {
        "ts": ts,
        "hours_before_pickup": hours_before_pickup,
        "hours_since_available": hours_since_available,
        "metric": metric,
        "delta_usd": delta_usd,
        "delta_other": delta_other,
        "category": category,
        "driver_text": driver_text,
        "confidence": confidence,
        "evidence_cols": evidence_cols or [],
        "is_pricing_driver": is_pricing_driver,
    }


def _checkpoint_timing(checkpoints: pl.DataFrame, mark_hrs: int) -> tuple[int | None, int | None]:
    if checkpoints.is_empty():
        return None, None
    row = checkpoints.filter(pl.col("mark_hrs") == mark_hrs)
    if row.is_empty():
        return mark_hrs, None
    hbp = row["hours_before_pickup"][0]
    hsa = row["hours_since_available"][0] if "hours_since_available" in row.columns else None
    return (
        int(hbp) if hbp is not None else mark_hrs,
        int(hsa) if hsa is not None else None,
    )


def _resolve_paths(
    data_dir: Path | str | None = None,
    *,
    davis_cache: Path | str | None = None,
) -> dict[str, Path]:
    base = resolve_data_dir(data_dir)
    cache = resolve_etp_cache_dir(base)
    book_candidates = sorted(cache.glob("leadtime-book-quotes-*.parquet"))
    return {
        "data_dir": base,
        "cache": cache,
        "labeled": cache / DEFAULT_EXEC_DIR / "labeled-cohort.parquet",
        "davis": Path(davis_cache) if davis_cache else cache / DEFAULT_DAVIS,
        "lc_tail": cache / "lc-tail-2025.parquet",
        "hist": cache / HISTORY_CACHE,
        "hc_index": cache / HC_OUTLIER_REL,
        "survival": cache / SURVIVAL_PER_LOAD_CACHE,
        "book_quotes": book_candidates[-1] if book_candidates else cache / "leadtime-book-quotes-missing.parquet",
    }


def _join_book_quotes_readonly(frame: pl.DataFrame, paths: dict[str, Path]) -> pl.DataFrame:
    """Join book-time quotes without writing supplement rows to the shared cache."""
    book_cache = paths["book_quotes"]
    if book_cache.is_file():
        cached = pl.read_parquet(book_cache).join(frame.select(ID_COL), on=ID_COL, how="inner")
        if not cached.is_empty():
            return _join_book_time_quotes(frame, cached)

    if not paths["hist"].is_file() or not paths["survival"].is_file():
        return frame
    book = _book_quotes_for_loads(
        frame[ID_COL],
        hist_path=paths["hist"],
        survival_path=paths["survival"],
    )
    if book.is_empty():
        return frame
    return _join_book_time_quotes(frame, book)


def select_outlier_cohort(
    *,
    cohort: CohortName = COHORT_HC,
    min_shift_pct: float = 0.10,
    top_n: int | None = None,
    data_dir: Path | str | None = None,
    davis_cache: Path | str | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    """Rank HC leadtime-isolated outliers (or lane filters) for lifecycle gallery."""
    paths = _resolve_paths(data_dir, davis_cache=davis_cache)
    index_path = paths["hc_index"]
    if index_path.is_file() and not refresh and cohort == COHORT_HC:
        cached = pl.read_parquet(index_path)
        if top_n is not None:
            return cached.head(top_n)
        return cached

    if not paths["labeled"].is_file():
        raise FileNotFoundError(f"Missing labeled cohort: {paths['labeled']}")

    labeled = pl.read_parquet(paths["labeled"])
    davis = paths["davis"] if paths["davis"].is_file() else None
    frame = _prepare_leadtime_pricing_frame(
        labeled,
        davis_cache=davis,
        data_dir=paths["data_dir"],
        hist_path=paths["hist"],
        problem_only=True,
        with_archetypes=True,
    )
    if frame.is_empty():
        return pl.DataFrame()

    if cohort == COHORT_HC:
        frame = frame.filter(pl.col("leadtime_isolated_ind") == 1)
    elif cohort == COHORT_VOLATILE_LANE:
        if "volatile_lane_1_ind" in frame.columns:
            frame = frame.filter(pl.col("volatile_lane_1_ind") == 1)
        elif "shift_segment" in frame.columns:
            frame = frame.filter(pl.col("shift_segment") == "problem")

    if "etp50_shift_pct" in frame.columns:
        frame = frame.filter(pl.col("etp50_shift_pct").abs() >= min_shift_pct)

    ranked = (
        frame.sort("etp50_shift_pct", descending=True)
        .with_row_index("hc_rank", offset=1)
        .with_columns(pl.lit(cohort).alias("cohort"))
    )
    if top_n is not None:
        ranked = ranked.head(top_n)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    ranked.write_parquet(index_path)
    return ranked


def resolve_load_id(
    *,
    cohort: CohortName = COHORT_HC,
    rank: int = 1,
    load_id: int | None = None,
    data_dir: Path | str | None = None,
    davis_cache: Path | str | None = None,
) -> int:
    """Resolve loadnumber from explicit override or HC rank."""
    if load_id is not None:
        return int(load_id)
    if cohort == COHORT_MANUAL:
        raise ValueError("COHORT=manual requires LOAD_ID")
    hc = select_outlier_cohort(cohort=cohort, data_dir=data_dir, davis_cache=davis_cache)
    row = hc.filter(pl.col("hc_rank") == rank)
    if row.is_empty():
        raise ValueError(f"No load at rank {rank} in cohort {cohort!r}")
    return int(row["loadnumber"][0])


def _clock_events_from_deltas(deltas: pl.DataFrame) -> list[dict[str, Any]]:
    if deltas.is_empty():
        return []
    events: list[dict[str, Any]] = []
    for row in deltas.iter_rows(named=True):
        detp = row.get("delta_etp50")
        if detp is None or abs(float(detp)) < ETP_IMPACT_MIN_USD:
            continue
        for col, label in (("book_2_pkup", "book_2_pkup"), ("avail_2_book", "avail_2_book")):
            dcol = f"delta_{col}"
            if dcol not in row or row[dcol] is None:
                continue
            delta = float(row[dcol])
            if abs(delta) < CLOCK_MIN_H:
                continue
            sign = "+" if delta >= 0 else "−"
            events.append(
                _ledger_row(
                    ts=row.get("snapshot_utc_t1"),
                    hours_before_pickup=int(row["hours_before_pickup"])
                    if "hours_before_pickup" in row and row["hours_before_pickup"] is not None
                    else None,
                    hours_since_available=int(row["hours_since_available"])
                    if "hours_since_available" in row and row["hours_since_available"] is not None
                    else None,
                    metric=col,
                    delta_usd=None,
                    delta_other=delta,
                    category="LeadtimeChange",
                    driver_text=f"{label} {sign}{abs(delta):.1f}h with ETP {float(detp):+,.0f}",
                    confidence="high",
                    evidence_cols=[dcol, "delta_etp50"],
                )
            )
    return events


def _davis_clock_summary(davis_row: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not davis_row:
        return []
    events: list[dict[str, Any]] = []
    for col, label in (("book_2_pkup_delta", "book_2_pkup"), ("avail_2_book_delta", "avail_2_book")):
        if col not in davis_row or davis_row[col] is None:
            continue
        delta = float(davis_row[col])
        if abs(delta) < CLOCK_MIN_H:
            continue
        sign = "+" if delta >= 0 else "−"
        events.append(
            _ledger_row(
                metric=col.replace("_delta", ""),
                delta_usd=None,
                delta_other=delta,
                category="LeadtimeChange",
                driver_text=f"{label} {sign}{abs(delta):.0f}h avail→48hr (Davis)",
                confidence="high",
                evidence_cols=[col],
            )
        )
    return events


def build_material_change_ledger(
    payload: dict[str, Any],
    feat_deltas: pl.DataFrame,
    decomp: dict[str, Any] | None = None,
    *,
    davis_row: dict[str, Any] | None = None,
    lightning: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Merge checkpoint, hourly, display, clock, feature, MDE, and Lightning signals."""
    if decomp:
        payload = {**payload, "decompose": decomp}
    rows: list[dict[str, Any]] = []
    checkpoints = payload.get("checkpoints")
    if checkpoints is None:
        checkpoints = pl.DataFrame()
    display = payload.get("display")
    if display is None:
        display = pl.DataFrame()

    for impact in payload.get("etp_impacts") or []:
        mark = int(impact["mark_hrs"])
        hbp, hsa = _checkpoint_timing(checkpoints, mark)
        rows.append(
            _ledger_row(
                hours_before_pickup=hbp,
                hours_since_available=hsa,
                metric="etp50",
                delta_usd=float(impact["delta_etp50"]),
                delta_other=float(impact.get("delta_etp50_pct") or 0.0),
                category="LeadtimeChange",
                driver_text=impact["text"],
                confidence="high",
                evidence_cols=["delta_etp50"],
            )
        )

    if not feat_deltas.is_empty():
        enriched = feat_deltas
        if "hours_before_pickup" not in enriched.columns and "snapshot_utc_t1" in enriched.columns:
            snaps = payload.get("feature_snaps")
            if isinstance(snaps, pl.DataFrame) and not snaps.is_empty():
                timing = snaps.select(
                    "snapshot_utc",
                    "hours_before_pickup",
                    "hours_since_available",
                ).rename({"snapshot_utc": "snapshot_utc_t1"})
                enriched = enriched.join(timing, on="snapshot_utc_t1", how="left")

        for row in enriched.iter_rows(named=True):
            detp = row.get("delta_etp50")
            if detp is None or abs(float(detp)) < ETP_IMPACT_MIN_USD:
                continue
            dknn = row.get("delta_knn_50")
            category = "LeadtimeChange"
            confidence = "medium"
            evidence = ["delta_etp50"]
            driver = f"hourly model ETP {float(detp):+,.0f}"
            if dknn is not None and abs(float(dknn)) >= KNN_CO_MOVE_MIN_USD:
                category = "QuantileRefresh"
                confidence = "high"
                evidence.append("delta_knn_50")
                driver = (
                    f"quantile refresh: ETP {float(detp):+,.0f}, knn_50 {float(dknn):+,.0f}"
                )
            rows.append(
                _ledger_row(
                    ts=row.get("snapshot_utc_t1"),
                    hours_before_pickup=int(row["hours_before_pickup"])
                    if row.get("hours_before_pickup") is not None
                    else None,
                    hours_since_available=int(row["hours_since_available"])
                    if row.get("hours_since_available") is not None
                    else None,
                    metric="etp50",
                    delta_usd=float(detp),
                    delta_other=float(dknn) if dknn is not None else None,
                    category=category,
                    driver_text=driver,
                    confidence=confidence,
                    evidence_cols=evidence,
                )
            )

        rows.extend(_clock_events_from_deltas(enriched))

    for ev in payload.get("clock_events") or []:
        mark = int(ev["mark_hrs"])
        hbp, hsa = _checkpoint_timing(checkpoints, mark)
        rows.append(
            _ledger_row(
                hours_before_pickup=hbp,
                hours_since_available=hsa,
                metric=ev["text"].split()[0] if ev.get("text") else "clock",
                delta_usd=None,
                delta_other=None,
                category=ev.get("category", "LeadtimeChange"),
                driver_text=ev["text"],
                confidence="high",
                evidence_cols=["book_2_pkup", "avail_2_book"],
            )
        )

    for ev in payload.get("feature_events") or []:
        mark = int(ev["mark_hrs"])
        hbp, hsa = _checkpoint_timing(checkpoints, mark)
        rows.append(
            _ledger_row(
                hours_before_pickup=hbp,
                hours_since_available=hsa,
                metric="feature",
                delta_usd=None,
                category=ev.get("category", "ShipmentChange"),
                driver_text=ev["text"],
                confidence="medium",
                evidence_cols=[ev.get("category", "feature")],
            )
        )

    for ev in payload.get("mde_events") or []:
        mark = int(ev.get("mark_hrs", 999))
        hbp, hsa = _checkpoint_timing(checkpoints, mark)
        rows.append(
            _ledger_row(
                hours_before_pickup=hbp,
                hours_since_available=hsa,
                metric="mde",
                category="MDE",
                driver_text=ev.get("text", "MDE applied"),
                confidence="high",
                evidence_cols=["mde"],
            )
        )

    for ev in payload.get("display_events") or []:
        mark = int(ev.get("mark_hrs", 999))
        hbp, hsa = _checkpoint_timing(checkpoints, mark)
        rows.append(
            _ledger_row(
                hours_before_pickup=hbp,
                hours_since_available=hsa,
                metric="display",
                category="DisplayAdjust",
                driver_text=ev["text"],
                confidence="low",
                evidence_cols=["target1", "target3", "t1_gap"],
                is_pricing_driver=False,
            )
        )

    if not checkpoints.is_empty():
        ordered = checkpoints.sort("mark_hrs", descending=True)
        prev_rows = ordered.to_dicts()
        for prev, cur in pairwise(prev_rows):
            mark = int(cur["mark_hrs"])
            hbp = int(cur.get("hours_before_pickup") or mark)
            for col, metric in (("target1", "target1"), ("target3", "target3")):
                if col not in prev or col not in cur or prev[col] is None or cur[col] is None:
                    continue
                delta = float(cur[col]) - float(prev[col])
                if abs(delta) < TARGET_MIN_USD:
                    continue
                rows.append(
                    _ledger_row(
                        hours_before_pickup=hbp,
                        metric=metric,
                        delta_usd=delta,
                        category="DisplayAdjust",
                        driver_text=f"{metric} {delta:+,.0f} vs prior checkpoint",
                        confidence="low",
                        evidence_cols=[col],
                        is_pricing_driver=False,
                    )
                )
            t1_gap = cur.get("t1_gap")
            prev_gap = prev.get("t1_gap")
            if t1_gap is not None and prev_gap is not None:
                dgap = float(t1_gap) - float(prev_gap)
                if abs(dgap) >= DISPLAY_GAP_MIN_USD:
                    rows.append(
                        _ledger_row(
                            hours_before_pickup=hbp,
                            metric="t1_gap",
                            delta_usd=dgap,
                            category="DisplayAdjust",
                            driver_text=f"T1 gap Δ {dgap:+,.0f} vs prior checkpoint",
                            confidence="low",
                            evidence_cols=["t1_gap"],
                            is_pricing_driver=False,
                        )
                    )

    if not display.is_empty() and display.height >= 2:
        ordered = display.filter(pl.col("target3").is_not_null()).sort(
            "hours_before_pickup", descending=True
        )
        if ordered.height >= 2:
            prev_rows = ordered.to_dicts()
            for prev, cur in pairwise(prev_rows):
                if prev.get("target1") is not None and cur.get("target1") is not None:
                    dt1 = float(cur["target1"]) - float(prev["target1"])
                    if abs(dt1) >= TARGET_MIN_USD:
                        rows.append(
                            _ledger_row(
                                hours_before_pickup=int(cur["hours_before_pickup"]),
                                hours_since_available=int(cur["hours_since_available"])
                                if cur.get("hours_since_available") is not None
                                else None,
                                metric="target1",
                                delta_usd=dt1,
                                category="DisplayAdjust",
                                driver_text=f"Target 1 {dt1:+,.0f} (hourly audit snap)",
                                confidence="low",
                                evidence_cols=["target1"],
                                is_pricing_driver=False,
                            )
                        )

    rows.extend(_davis_clock_summary(davis_row))

    if lightning:
        for key in (
            "hourly_events",
            "endpoint_events",
            "path_checkpoint_events",
            "path_meta_events",
        ):
            for ev in lightning.get(key) or []:
                rows.append(
                    _ledger_row(
                        ts=ev.get("ts"),
                        hours_before_pickup=ev.get("hours_before_pickup"),
                        hours_since_available=ev.get("hours_since_available"),
                        metric=str(ev.get("metric", "clhp")),
                        delta_usd=ev.get("delta_usd"),
                        delta_other=ev.get("delta_other"),
                        category=str(ev.get("category", "ShipmentChange")),
                        driver_text=str(ev.get("driver_text", "")),
                        confidence=str(ev.get("confidence", "medium")),
                        evidence_cols=list(ev.get("evidence_cols") or []),
                        is_pricing_driver=ev.get("is_pricing_driver", True),
                    )
                )

    if not rows:
        return _empty_ledger()

    ledger = pl.DataFrame(rows)
    for col in LEDGER_COLUMNS:
        if col not in ledger.columns:
            ledger = ledger.with_columns(pl.lit(None).alias(col))

    ledger = ledger.select(list(LEDGER_COLUMNS)).with_columns(
        pl.col("hours_before_pickup").cast(pl.Int64, strict=False),
        pl.col("hours_since_available").cast(pl.Int64, strict=False),
        pl.col("is_pricing_driver").fill_null(True),
    )

    return ledger.sort(
        ["hours_before_pickup", "delta_usd"],
        descending=[True, True],
        nulls_last=True,
    )


def attribution_card(ledger: pl.DataFrame, *, top_n: int = 5) -> list[dict[str, Any]]:
    """Top pricing drivers by |Δetp50|, excluding display adjustments."""
    if ledger.is_empty():
        return []
    pricing = ledger.filter(
        (pl.col("is_pricing_driver") == True)
        & (pl.col("metric") == "etp50")
        & pl.col("delta_usd").is_not_null()
    )
    if pricing.is_empty():
        pricing = ledger.filter(pl.col("is_pricing_driver") == True)
    ranked = pricing.with_columns(pl.col("delta_usd").abs().alias("_abs")).sort(
        "_abs", descending=True
    )
    out: list[dict[str, Any]] = []
    for row in ranked.head(top_n).iter_rows(named=True):
        out.append(
            {
                "metric": row["metric"],
                "delta_usd": row["delta_usd"],
                "category": row["category"],
                "driver_text": row["driver_text"],
                "hours_before_pickup": row["hours_before_pickup"],
                "confidence": row["confidence"],
            }
        )
    return out


def summarize_load(
    payload: dict[str, Any],
    ledger: pl.DataFrame,
    *,
    movement_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executive summary: endpoint shifts, category mix, broker one-liner."""
    sm = payload.get("summary") or {}
    loadnumber = payload.get("loadnumber") or sm.get("loadnumber")
    endpoint = payload.get("endpoint") or {}

    category_counts: dict[str, int] = {}
    if not ledger.is_empty() and "category" in ledger.columns:
        pricing = ledger.filter(pl.col("is_pricing_driver") == True)
        for cat in pricing["category"].unique().to_list():
            category_counts[str(cat)] = int(pricing.filter(pl.col("category") == cat).height)

    lines: list[str] = []
    if sm.get("shift_amt") is not None and sm.get("shift_pct") is not None:
        lines.append(
            f"Model ETP50 avail→48hr: ${sm['shift_amt']:+,.0f} ({sm['shift_pct'] * 100:+.1f}%)"
        )
    if sm.get("t1_gap_mean") is not None:
        lines.append(f"T1 display avg {sm['t1_gap_mean']:+,.0f} vs model")

    primary = (movement_row or {}).get("primary_category") or "Unknown"
    archetype = (movement_row or {}).get("path_archetype")
    mde_n = (sm.get("mde") or {}).get("n_events", 0)
    cost = endpoint.get("cost") or endpoint.get("carrier_shipment_charges_total")

    card = attribution_card(ledger, top_n=3)
    driver_bits = [c["driver_text"] for c in card[:2]]
    driver_clause = "; ".join(driver_bits) if driver_bits else "no material pricing steps flagged"

    one_liner = (
        f"Load {loadnumber}"
        + (f" ({archetype})" if archetype else "")
        + f": {primary} — "
        + (lines[0] if lines else "lifecycle shift unavailable")
        + f" — drivers: {driver_clause}."
    )
    if cost is not None and sm.get("etp50_48hr") is not None:
        one_liner += f" Realized cost ${float(cost):,.0f} vs book ETP50 ${sm['etp50_48hr']:,.0f}."
    if mde_n:
        one_liner += f" {mde_n} MDE event(s)."

    return {
        "loadnumber": loadnumber,
        "primary_category": primary,
        "path_archetype": archetype,
        "endpoint_shifts": {
            k: sm.get(k)
            for k in (
                "etp50_avail",
                "etp50_48hr",
                "shift_amt",
                "shift_pct",
                "t1_gap_mean",
                "t3_gap_mean",
            )
        },
        "category_counts": category_counts,
        "attribution_card": card,
        "one_liner": one_liner,
        "movement_flags": movement_row,
    }


def pricing_accuracy_for_load(
    loadnumber: int,
    *,
    etp50_avail: float | None = None,
    data_dir: Path | str | None = None,
    davis_cache: Path | str | None = None,
    endpoint: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """ETP p50 MAE @ available and @ book for a covered (booked) load.

    Returns ``None`` when survival/cost/book-quote inputs are unavailable.
    """
    paths = _resolve_paths(data_dir, davis_cache=davis_cache)
    if not paths["survival"].is_file():
        return None

    surv = pl.read_parquet(paths["survival"]).filter(pl.col(ID_COL) == loadnumber)
    if surv.is_empty() or surv["booked_on_utc"].null_count() == surv.height:
        return {"covered": False, "reason": "not_booked"}

    frame = surv.select(
        ID_COL,
        "booked_on_utc",
        *[c for c in ("etp50_avail", "made_available_utc") if c in surv.columns],
    )
    frame = join_lc_carrier_cost(frame, data_dir=paths["data_dir"])
    if COST_COL not in frame.columns or frame[COST_COL].null_count() == frame.height:
        ep_cost = None
        if endpoint:
            ep_cost = endpoint.get("cost") or endpoint.get("carrier_shipment_charges_total")
        if ep_cost is not None:
            frame = frame.with_columns(pl.lit(float(ep_cost)).alias(COST_COL))

    if etp50_avail is not None:
        frame = frame.with_columns(pl.lit(float(etp50_avail)).alias("etp50_avail"))
    elif "etp50_avail" not in frame.columns or frame["etp50_avail"].null_count() == frame.height:
        return None

    frame = _join_book_quotes_readonly(frame, paths)

    timings: list[dict[str, Any]] = []
    for timing, timing_label, col in (
        ("avail", "Available", "etp50_avail"),
        ("book", "At book", "etp50_book"),
    ):
        row = _score_pricing_slice(
            frame,
            col,
            timing=timing,
            timing_label=timing_label,
            quote="etp50",
            quote_label="ETP p50",
            nominal=0.50,
        )
        if row:
            timings.append(row)

    if not timings:
        return None

    booked_on = frame["booked_on_utc"][0]
    realized = frame[COST_COL][0] if COST_COL in frame.columns else None
    return {
        "covered": True,
        "booked_on_utc": str(booked_on) if booked_on is not None else None,
        "realized_cost_usd": float(realized) if realized is not None else None,
        "timings": timings,
    }


def format_pricing_accuracy(pricing: dict[str, Any] | None) -> list[str]:
    """Human-readable lines for CLI / notebook display."""
    if not pricing:
        return []
    if not pricing.get("covered"):
        reason = pricing.get("reason") or "unknown"
        return [f"Pricing accuracy: skipped ({reason.replace('_', ' ')})"]

    lines = ["Pricing accuracy (covered load):"]
    realized = pricing.get("realized_cost_usd")
    if realized is not None:
        lines.append(f"  Realized cost: ${realized:,.0f}")
    if pricing.get("booked_on_utc"):
        lines.append(f"  Booked on: {pricing['booked_on_utc']}")

    for row in pricing.get("timings") or []:
        quote = row.get("mean_quote_usd")
        mae = row.get("mae_usd")
        att = row.get("attainment")
        gap = row.get("gap_pp")
        label = row.get("timing_label") or row.get("timing")
        if quote is None or mae is None:
            continue
        att_s = f"{att * 100:.0f}%" if att is not None else "n/a"
        gap_s = f"{gap:+.0f}pp" if gap is not None else "n/a"
        lines.append(
            f"  ETP p50 @ {label}: ${quote:,.0f} → MAE ${mae:,.0f} (att {att_s}, gap {gap_s})"
        )
    return lines


def explain_load(
    lake: EtpLake,
    loadnumber: int,
    *,
    mde_cache: Path | str | None = None,
    query_mde: bool = False,
    davis_cache: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Single entry: timeline payload, ledger, decomposition, and summary."""
    payload = build_load_timeline(
        lake,
        loadnumber,
        mde_cache=mde_cache,
        query_mde=query_mde,
    )

    feat_snaps = pl.DataFrame()
    try:
        feat_snaps = lake.feature_history(loadnumber=loadnumber)
    except FileNotFoundError:
        pass
    payload["feature_snaps"] = feat_snaps

    feat_deltas = (
        consecutive_feature_deltas(feat_snaps.filter(pl.col(ID_COL) == loadnumber))
        if not feat_snaps.is_empty()
        else pl.DataFrame()
    )
    payload["feat_deltas"] = feat_deltas

    decomp: dict[str, Any] = {}
    try:
        decomp = lake.decompose(loadnumber=loadnumber)
    except (FileNotFoundError, ValueError):
        pass
    payload["decompose"] = decomp

    paths = _resolve_paths(data_dir, davis_cache=davis_cache)
    davis_row: dict[str, Any] | None = None
    movement_row: dict[str, Any] | None = None
    if paths["davis"].is_file():
        davis = pl.read_parquet(paths["davis"]).filter(pl.col(ID_COL) == loadnumber)
        if not davis.is_empty():
            davis_row = enrich_movement_flags(davis).row(0, named=True)
            movement_row = davis_row
    if paths["labeled"].is_file():
        labeled = pl.read_parquet(paths["labeled"]).filter(pl.col(ID_COL) == loadnumber)
        if not labeled.is_empty():
            movement_row = {**(movement_row or {}), **labeled.row(0, named=True)}

    feat_snaps = payload.get("feature_snaps")
    if not isinstance(feat_snaps, pl.DataFrame):
        feat_snaps = pl.DataFrame()
    lightning = build_lightning_tracking(
        feat_snaps,
        loadnumber,
        davis_row=davis_row,
    )

    ledger = build_material_change_ledger(
        payload,
        feat_deltas,
        decomp,
        davis_row=davis_row,
        lightning=lightning,
    )
    summary = summarize_load(payload, ledger, movement_row=movement_row)
    endpoint_shifts = summary.get("endpoint_shifts") or {}
    pricing_accuracy = pricing_accuracy_for_load(
        loadnumber,
        etp50_avail=endpoint_shifts.get("etp50_avail"),
        data_dir=data_dir,
        davis_cache=paths["davis"] if paths["davis"].is_file() else None,
        endpoint=payload.get("endpoint"),
    )

    return {
        "loadnumber": loadnumber,
        "payload": payload,
        "ledger": ledger,
        "decompose": decomp,
        "lightning": lightning,
        "summary": summary,
        "movement": movement_row,
        "davis": davis_row,
        "pricing_accuracy": pricing_accuracy,
    }


def ledger_to_json(ledger: pl.DataFrame) -> list[dict[str, Any]]:
    """Serialize ledger for CLI / notebook export."""
    if ledger.is_empty():
        return []
    return json.loads(ledger.write_json())


__all__ = [
    "COHORT_HC",
    "COHORT_MANUAL",
    "COHORT_VOLATILE_LANE",
    "DEFAULT_AVAIL_END",
    "DEFAULT_AVAIL_START",
    "LEDGER_COLUMNS",
    "attribution_card",
    "build_material_change_ledger",
    "explain_load",
    "format_pricing_accuracy",
    "ledger_to_json",
    "pricing_accuracy_for_load",
    "resolve_load_id",
    "select_outlier_cohort",
    "summarize_load",
]

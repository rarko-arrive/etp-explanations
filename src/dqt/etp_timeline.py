"""Single-load ETP lifecycle timeline — model output, audit display, inflections."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

from dqt.etp_mart import consecutive_feature_deltas
from dqt.score.constants import COST_COL, ID_COL
from dqt.viz import (
    C_ETP,
    C_LIGHTNING,
    C_OTHER,
    DEEMPH,
    apply_style,
    escape_mpl_text,
    titles,
)

REALIZED_COST_COLOR = "#34495e"

if TYPE_CHECKING:
    from dqt.etp_lake import EtpLake

CHECKPOINT_HRS: tuple[int, ...] = (168, 144, 120, 96, 72, 48, 24)
MARK_HRS: tuple[int, ...] = (999, *CHECKPOINT_HRS)
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

DISPLAY_METRICS: tuple[tuple[str, str, str], ...] = (
    ("etp50", "model", "ETP50"),
    ("target1", "audit", "Target 1"),
    ("target3", "audit", "Target 3"),
)

_CLOCK_COLS: tuple[str, ...] = ("book_2_pkup", "avail_2_book")
_ANNOTATION_NUMERIC_COLS: tuple[str, ...] = (
    "total_charges",
    "hard_ft_cnt",
    "dat_rate",
    "fuel_cost",
    "lag7_cpm_x_miles",
)
_ANNOTATION_CATEGORICAL_COLS: tuple[str, ...] = ("load_type", "path_taken", "override_flag")
_INDEX_ANNOTATION_LABELS: dict[str, str] = {
    "dat_rate": "DAT",
    "fuel_cost": "fuel",
    "lag7_cpm_x_miles": "lag7 CPM",
}
_CHARGE_ANNOTATION_EPS = 50.0
_HARD_FT_ANNOTATION_EPS = 1.0
_ETP_IMPACT_MIN_USD = 25.0
_INDEX_REL_DELTA_MIN = 0.10
_DISPLAY_GAP_ANNOT_MIN_USD = 40.0
_TARGET_PULLBACK_MIN_USD = 5.0


def _at_mark(df: pl.DataFrame, col: str, mark_hrs: int) -> pl.DataFrame:
    sub = df.filter(pl.col(col).is_not_null())
    sort_cols = [ID_COL]
    if "hours_since_available" in sub.columns:
        sort_cols.append("hours_since_available")
    if "snapshot_utc" in sub.columns:
        sort_cols.append("snapshot_utc")
    if mark_hrs == 999:
        return sub.sort(sort_cols).group_by(ID_COL).agg(pl.col(col).first().alias(col))
    return (
        sub.filter(pl.col("hours_before_pickup") >= mark_hrs)
        .sort([ID_COL, "hours_before_pickup"])
        .group_by(ID_COL)
        .agg(pl.col(col).first().alias(col))
    )


def _last_per_hbp(hist: pl.DataFrame, source: str, value_cols: list[str]) -> pl.DataFrame:
    sub = hist.filter(pl.col("source") == source).sort("snapshot_utc")
    cols = [c for c in value_cols if c in sub.columns]
    if sub.is_empty() or not cols:
        return pl.DataFrame()
    return sub.group_by("hours_before_pickup").agg([pl.col(c).last().alias(c) for c in cols])


def _load_slider_history(lake: EtpLake, loadnumber: int) -> pl.DataFrame:
    try:
        hist = lake.history(loadnumber=loadnumber)
    except FileNotFoundError:
        return pl.DataFrame()
    if hist.is_empty():
        return pl.DataFrame()
    return hist.sort(["hours_before_pickup", "snapshot_utc"], descending=[True, False])


def _join_model_audit(hist: pl.DataFrame) -> pl.DataFrame:
    model = _last_per_hbp(hist, "model", ["etp50", "etp10", "hours_since_available"])
    audit = _last_per_hbp(hist, "audit", ["target1", "target3"])
    if model.is_empty():
        return pl.DataFrame()
    out = model.join(audit, on="hours_before_pickup", how="left").sort("hours_before_pickup", descending=True)
    if "target1" in out.columns and "etp50" in out.columns:
        out = out.with_columns(
            (pl.col("target1") - pl.col("etp50")).alias("t1_gap"),
            (pl.col("target3") - pl.col("etp50")).alias("t3_gap"),
        )
    return out


def _checkpoint_feature_values(feat: pl.DataFrame, loadnumber: int) -> dict[int, dict[str, Any]]:
    sub = feat.filter(pl.col(ID_COL) == loadnumber)
    if sub.is_empty():
        return {}
    out: dict[int, dict[str, Any]] = {}
    cols = [c for c in (*_ANNOTATION_NUMERIC_COLS, *_ANNOTATION_CATEGORICAL_COLS, *_CLOCK_COLS) if c in sub.columns]
    for mark in MARK_HRS:
        vals: dict[str, Any] = {}
        for col in cols:
            snap = _at_mark(sub, col, mark)
            if snap.is_empty() or snap[col][0] is None:
                continue
            vals[col] = snap[col][0]
        if vals:
            out[mark] = vals
    return out


def _events_between_checkpoints(v0: dict[str, Any], v1: dict[str, Any], *, mark_hrs: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    mark_label = MARK_LABELS.get(mark_hrs, str(mark_hrs))

    if "total_charges" in v0 and "total_charges" in v1:
        delta = float(v1["total_charges"]) - float(v0["total_charges"])
        if delta > _CHARGE_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "mark_label": mark_label,
                    "text": f"charges +${delta:,.0f}",
                    "category": "ShipmentChange",
                }
            )

    if "hard_ft_cnt" in v0 and "hard_ft_cnt" in v1:
        delta = float(v1["hard_ft_cnt"]) - float(v0["hard_ft_cnt"])
        if abs(delta) >= _HARD_FT_ANNOTATION_EPS:
            events.append(
                {
                    "mark_hrs": mark_hrs,
                    "mark_label": mark_label,
                    "text": f"hard_ft {delta:+.0f} (difficulty)",
                    "category": "DifficultyOverride",
                }
            )

    if "load_type" in v0 and "load_type" in v1 and v0["load_type"] != v1["load_type"] and v1["load_type"] is not None:
        events.append(
            {
                "mark_hrs": mark_hrs,
                "mark_label": mark_label,
                "text": f"equip {v0['load_type']}→{v1['load_type']}",
                "category": "ShipmentChange",
            }
        )

    if (
        "path_taken" in v0
        and "path_taken" in v1
        and v0["path_taken"] != v1["path_taken"]
        and v1["path_taken"] is not None
    ):
        events.append(
            {
                "mark_hrs": mark_hrs,
                "mark_label": mark_label,
                "text": f"path → {v1['path_taken']}",
                "category": "RandomChange",
            }
        )

    return events


def _index_events_from_available(
    cps: dict[int, dict[str, Any]],
    marks: list[int],
) -> list[dict[str, Any]]:
    if 999 not in cps:
        return []
    v_avail = cps[999]
    events: list[dict[str, Any]] = []
    fired: set[str] = set()
    for mark in marks:
        if mark == 999 or mark not in cps:
            continue
        v_mark = cps[mark]
        mark_label = MARK_LABELS.get(mark, str(mark))
        for col, name in _INDEX_ANNOTATION_LABELS.items():
            if col in fired or col not in v_avail or col not in v_mark:
                continue
            base = abs(float(v_avail[col]))
            if base < 1e-6:
                continue
            v0f = float(v_avail[col])
            v1f = float(v_mark[col])
            rel = abs(v1f - v0f) / base
            if rel >= _INDEX_REL_DELTA_MIN:
                sign = "+" if v1f >= v0f else "−"
                events.append(
                    {
                        "mark_hrs": mark,
                        "mark_label": mark_label,
                        "text": f"{name} {sign}{rel * 100:.0f}% vs avail",
                        "category": "RandomChange",
                    }
                )
                fired.add(col)
    return events


def _build_feature_annotations(feat: pl.DataFrame, loadnumber: int) -> list[dict[str, Any]]:
    cps = _checkpoint_feature_values(feat, loadnumber)
    if not cps:
        return []
    marks = list(MARK_HRS)
    events: list[dict[str, Any]] = []
    for m0, m1 in pairwise(marks):
        if m0 not in cps or m1 not in cps:
            continue
        events.extend(_events_between_checkpoints(cps[m0], cps[m1], mark_hrs=m1))
    events.extend(_index_events_from_available(cps, marks))
    return events


def _display_checkpoint_labels(checkpoints: pl.DataFrame) -> list[dict[str, Any]]:
    """Label audit display offset from model at each checkpoint."""
    if checkpoints.is_empty() or "t1_gap" not in checkpoints.columns:
        return []
    out: list[dict[str, Any]] = []
    for row in checkpoints.iter_rows(named=True):
        mark = int(row["mark_hrs"])
        parts: list[str] = []
        t1 = row.get("t1_gap")
        t3 = row.get("t3_gap")
        if t1 is not None and abs(float(t1)) >= 50:
            parts.append(f"T1 {float(t1):+,.0f} vs model")
        if t3 is not None and abs(float(t3)) >= 25:
            parts.append(f"T3 {float(t3):+,.0f} vs model")
        if parts:
            out.append(
                {
                    "mark_hrs": mark,
                    "mark_label": row["mark_label"],
                    "text": " · ".join(parts),
                    "category": "DisplayAdjust",
                }
            )
    return out


def _display_gap_events(display: pl.DataFrame) -> list[dict[str, Any]]:
    """Operational audit-vs-model display adjustments between checkpoints."""
    if display.height < 2 or "t1_gap" not in display.columns:
        return []

    events: list[dict[str, Any]] = []
    ordered = display.filter(pl.col("target3").is_not_null()).sort("hours_before_pickup", descending=True)
    if ordered.height < 2:
        return events

    for prev, cur in pairwise(ordered.iter_rows(named=True)):
        mark_hrs = int(cur["hours_before_pickup"])
        mark = min(MARK_HRS, key=lambda m: abs(m - mark_hrs))
        mark_label = MARK_LABELS.get(mark, str(mark_hrs))

        t1_prev = prev.get("t1_gap")
        t1_cur = cur.get("t1_gap")
        if t1_prev is not None and t1_cur is not None:
            dt1 = float(t1_cur) - float(t1_prev)
            if abs(dt1) >= _DISPLAY_GAP_ANNOT_MIN_USD:
                events.append(
                    {
                        "mark_hrs": mark,
                        "mark_label": mark_label,
                        "text": f"T1 display gap Δ {dt1:+,.0f}",
                        "category": "DisplayAdjust",
                    }
                )

        detp = float(cur["etp50"]) - float(prev["etp50"])
        dt3 = float(cur["target3"]) - float(prev["target3"])
        if detp > 0 and dt3 < -_TARGET_PULLBACK_MIN_USD:
            events.append(
                {
                    "mark_hrs": mark,
                    "mark_label": mark_label,
                    "text": f"T3 pullback (model +{detp:.0f}, display {dt3:.0f})",
                    "category": "DisplayAdjust",
                }
            )
    return events


def _checkpoint_display_table(
    hist: pl.DataFrame,
    loadnumber: int,
    feat: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Model ETP50 + audit targets at explorer checkpoints."""
    model = hist.filter(pl.col("source") == "model")
    audit = hist.filter(pl.col("source") == "audit")
    if model.is_empty():
        return pl.DataFrame()

    clock_src = model
    if feat is not None and not feat.is_empty():
        feat_load = feat.filter(pl.col(ID_COL) == loadnumber)
        if not feat_load.is_empty() and any(c in feat_load.columns for c in _CLOCK_COLS):
            clock_src = feat_load

    rows: list[dict[str, Any]] = []
    for mark in MARK_HRS:
        etp_row = _at_mark(model, "etp50", mark)
        if etp_row.is_empty():
            continue
        row: dict[str, Any] = {
            ID_COL: loadnumber,
            "mark_hrs": mark,
            "mark_label": MARK_LABELS.get(mark, str(mark)),
            "etp50": float(etp_row["etp50"][0]),
        }
        hbp = (
            model.filter(pl.col("hours_before_pickup") >= mark)
            .sort("hours_before_pickup")
            .select("hours_before_pickup")
            .head(1)
        )
        row["hours_before_pickup"] = int(hbp["hours_before_pickup"][0]) if hbp.height else mark

        for col in ("target1", "target3"):
            if audit.is_empty() or col not in audit.columns:
                continue
            t_row = _at_mark(audit, col, mark)
            if not t_row.is_empty() and t_row[col][0] is not None:
                row[col] = float(t_row[col][0])

        for col in _CLOCK_COLS:
            if col not in clock_src.columns:
                continue
            val_row = _at_mark(clock_src, col, mark)
            if not val_row.is_empty() and val_row[col][0] is not None:
                row[col] = float(val_row[col][0])
        rows.append(row)

    if not rows:
        return pl.DataFrame()

    out = pl.DataFrame(rows).sort("mark_hrs", descending=True)
    base_etp = float(out.filter(pl.col("mark_hrs") == 999)["etp50"][0])
    exprs = [
        (pl.col("etp50") / base_etp * 100).alias("etp50_idx"),
        pl.col("etp50").diff(-1).alias("delta_etp50_prev"),
    ]
    if "target1" in out.columns:
        base_t1 = out.filter(pl.col("mark_hrs") == 999)["target1"].first()
        if base_t1 is not None:
            exprs.append((pl.col("target1") / float(base_t1) * 100).alias("target1_idx"))
        exprs.append((pl.col("target1") - pl.col("etp50")).alias("t1_gap"))
    if "target3" in out.columns:
        base_t3 = out.filter(pl.col("mark_hrs") == 999)["target3"].first()
        if base_t3 is not None:
            exprs.append((pl.col("target3") / float(base_t3) * 100).alias("target3_idx"))
        exprs.append((pl.col("target3") - pl.col("etp50")).alias("t3_gap"))
    return out.with_columns(exprs)


def _attach_timeline_x(display: pl.DataFrame, checkpoints: pl.DataFrame) -> pl.DataFrame:
    """Map checkpoints to a forward lifecycle x-coordinate (hours since available)."""
    if "hours_since_available" not in display.columns:
        return checkpoints.with_columns(pl.col("hours_before_pickup").alias("timeline_x"))
    hsa_map = display.select("hours_before_pickup", "hours_since_available").unique("hours_before_pickup", keep="last")
    avail_x = int(display["hours_since_available"].min())
    cp = checkpoints.join(hsa_map, on="hours_before_pickup", how="left")
    return cp.with_columns(
        pl.when(pl.col("mark_hrs") == 999)
        .then(pl.lit(avail_x))
        .otherwise(pl.col("hours_since_available"))
        .alias("timeline_x")
    ).sort("timeline_x")


def _clock_events(checkpoints: pl.DataFrame) -> list[dict[str, Any]]:
    if checkpoints.height < 2:
        return []
    events: list[dict[str, Any]] = []
    ordered = checkpoints.sort("mark_hrs", descending=True)
    for prev, cur in pairwise(ordered.iter_rows(named=True)):
        mark = int(cur["mark_hrs"])
        for col, label in (("book_2_pkup", "book_2_pkup"), ("avail_2_book", "avail_2_book")):
            if col not in cur or col not in prev:
                continue
            delta = float(cur[col]) - float(prev[col])
            if abs(delta) < 0.5:
                continue
            sign = "+" if delta >= 0 else "−"
            events.append(
                {
                    "mark_hrs": mark,
                    "mark_label": cur["mark_label"],
                    "text": f"{label} {sign}{abs(delta):.0f}h",
                    "category": "LeadtimeChange",
                }
            )
    return events


def _checkpoint_etp_impacts(checkpoints: pl.DataFrame) -> list[dict[str, Any]]:
    if checkpoints.height < 2:
        return []
    impacts: list[dict[str, Any]] = []
    ordered = checkpoints.sort("mark_hrs", descending=True)
    for prev, cur in pairwise(ordered.iter_rows(named=True)):
        delta = float(cur["etp50"]) - float(prev["etp50"])
        if abs(delta) < _ETP_IMPACT_MIN_USD:
            continue
        pct = delta / float(prev["etp50"]) if prev["etp50"] else 0.0
        impacts.append(
            {
                "mark_hrs": int(cur["mark_hrs"]),
                "mark_label": cur["mark_label"],
                "text": f"model ETP {delta:+,.0f} ({pct * 100:+.1f}%)",
                "delta_etp50": delta,
                "delta_etp50_pct": pct,
            }
        )
    return impacts


def _load_mde_for_load(
    lake: EtpLake,
    loadnumber: int,
    *,
    mde_cache: Path | str | None = None,
    query_sf: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Market Movement MDE events for one load (cache first, optional SF)."""
    from dqt.etp_slider.drift.mde_timeline import (
        MARK_LABELS as MDE_MARK_LABELS,
    )
    from dqt.etp_slider.drift.mde_timeline import (
        build_mde_explorer_overlay,
        load_mde_timeline_events,
    )

    events_df = pl.DataFrame()
    cache_candidates: list[Path] = []
    if mde_cache is not None:
        cache_candidates.append(Path(mde_cache))
    cache_dir = lake.paths.cache_dir
    cache_candidates.extend(sorted(cache_dir.glob("explorer-mde-timeline*.parquet")))

    for path in cache_candidates:
        if path.is_file():
            events_df = pl.read_parquet(path).filter(pl.col("loadnumber") == loadnumber)
            if not events_df.is_empty():
                break

    if events_df.is_empty() and query_sf:
        events_df = load_mde_timeline_events([loadnumber], force=True)

    meta: dict[str, Any] = {"n_events": 0, "market_movement": False, "all_types": []}
    annotations: list[dict[str, Any]] = []
    if events_df.is_empty():
        return annotations, meta

    meta["n_events"] = events_df.height
    meta["market_movement"] = True
    if "mde_description" in events_df.columns:
        meta["all_types"] = events_df["mde_description"].unique().to_list()

    pickup = None
    try:
        io = lake.io_state(loadnumber)
        pickup = (io.get("endpoint") or {}).get("pickup_appt_latest_utc")
    except FileNotFoundError:
        pass

    if pickup is not None:
        timing = pl.DataFrame({ID_COL: [loadnumber], "pickup_appt_latest_utc": [pickup]})
        ann_map, ann_meta = build_mde_explorer_overlay(events_df, timing, [loadnumber])
        annotations = ann_map.get(str(loadnumber), [])
        meta.update(ann_meta.get(str(loadnumber), {}))
    else:
        for row in events_df.sort("applied_at_utc").iter_rows(named=True):
            desc = str(row.get("mde_description") or "Market Movement")
            annotations.append(
                {
                    "mark_hrs": 999,
                    "mark_label": MDE_MARK_LABELS.get(999, "Available"),
                    "text": f"MDE: {desc}",
                    "category": "MDE",
                    "applied_at_utc": str(row.get("applied_at_utc")),
                }
            )

    for ann in annotations:
        if "mark_label" not in ann and "label" in ann:
            ann["mark_label"] = ann.pop("label")

    return annotations, meta


def build_load_timeline(
    lake: EtpLake,
    loadnumber: int,
    *,
    mde_cache: Path | str | None = None,
    query_mde: bool = False,
) -> dict[str, Any]:
    """Assemble lifecycle series, checkpoints, display gaps, and inflections."""
    hist = _load_slider_history(lake, loadnumber)
    if hist.is_empty():
        raise ValueError(f"No ETP history for load {loadnumber}")

    display = _join_model_audit(hist)

    feat_snaps = pl.DataFrame()
    try:
        feat_snaps = lake.feature_history(loadnumber=loadnumber)
    except FileNotFoundError:
        pass

    checkpoints = _checkpoint_display_table(hist, loadnumber, feat=feat_snaps)
    feature_events = _build_feature_annotations(feat_snaps, loadnumber) if not feat_snaps.is_empty() else []
    clock_events = _clock_events(checkpoints)
    etp_impacts = _checkpoint_etp_impacts(checkpoints)
    display_events = _display_gap_events(display) + _display_checkpoint_labels(checkpoints)
    mde_events, mde_meta = _load_mde_for_load(lake, loadnumber, mde_cache=mde_cache, query_sf=query_mde)

    endpoint: dict[str, Any] | None = None
    try:
        io = lake.io_state(loadnumber)
        if "endpoint" in io:
            endpoint = io["endpoint"]
    except FileNotFoundError:
        pass

    model_path = display.select(
        [
            c
            for c in (
                "hours_before_pickup",
                "hours_since_available",
                "etp50",
                "etp10",
                "target1",
                "target3",
                "t1_gap",
                "t3_gap",
            )
            if c in display.columns
        ]
    )

    summary: dict[str, Any] = {
        "loadnumber": loadnumber,
        "n_model_snaps": hist.filter(pl.col("source") == "model").height,
        "n_audit_snaps": hist.filter(pl.col("source") == "audit").height,
        "n_display_hours": display.height,
    }

    if not checkpoints.is_empty():
        avail = checkpoints.filter(pl.col("mark_hrs") == 999)
        hr48 = checkpoints.filter(pl.col("mark_hrs") == 48)
        if avail.height and hr48.height:
            v0 = float(avail["etp50"][0])
            v1 = float(hr48["etp50"][0])
            summary.update(
                {
                    "etp50_avail": v0,
                    "etp50_48hr": v1,
                    "shift_amt": v1 - v0,
                    "shift_pct": (v1 - v0) / v0 if v0 else None,
                }
            )
        if "t1_gap" in checkpoints.columns:
            gaps = checkpoints.filter(pl.col("t1_gap").is_not_null())
            if gaps.height:
                summary["t1_gap_mean"] = float(gaps["t1_gap"].mean())
                summary["t3_gap_mean"] = float(gaps["t3_gap"].mean())
                summary["t1_gap_avail"] = float(avail["t1_gap"][0]) if avail.height else None
                summary["t3_gap_avail"] = (
                    float(avail["t3_gap"][0]) if avail.height and "t3_gap" in avail.columns else None
                )

    if endpoint:
        for k in ("target1_shift_pct", "target3_shift_pct", "target_pullback_ind"):
            if k in endpoint and endpoint[k] is not None:
                summary[k] = endpoint[k]

    summary["mde"] = mde_meta

    deltas = (
        consecutive_feature_deltas(feat_snaps.filter(pl.col(ID_COL) == loadnumber))
        if not feat_snaps.is_empty()
        else pl.DataFrame()
    )
    top_steps = (
        deltas.filter(pl.col("delta_etp50").abs() >= _ETP_IMPACT_MIN_USD).sort("delta_etp50", descending=True).head(5)
        if not deltas.is_empty()
        else pl.DataFrame()
    )

    return {
        "loadnumber": loadnumber,
        "history": hist,
        "display": display,
        "path": model_path,
        "checkpoints": checkpoints,
        "feature_events": feature_events,
        "clock_events": clock_events,
        "display_events": display_events,
        "mde_events": mde_events,
        "etp_impacts": etp_impacts,
        "top_steps": top_steps,
        "endpoint": endpoint,
        "summary": summary,
    }


def _resolve_realized_cost(
    loadnumber: int,
    endpoint: dict[str, Any] | None,
    *,
    data_dir: Path | str | None = None,
) -> float | None:
    """Covered carrier cost from lake endpoint or features.parquet fallback."""
    ep = endpoint or {}
    for key in ("cost", "carrier_shipment_charges_total", COST_COL):
        val = ep.get(key)
        if val is None:
            continue
        try:
            cost = float(val)
        except (TypeError, ValueError):
            continue
        if cost > 0:
            return cost

    from dqt import resolve_data_dir
    from dqt.etp_slider.problem_loads.leadtime import join_lc_carrier_cost

    frame = join_lc_carrier_cost(
        pl.DataFrame({ID_COL: [loadnumber]}),
        data_dir=resolve_data_dir(data_dir),
    )
    if COST_COL not in frame.columns or frame[COST_COL].null_count() == frame.height:
        return None
    cost = frame[COST_COL][0]
    if cost is None:
        return None
    try:
        out = float(cost)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def plot_etp_timeline(
    lake: EtpLake,
    loadnumber: int,
    *,
    show: bool = True,
    title: str | None = None,
    mde_cache: Path | str | None = None,
    query_mde: bool = False,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Plot model ETP + audit display targets with checkpoint inflections."""
    import matplotlib.pyplot as plt

    payload = build_load_timeline(lake, loadnumber, mde_cache=mde_cache, query_mde=query_mde)
    display = payload["display"]
    checkpoints = payload["checkpoints"]
    if display.is_empty() or checkpoints.is_empty():
        raise ValueError(f"Insufficient timeline data for load {loadnumber}")

    plot_display = (
        display.sort("hours_since_available")
        if "hours_since_available" in display.columns
        else display.sort("hours_before_pickup", descending=True)
    )
    x_col = "hours_since_available" if "hours_since_available" in plot_display.columns else "hours_before_pickup"
    cp = _attach_timeline_x(display, checkpoints).sort("timeline_x")
    x = plot_display[x_col].to_list()

    apply_style()
    fig, (ax1, ax2) = plt.subplots(
        2,
        1,
        figsize=(11, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.4, 1.2], "hspace": 0.08},
    )

    ax1.plot(
        x, plot_display["etp50"].to_list(), color=C_ETP, linewidth=2.2, marker=".", markersize=4, label="Model ETP50"
    )
    if "target3" in plot_display.columns and plot_display["target3"].null_count() < plot_display.height:
        ax1.plot(
            x,
            plot_display["target3"].to_list(),
            color=C_LIGHTNING,
            linewidth=1.8,
            linestyle="-",
            alpha=0.9,
            label="Audit Target 3 (display)",
        )
    if "target1" in plot_display.columns and plot_display["target1"].null_count() < plot_display.height:
        ax1.plot(
            x,
            plot_display["target1"].to_list(),
            color=C_OTHER,
            linewidth=1.8,
            linestyle="--",
            alpha=0.9,
            label="Audit Target 1 (display)",
        )

    realized_cost = _resolve_realized_cost(loadnumber, payload.get("endpoint"), data_dir=data_dir)
    if realized_cost is not None:
        ax1.axhline(
            realized_cost,
            color=REALIZED_COST_COLOR,
            linestyle=(0, (6, 4)),
            linewidth=2.0,
            label=escape_mpl_text(f"Covered carrier cost (${realized_cost:,.0f})"),
            zorder=2,
        )
        payload.setdefault("summary", {})["realized_cost_usd"] = realized_cost

    sm = payload["summary"]
    mde_note = "no MDE" if not sm.get("mde", {}).get("n_events") else f"{sm['mde']['n_events']} MDE"
    if sm.get("shift_amt") is not None and sm.get("shift_pct") is not None:
        gap_note = ""
        if sm.get("t1_gap_mean") is not None:
            gap_note = f" · T1 display avg {sm['t1_gap_mean']:+,.0f} vs model"
        cost_note = f" · covered cost ${realized_cost:,.0f}" if realized_cost is not None else ""
        subtitle = f"model avail→48hr: ${sm['shift_amt']:+,.0f} ({sm['shift_pct'] * 100:+.1f}%){gap_note}{cost_note}  ·  {mde_note}"
    elif realized_cost is not None:
        subtitle = f"covered carrier cost ${realized_cost:,.0f}  ·  {mde_note}"
    else:
        subtitle = mde_note
    titles(ax1, title or f"Load {loadnumber} — model vs display lifecycle", subtitle)

    impact_by_mark = {e["mark_hrs"]: e for e in payload["etp_impacts"]}
    for row in cp.iter_rows(named=True):
        tx = float(row["timeline_x"])
        ax1.axvline(tx, color=DEEMPH, linestyle=":", linewidth=0.9, alpha=0.7)
        impact = impact_by_mark.get(int(row["mark_hrs"]))
        if impact:
            ax1.annotate(
                escape_mpl_text(impact["text"]),
                xy=(tx, float(row["etp50"])),
                xytext=(0, 10),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color=C_ETP,
                fontweight="semibold",
            )

    cp_x = cp["timeline_x"].to_list()
    ax2.plot(cp_x, cp["etp50_idx"].to_list(), "s-", color=C_ETP, linewidth=2, markersize=5, label="Model ETP50 idx")
    if "target3_idx" in cp.columns:
        ax2.plot(
            cp_x,
            cp["target3_idx"].to_list(),
            "o--",
            color=C_LIGHTNING,
            linewidth=1.8,
            markersize=4,
            label="Target 3 idx",
        )
    if "target1_idx" in cp.columns:
        ax2.plot(
            cp_x,
            cp["target1_idx"].to_list(),
            "^--",
            color=C_OTHER,
            linewidth=1.8,
            markersize=4,
            label="Target 1 idx",
        )
    ax2.axhline(100, color=DEEMPH, linestyle="--", alpha=0.6)
    ax2.set_ylabel("Index (avail=100)")
    x_label = (
        "Hours since available (→ pickup)"
        if x_col == "hours_since_available"
        else "Lifecycle checkpoint (Available → pickup)"
    )
    ax2.set_xlabel(x_label)

    events = payload["feature_events"] + payload["clock_events"] + payload["display_events"] + payload["mde_events"]
    mark_x = {int(r["mark_hrs"]): float(r["timeline_x"]) for r in cp.iter_rows(named=True)}
    y_cursor = 0.0
    for ev in sorted(events, key=lambda e: mark_x.get(int(e["mark_hrs"]), 0.0)):
        mark = int(ev["mark_hrs"])
        cp_row = cp.filter(pl.col("mark_hrs") == mark)
        if cp_row.is_empty():
            continue
        tx = float(cp_row["timeline_x"][0])
        idx = float(cp_row["etp50_idx"][0])
        color = "#52514e"
        if ev.get("category") == "MDE":
            color = "#e34948"
        elif ev.get("category") == "DisplayAdjust":
            color = C_LIGHTNING
        y_cursor = max(y_cursor, idx + 4)
        ax2.annotate(
            escape_mpl_text(ev["text"]),
            xy=(tx, idx),
            xytext=(0, 8 + (y_cursor - idx) * 0.12),
            textcoords="offset points",
            ha="center",
            fontsize=7.5,
            color=color,
            rotation=35,
        )

    ax2.set_xticks(cp["timeline_x"].to_list())
    ax2.set_xticklabels(cp["mark_label"].to_list(), rotation=35, ha="right")
    ax1.legend(loc="upper left", fontsize=9)
    ax2.legend(loc="upper left", fontsize=9)
    fig.tight_layout()

    payload["figure"] = fig
    if show:
        plt.show()
    return payload


__all__ = [
    "DISPLAY_METRICS",
    "MARK_HRS",
    "MARK_LABELS",
    "REALIZED_COST_COLOR",
    "_resolve_realized_cost",
    "build_load_timeline",
    "plot_etp_timeline",
]

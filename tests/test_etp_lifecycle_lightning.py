"""Unit tests for Lightning cost tracking helpers."""

from __future__ import annotations

import polars as pl
import pytest

from dqt.etp_lifecycle.lightning import (
    DEFAULT_ABS_USD,
    DEFAULT_REL_PCT,
    aggregate_clhp_misses_weekly,
    build_clhp_miss_frame,
    build_lightning_calibration_frame,
    build_lightning_tracking,
    lightning_endpoint_events,
    lightning_events_from_deltas,
    summarize_clhp_miss_context,
    sweep_lightning_thresholds,
)


def test_lightning_endpoint_events_abs_gate():
    row = {
        "clhp_pred_tot_cost_delta": 75.0,
        "clhp_pred_tot_cost_avail": 1000.0,
    }
    events = lightning_endpoint_events(row, abs_usd=50.0, rel_pct=0.10)
    assert len(events) == 1
    assert events[0]["delta_usd"] == 75.0


def test_lightning_endpoint_events_rel_gate():
    row = {
        "clhp_pred_tot_cost_delta": 30.0,
        "clhp_pred_tot_cost_avail": 200.0,
    }
    events = lightning_endpoint_events(row, abs_usd=50.0, rel_pct=0.10)
    assert len(events) == 1
    assert events[0]["delta_other"] == pytest.approx(0.15)


def test_lightning_events_from_deltas():
    deltas = pl.DataFrame(
        {
            "delta_clhp_pred_tot_cost": [120.0],
            "clhp_pred_tot_cost": [1000.0],
            "delta_etp50": [40.0],
            "snapshot_utc_t1": [None],
        }
    )
    events = lightning_events_from_deltas(deltas, abs_usd=50.0, rel_pct=0.10)
    assert len(events) == 1
    assert "Lightning" in events[0]["driver_text"]


def test_build_lightning_tracking_empty():
    out = build_lightning_tracking(pl.DataFrame(), 1)
    assert out["hourly_events"] == []
    assert out["endpoint_events"] == []


def test_sweep_lightning_thresholds():
    frame = pl.DataFrame(
        {
            "clhp_pred_tot_cost_delta": [50.0, 200.0, 5.0],
            "clhp_pred_tot_cost_avail": [1000.0, 1000.0, 1000.0],
            "clhp_change_ind": [1, 1, 0],
            "shipment_change_ind": [1, 0, 0],
        }
    )
    sweep = sweep_lightning_thresholds(frame, pct_grid=[0.05, 0.10], abs_usd=50.0)
    assert sweep.height == 2
    assert "precision_vs_clhp_change_ind" in sweep.columns


def test_calibration_frame_joins_labels():
    davis = pl.DataFrame(
        {
            "loadnumber": [1, 2],
            "clhp_pred_tot_cost_delta": [100.0, 10.0],
            "clhp_pred_tot_cost_avail": [1000.0, 1000.0],
            "clhp_pred_line_delta": [0.0, 0.0],
            "clhp_pred_line_avail": [900.0, 900.0],
            "total_charges_delta": [0.0, 0.0],
            "hard_ft_delta": [0.0, 0.0],
            "book_2_pkup_delta": [0.0, 0.0],
            "avail_2_book_delta": [0.0, 0.0],
        }
    )
    labeled = pl.DataFrame(
        {
            "loadnumber": [1, 2],
            "primary_category": ["ShipmentChange", "LeadtimeChange"],
        }
    )
    frame = build_lightning_calibration_frame(davis, labeled)
    assert "shipment_change_ind" in frame.columns
    assert "primary_category" in frame.columns
    assert frame.filter(pl.col("loadnumber") == 1)["shipment_change_ind"][0] == 1


def test_clhp_miss_frame_and_weekly_agg():
    davis = pl.DataFrame(
        {
            "loadnumber": [1, 2, 3],
            "clhp_pred_tot_cost_delta": [10.0, 10.0, 5.0],
            "clhp_pred_tot_cost_avail": [1000.0, 1000.0, 1000.0],
            "clhp_pred_line_delta": [0.0, 0.0, 0.0],
            "clhp_pred_line_avail": [900.0, 900.0, 900.0],
            "total_charges_delta": [60.0, 0.0, 0.0],
            "hard_ft_delta": [0.0, 0.0, 0.0],
            "book_2_pkup_delta": [0.0, 50.0, 0.0],
            "avail_2_book_delta": [0.0, 0.0, 0.0],
            "made_available_utc": [
                "2025-11-24T12:00:00",
                "2025-06-01T12:00:00",
                "2025-03-01T12:00:00",
            ],
        }
    ).with_columns(pl.col("made_available_utc").str.to_datetime())
    labeled = pl.DataFrame(
        {
            "loadnumber": [1, 2, 3],
            "primary_category": ["ShipmentChange", "ShipmentChange", "ShipmentChange"],
        }
    )
    miss = build_clhp_miss_frame(davis, labeled)
    assert miss.height == 3
    assert set(miss["miss_driver"].to_list()) >= {"charge_inc", "clocks_only_label_drift"}
    weekly = aggregate_clhp_misses_weekly(miss)
    assert not weekly.is_empty()
    ctx, by_driver = summarize_clhp_miss_context(miss)
    assert ctx.height == 3
    assert "event_context" in by_driver.columns


def test_tag_holiday_windows():
    from dqt.holidays import tag_holiday_windows

    df = pl.DataFrame(
        {
            "loadnumber": [1, 2],
            "made_available_utc": ["2025-11-26T12:00:00", "2025-03-15T12:00:00"],
        }
    ).with_columns(pl.col("made_available_utc").str.to_datetime())
    tagged = tag_holiday_windows(df, "made_available_utc")
    assert tagged.filter(pl.col("loadnumber") == 1)["in_holiday_window"][0]
    assert not tagged.filter(pl.col("loadnumber") == 2)["in_holiday_window"][0]


@pytest.mark.integration
def test_calibration_on_local_davis_sample():
    from dqt import resolve_data_dir

    base = resolve_data_dir()
    davis_path = base / "etp" / "analysis-davis-raw-2025-01-01_2026-08-28.parquet"
    labeled_path = base / "etp" / "executive-2025-01-01_2026-08-28" / "labeled-cohort.parquet"
    if not davis_path.is_file() or not labeled_path.is_file():
        pytest.skip("local mart caches missing")

    davis = pl.read_parquet(davis_path).head(50_000)
    labeled = pl.read_parquet(labeled_path)
    frame = build_lightning_calibration_frame(davis, labeled)
    sweep = sweep_lightning_thresholds(frame)
    assert not sweep.is_empty()
    at_default = sweep.filter(pl.col("rel_pct_threshold") == DEFAULT_REL_PCT)
    assert at_default.height == 1
    row = at_default.row(0, named=True)
    assert row["abs_usd_gate"] == DEFAULT_ABS_USD

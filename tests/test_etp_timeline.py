"""Tests for :mod:`dqt.etp_timeline`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from dqt.etp_lake import EtpLake, LakePaths
from dqt.etp_slider import HISTORY_CACHE
from dqt.etp_timeline import build_load_timeline, plot_etp_timeline
from dqt.score.constants import ID_COL


def _history_rows() -> pl.DataFrame:
    base = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    for h in range(8):
        hbp = 192 - h * 24
        hsa = h * 24
        ts = base + timedelta(hours=h)
        etp = 1000.0 + h * 50
        rows.append(
            {
                ID_COL: 9394640,
                "snapshot_utc": ts,
                "source": "model",
                "etp10": etp - 80,
                "etp50": etp,
                "target1": None,
                "target3": None,
                "hours_before_pickup": hbp,
                "hours_since_available": hsa,
            }
        )
        rows.append(
            {
                ID_COL: 9394640,
                "snapshot_utc": ts + timedelta(minutes=5),
                "source": "audit",
                "etp10": None,
                "etp50": None,
                "target1": etp - 120,
                "target3": etp - 10,
                "hours_before_pickup": hbp,
                "hours_since_available": hsa,
            }
        )
    return pl.DataFrame(rows)


@pytest.fixture
def timeline_lake(tmp_path: Path) -> EtpLake:
    data = tmp_path / "data"
    cache = tmp_path / "etp"
    cache.mkdir(parents=True)
    lake_root = data / "etp_lake"
    snaps = lake_root / "snapshots" / "ship_month=2026-08"
    snaps.mkdir(parents=True)
    _history_rows().write_parquet(snaps / "part-0.parquet")
    _history_rows().write_parquet(cache / HISTORY_CACHE)
    paths = LakePaths.resolve(data, cache, root=tmp_path)
    return EtpLake(paths.data_dir, paths.cache_dir, root=tmp_path)


class TestEtpTimeline:
    def test_build_load_timeline(self, timeline_lake: EtpLake) -> None:
        payload = build_load_timeline(timeline_lake, 9394640)
        assert payload["display"].height == 8
        assert "target3" in payload["display"].columns
        assert payload["checkpoints"].height >= 2
        assert payload["summary"]["shift_amt"] is not None
        assert payload["summary"]["t1_gap_mean"] is not None
        assert payload["summary"]["mde"]["n_events"] == 0

    def test_plot_etp_timeline(self, timeline_lake: EtpLake) -> None:
        payload = plot_etp_timeline(timeline_lake, 9394640, show=False)
        assert "figure" in payload
        payload["figure"].clf()

    def test_missing_load_raises(self, timeline_lake: EtpLake) -> None:
        with pytest.raises(ValueError, match="No ETP history"):
            build_load_timeline(timeline_lake, 9999999)

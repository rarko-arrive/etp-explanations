"""Tests for :mod:`dqt.etp_timeline`."""

from __future__ import annotations

from pathlib import Path

import pytest

from dqt.etp_lake import EtpLake
from dqt.etp_timeline import build_load_timeline, plot_etp_timeline


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
        fig = payload["figure"]
        ax = fig.axes[1]
        labels = [t.get_text() for t in ax.get_xticklabels()]
        assert labels[0] == "Available"
        assert ax.get_xticks()[0] < ax.get_xticks()[-1]
        fig.clf()

    def test_plot_timeline_x_increases_forward(self, timeline_lake: EtpLake) -> None:
        payload = build_load_timeline(timeline_lake, 9394640)
        from dqt.etp_timeline import _attach_timeline_x

        cp = _attach_timeline_x(payload["display"], payload["checkpoints"]).sort("timeline_x")
        assert cp["timeline_x"][0] < cp["timeline_x"][-1]
        assert cp["mark_label"][0] == "Available"

    def test_resolve_realized_cost_from_endpoint(self) -> None:
        from dqt.etp_timeline import _resolve_realized_cost

        assert _resolve_realized_cost(1, {"cost": 1500.0}) == 1500.0
        assert _resolve_realized_cost(1, {"carrier_shipment_charges_total": 900.0}) == 900.0
        assert _resolve_realized_cost(1, None) is None

    def test_plot_shows_realized_cost_line(self, timeline_lake: EtpLake, monkeypatch) -> None:
        import io

        from dqt.etp_timeline import plot_etp_timeline

        monkeypatch.setattr(
            "dqt.etp_timeline._resolve_realized_cost",
            lambda loadnumber, endpoint, data_dir=None: 1500.0,
        )
        payload = plot_etp_timeline(timeline_lake, 9394640, show=False)
        ax = payload["figure"].axes[0]
        labels = [line.get_label() for line in ax.get_lines()]
        assert any("Covered carrier cost" in label for label in labels)
        buf = io.BytesIO()
        payload["figure"].savefig(buf, format="png")
        assert buf.getbuffer().nbytes > 0
        payload["figure"].clf()

    def test_escape_mpl_text_dollar_amounts(self) -> None:
        from dqt.viz import escape_mpl_text

        assert escape_mpl_text("$+294 (+43.2%) · covered cost $1,650") == (
            r"\$+294 (+43.2%) · covered cost \$1,650"
        )

    def test_missing_load_raises(self, timeline_lake: EtpLake) -> None:
        with pytest.raises(ValueError, match="No ETP history"):
            build_load_timeline(timeline_lake, 9999999)

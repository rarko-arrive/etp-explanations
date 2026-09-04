"""Tests for the ETP explainer HTML frontend."""

from __future__ import annotations

import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import polars as pl
import pytest

from app.explain.render import render_explain_html
from app.explain.view_model import build_view_model, figure_to_png_b64
from dqt.etp_lifecycle import LEDGER_COLUMNS, ledger_to_json


def _minimal_ledger() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "ts": None,
                "hours_before_pickup": 168,
                "hours_since_available": 0,
                "metric": "etp50",
                "delta_usd": 70.0,
                "delta_other": 0.14,
                "category": "LeadtimeChange",
                "driver_text": "model ETP +70 (+14.9%)",
                "confidence": "high",
                "evidence_cols": ["delta_etp50"],
                "is_pricing_driver": True,
            },
            {
                "ts": None,
                "hours_before_pickup": 168,
                "hours_since_available": 0,
                "metric": "display",
                "delta_usd": None,
                "delta_other": None,
                "category": "DisplayAdjust",
                "driver_text": "T1 -60 vs model",
                "confidence": "low",
                "evidence_cols": ["t1_gap"],
                "is_pricing_driver": False,
            },
        ]
    ).cast(
        {
            "hours_before_pickup": pl.Int64,
            "hours_since_available": pl.Int64,
            "delta_usd": pl.Float64,
            "delta_other": pl.Float64,
            "is_pricing_driver": pl.Boolean,
        }
    )


def _minimal_explain_result() -> dict:
    ledger = _minimal_ledger()
    return {
        "loadnumber": 6963033,
        "summary": {
            "loadnumber": 6963033,
            "primary_category": "LeadtimeChange",
            "path_archetype": "late_cliff",
            "one_liner": "Load 6963033: LeadtimeChange — test one-liner.",
            "endpoint_shifts": {
                "etp50_avail": 470.0,
                "etp50_48hr": 641.0,
                "shift_amt": 171.0,
                "shift_pct": 0.363,
                "t1_gap_mean": -70.0,
            },
            "attribution_card": [
                {
                    "metric": "etp50",
                    "delta_usd": 70.0,
                    "category": "LeadtimeChange",
                    "driver_text": "model ETP +70 (+14.9%)",
                    "hours_before_pickup": 168,
                    "confidence": "high",
                }
            ],
            "category_counts": {"LeadtimeChange": 1},
        },
        "ledger": ledger,
        "pricing_accuracy": {
            "covered": True,
            "realized_cost_usd": 600.0,
            "booked_on_utc": "2025-05-19 22:04:00",
            "timings": [
                {
                    "timing": "avail",
                    "timing_label": "Available",
                    "mae_usd": 130.0,
                    "attainment": 0.0,
                    "gap_pp": -50.0,
                    "mean_quote_usd": 470.0,
                }
            ],
        },
        "lightning": {
            "endpoint_summary": {
                "clhp_pred_tot_cost_delta": 14.04,
                "clhp_pred_tot_cost_rel_pct": 0.024,
                "clhp_change_ind": 0,
            }
        },
        "payload": {
            "checkpoints": pl.DataFrame(
                {
                    "mark_hrs": [999, 48],
                    "mark_label": ["Available", "2 days out"],
                    "etp50": [470.0, 641.0],
                    "target1": [420.0, 590.0],
                    "target3": [480.0, 650.0],
                    "t1_gap": [-50.0, -51.0],
                    "t3_gap": [10.0, 9.0],
                    "delta_etp50_prev": [None, 171.0],
                    "hours_before_pickup": [999, 48],
                }
            )
        },
    }


def test_build_view_model_smoke() -> None:
    fig, _ax = plt.subplots(figsize=(4, 3))
    _ax.plot([0, 1], [1, 2])
    vm = build_view_model(_minimal_explain_result(), timeline_figure=fig, cohort="hc_leadtime_isolated", rank=2)

    assert vm["meta"]["loadnumber"] == 6963033
    assert vm["meta"]["cohort"] == "hc_leadtime_isolated"
    assert vm["summary"]["one_liner"].startswith("Load 6963033")
    assert len(vm["ledger"]["pricing"]) == 1
    assert len(vm["ledger"]["display"]) == 1
    assert len(vm["checkpoints"]) == 2
    assert vm["timeline_png_b64"]
    assert vm["lightning"]["endpoint_summary"]["clhp_change_ind"] == 0


def test_figure_to_png_b64() -> None:
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.set_title("test $1,650")
    b64 = figure_to_png_b64(fig)
    assert len(b64) > 100
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", b64)


def test_render_explain_html_injects_json() -> None:
    fig, _ax = plt.subplots(figsize=(4, 3))
    vm = build_view_model(_minimal_explain_result(), timeline_figure=fig)
    html = render_explain_html(vm)

    assert "window.__EXPLAIN__" in html
    assert "6963033" in html
    assert "data:image/png;base64," in html
    assert "</script>" in html
    assert re.search(r"window\.__EXPLAIN__\s*=\s*\{", html)

    match = re.search(r"window\.__EXPLAIN__\s*=\s*(\{.*?\});", html, re.DOTALL)
    assert match is not None
    parsed = json.loads(match.group(1))
    assert parsed["meta"]["loadnumber"] == 6963033


def test_explain_ui_integration(timeline_lake, tmp_path: Path) -> None:
    from scripts.explain_ui import main

    out = tmp_path / "explain-9394640.html"
    rc = main(
        [
            "--load",
            "9394640",
            "--data-dir",
            str(timeline_lake.paths.data_dir),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert out.is_file()
    html = out.read_text()
    assert "9394640" in html
    assert "data:image/png;base64," in html
    assert "Material change ledger" in html


def test_ledger_to_json_matches_columns() -> None:
    ledger = _minimal_ledger()
    rows = ledger_to_json(ledger)
    assert rows
    assert set(rows[0].keys()) == set(LEDGER_COLUMNS)

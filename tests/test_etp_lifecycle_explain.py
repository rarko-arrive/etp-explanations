"""Tests for ETP lifecycle explanation module."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from dqt import resolve_data_dir
from dqt.etp_lake import EtpLake
from dqt.etp_lifecycle import (
    LEDGER_COLUMNS,
    attribution_card,
    build_material_change_ledger,
    explain_load,
    select_outlier_cohort,
)
from dqt.etp_timeline import build_load_timeline


def test_ledger_schema_empty_payload() -> None:
    ledger = build_material_change_ledger({"etp_impacts": []}, pl.DataFrame())
    assert list(ledger.columns) == list(LEDGER_COLUMNS)


def test_attribution_card_excludes_display() -> None:
    ledger = pl.DataFrame(
        [
            {
                "ts": None,
                "hours_before_pickup": 48,
                "hours_since_available": 100,
                "metric": "etp50",
                "delta_usd": 90.0,
                "delta_other": None,
                "category": "LeadtimeChange",
                "driver_text": "model jump",
                "confidence": "high",
                "evidence_cols": ["delta_etp50"],
                "is_pricing_driver": True,
            },
            {
                "ts": None,
                "hours_before_pickup": 48,
                "hours_since_available": 100,
                "metric": "display",
                "delta_usd": None,
                "delta_other": None,
                "category": "DisplayAdjust",
                "driver_text": "T1 gap",
                "confidence": "low",
                "evidence_cols": ["t1_gap"],
                "is_pricing_driver": False,
            },
        ]
    )
    card = attribution_card(ledger)
    assert len(card) == 1
    assert card[0]["category"] == "LeadtimeChange"


def _data_gate() -> Path:
    base = resolve_data_dir()
    hist = base / "etp" / "etp-slider-history.parquet"
    if not hist.is_file():
        pytest.skip("ETP history cache missing")
    return base


@pytest.mark.integration
def test_select_outlier_cohort_smoke() -> None:
    base = _data_gate()
    labeled = base / "etp" / "executive-2025-01-01_2026-08-28" / "labeled-cohort.parquet"
    if not labeled.is_file():
        pytest.skip("labeled cohort missing")
    hc = select_outlier_cohort(data_dir=base, refresh=True, top_n=5)
    assert not hc.is_empty()
    assert "hc_rank" in hc.columns
    assert hc["hc_rank"][0] == 1


@pytest.mark.integration
def test_explain_load_9199475() -> None:
    base = _data_gate()
    lake = EtpLake(base)
    load = 9199475
    try:
        result = explain_load(lake, load, data_dir=base)
    except ValueError as exc:
        pytest.skip(str(exc))

    ledger = result["ledger"]
    assert ledger.height >= 2

    pricing = ledger.filter(pl.col("is_pricing_driver") == True)
    lc_rows = pricing.filter(pl.col("category") == "LeadtimeChange")
    assert lc_rows.height >= 2, "expected ≥2 material LeadtimeChange checkpoint rows"

    quantile = pricing.filter(pl.col("category") == "QuantileRefresh")
    assert quantile.height >= 1, "expected knn co-movement on largest hourly step"

    display = ledger.filter(pl.col("category") == "DisplayAdjust")
    assert display.height >= 1, "display rows segregated"

    davis = result.get("davis") or {}
    if davis.get("book_2_pkup_delta") is not None:
        book_delta = abs(float(davis["book_2_pkup_delta"]))
        assert book_delta > 100

    summary = result["summary"]
    assert "9199475" in summary["one_liner"]
    assert summary["attribution_card"]

    payload = build_load_timeline(lake, load)
    assert payload["clock_events"], "clock_events should populate via feature_history"

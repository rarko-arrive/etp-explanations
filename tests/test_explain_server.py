"""Tests for the interactive ETP explainer FastAPI server."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.explain.server import create_app
from app.explain.service import ExplainOptions
from dqt.etp_lake import EtpLake


@pytest.fixture
def explain_client(timeline_lake: EtpLake, tmp_path: Path) -> TestClient:
    cache_dir = tmp_path / "explain-shipments"
    app = create_app(
        default_opts=ExplainOptions(data_dir=timeline_lake.paths.data_dir, lake=timeline_lake),
        output_dir=cache_dir,
    )
    return TestClient(app)


def test_health(explain_client: TestClient) -> None:
    resp = explain_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_index_returns_search_form(explain_client: TestClient) -> None:
    resp = explain_client.get("/")
    assert resp.status_code == 200
    assert "loadnumber" in resp.text
    assert "/explain/" in resp.text


def test_explain_renders_html(explain_client: TestClient) -> None:
    resp = explain_client.get("/explain/9394640")
    assert resp.status_code == 200
    assert "9394640" in resp.text
    assert "data:image/png;base64," in resp.text


def test_explain_cache_hit(explain_client: TestClient, tmp_path: Path) -> None:
    cache_dir = tmp_path / "explain-shipments"
    first = explain_client.get("/explain/9394640")
    assert first.status_code == 200
    cached = cache_dir / "explain-9394640.html"
    assert cached.is_file()

    mtime = cached.stat().st_mtime
    time.sleep(0.01)
    second = explain_client.get("/explain/9394640")
    assert second.status_code == 200
    assert second.text == first.text
    assert cached.stat().st_mtime == mtime


def test_explain_refresh_bypasses_cache(explain_client: TestClient, tmp_path: Path) -> None:
    cache_dir = tmp_path / "explain-shipments"
    explain_client.get("/explain/9394640")
    cached = cache_dir / "explain-9394640.html"
    mtime_before = cached.stat().st_mtime

    time.sleep(0.02)
    resp = explain_client.get("/explain/9394640?refresh=1")
    assert resp.status_code == 200
    assert cached.stat().st_mtime >= mtime_before


def test_explain_unknown_load_404(explain_client: TestClient) -> None:
    resp = explain_client.get("/explain/9999999")
    assert resp.status_code == 404
    assert "No ETP history" in resp.text
    assert "Back to search" in resp.text

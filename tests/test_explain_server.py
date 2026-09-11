"""Tests for the interactive ETP explainer FastAPI server."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.explain.auth import hash_password, verify_password
from app.explain.config import ExplainSettings, get_settings
from app.explain.dependencies import get_cache_dir, get_etp_lake
from app.explain.server import create_app
from dqt.etp_lake import EtpLake

USER = "etp"
PASSWORD = "test-secret"
AUTH = (USER, PASSWORD)


def _settings(tmp_path: Path, data_dir: Path, **overrides: object) -> ExplainSettings:
    values: dict[str, object] = {
        "DQT_DATA_DIR": str(data_dir),
        "EXPLAIN_CACHE_DIR": str(tmp_path / "explain-shipments"),
        "AUTH_ENABLED": "1",
        "AUTH_USERNAME": USER,
        "AUTH_PASSWORD": PASSWORD,
    }
    values.update(overrides)
    return ExplainSettings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(lake: EtpLake, settings: ExplainSettings) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_etp_lake] = lambda: lake
    app.dependency_overrides[get_cache_dir] = lambda: settings.get_cache_dir()
    return TestClient(app)


@pytest.fixture
def explain_client(timeline_lake: EtpLake, tmp_path: Path) -> TestClient:
    return _client(timeline_lake, _settings(tmp_path, timeline_lake.paths.data_dir))


def test_health_is_public(explain_client: TestClient) -> None:
    resp = explain_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_index_requires_auth(explain_client: TestClient) -> None:
    resp = explain_client.get("/")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


def test_wrong_password_rejected(explain_client: TestClient) -> None:
    resp = explain_client.get("/", auth=(USER, "nope"))
    assert resp.status_code == 401


def test_index_returns_search_form(explain_client: TestClient) -> None:
    resp = explain_client.get("/", auth=AUTH)
    assert resp.status_code == 200
    assert "loadnumber" in resp.text
    assert "/explain/" in resp.text


def test_bcrypt_password_accepted(timeline_lake: EtpLake, tmp_path: Path) -> None:
    settings = _settings(tmp_path, timeline_lake.paths.data_dir, AUTH_PASSWORD=hash_password(PASSWORD))
    client = _client(timeline_lake, settings)
    assert client.get("/", auth=AUTH).status_code == 200
    assert client.get("/", auth=(USER, "nope")).status_code == 401


def test_auth_disabled_allows_anonymous(timeline_lake: EtpLake, tmp_path: Path) -> None:
    settings = _settings(tmp_path, timeline_lake.paths.data_dir, AUTH_ENABLED="0", AUTH_PASSWORD="")
    client = _client(timeline_lake, settings)
    # HTTPBasic still demands *some* credentials header; any value passes.
    assert client.get("/", auth=("anyone", "anything")).status_code == 200


def test_verify_password_edge_cases() -> None:
    assert verify_password("x", "") is False
    assert verify_password("x", "$2b$12$not-a-real-hash") is False
    assert verify_password("plain", "plain") is True


def test_explain_renders_html(explain_client: TestClient) -> None:
    resp = explain_client.get("/explain/9394640", auth=AUTH)
    assert resp.status_code == 200
    assert "9394640" in resp.text
    assert "data:image/png;base64," in resp.text


def test_explain_cache_hit(explain_client: TestClient, tmp_path: Path) -> None:
    cache_dir = tmp_path / "explain-shipments"
    first = explain_client.get("/explain/9394640", auth=AUTH)
    assert first.status_code == 200
    cached = cache_dir / "explain-9394640.html"
    assert cached.is_file()

    mtime = cached.stat().st_mtime
    time.sleep(0.01)
    second = explain_client.get("/explain/9394640", auth=AUTH)
    assert second.status_code == 200
    assert second.text == first.text
    assert cached.stat().st_mtime == mtime


def test_explain_refresh_bypasses_cache(explain_client: TestClient, tmp_path: Path) -> None:
    cache_dir = tmp_path / "explain-shipments"
    explain_client.get("/explain/9394640", auth=AUTH)
    cached = cache_dir / "explain-9394640.html"
    mtime_before = cached.stat().st_mtime

    time.sleep(0.02)
    resp = explain_client.get("/explain/9394640?refresh=1", auth=AUTH)
    assert resp.status_code == 200
    assert cached.stat().st_mtime >= mtime_before


def test_explain_unknown_load_404(explain_client: TestClient) -> None:
    resp = explain_client.get("/explain/9999999", auth=AUTH)
    assert resp.status_code == 404
    assert "No ETP history" in resp.text
    assert "Back to search" in resp.text

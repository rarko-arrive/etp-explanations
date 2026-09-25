"""lake.env contract with etp-lake's VM sync, and mirror completeness checks."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from dqt.etp_lake import MIRROR_READY_MARKER, MIRROR_REQUIRED_PATHS, resolve_lake_mirror
from dqt.lake_env import lake_env_path, load_lake_env

REPO = Path(__file__).resolve().parents[1]
REGISTER = REPO / "scripts" / "register_lake_consumer.sh"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("DQT_LAKE_ENV", "DQT_DATA_DIR", "DQT_USE_LAKE_MIRROR", "MIRROR_LAKE", "DQT_LAKE_MIRROR"):
        monkeypatch.delenv(var, raising=False)
    for var in ("LAKE_VERSION", "LAKE_STATUS"):
        monkeypatch.delenv(var, raising=False)


def _complete_mirror(root: Path) -> Path:
    for rel in MIRROR_REQUIRED_PATHS:
        (root / rel / "ship_month=2026-08").mkdir(parents=True)
    (root / MIRROR_READY_MARKER).write_text("version=20260925T000000Z\n")
    return root


def test_load_lake_env_overrides_only_lake_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "lake.env"
    f.write_text(
        "# Written by etp-lake\n"
        "DQT_DATA_DIR=/team/data\n"
        "DQT_USE_LAKE_MIRROR=1\n"
        "DQT_LAKE_MIRROR=/mnt/dqt/etp_lake\n"
        "LAKE_VERSION=v2\n"
        "AUTH_PASSWORD=not-from-the-lake\n"
    )
    monkeypatch.setenv("DQT_LAKE_ENV", str(f))
    monkeypatch.setenv("DQT_DATA_DIR", "/from/dotenv")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    applied = load_lake_env()

    assert os.environ["DQT_DATA_DIR"] == "/team/data"
    assert os.environ["DQT_USE_LAKE_MIRROR"] == "1"
    assert applied["LAKE_VERSION"] == "v2"
    assert "AUTH_PASSWORD" not in os.environ


@pytest.mark.parametrize("value", ["off", "0", "none", "OFF"])
def test_lake_env_can_be_disabled(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DQT_LAKE_ENV", value)
    assert lake_env_path() is None
    assert load_lake_env() == {}


def test_lake_env_defaults_to_config_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DQT_LAKE_ENV", "")
    assert lake_env_path() == Path("~/.config/dqt/lake.env").expanduser()


def test_missing_lake_env_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DQT_LAKE_ENV", str(tmp_path / "nope.env"))
    assert load_lake_env() == {}


def test_complete_mirror_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mirror = _complete_mirror(tmp_path / "mirror")
    monkeypatch.setenv("MIRROR_LAKE", "1")  # etp-lake's synonym
    monkeypatch.setenv("DQT_LAKE_MIRROR", str(mirror))
    assert resolve_lake_mirror() == mirror.resolve()


def test_mirror_without_marker_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mirror = _complete_mirror(tmp_path / "mirror")
    (mirror / MIRROR_READY_MARKER).unlink()
    monkeypatch.setenv("DQT_USE_LAKE_MIRROR", "1")
    monkeypatch.setenv("DQT_LAKE_MIRROR", str(mirror))
    assert resolve_lake_mirror() is None


def test_mirror_missing_snapshots_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The lite-mirror bug: marker present, snapshots/ excluded → must not be trusted."""
    mirror = _complete_mirror(tmp_path / "mirror")
    for child in (mirror / "snapshots").iterdir():
        child.rmdir()
    monkeypatch.setenv("DQT_USE_LAKE_MIRROR", "1")
    monkeypatch.setenv("DQT_LAKE_MIRROR", str(mirror))
    assert resolve_lake_mirror() is None


def test_register_requires_match_python_constant():
    m = re.search(r'^REQUIRES="([^"]*)"', REGISTER.read_text(), re.MULTILINE)
    assert m, "REQUIRES= not found in register_lake_consumer.sh"
    assert tuple(m.group(1).split()) == MIRROR_REQUIRED_PATHS


@pytest.mark.parametrize(
    ("mode", "on_sync"),
    [("serve", "manage-explainer.sh ensure"), ("share", "scripts/share_explainer.sh ensure --port 8799")],
)
def test_register_writes_consumer_conf(tmp_path: Path, mode: str, on_sync: str):
    env = os.environ | {"DQT_CONFIG_DIR": str(tmp_path), "DQT_ALLOW_EPHEMERAL_REPO": "1"}
    args = ["bash", str(REGISTER), "--mode", mode] + (["--port", "8799"] if mode == "share" else [])
    subprocess.run(args, env=env, check=True, capture_output=True, text=True)

    conf = (tmp_path / "lake-consumers.d" / "etp-explanations.conf").read_text()
    kv = dict(line.split("=", 1) for line in conf.splitlines() if "=" in line and not line.startswith("#"))
    assert kv["requires"].split() == list(MIRROR_REQUIRED_PATHS)
    assert kv["on_sync"].endswith(on_sync)
    assert "lake-updated" in kv["on_update"]

    subprocess.run(["bash", str(REGISTER), "--unregister"], env=env, check=True, capture_output=True)
    assert not (tmp_path / "lake-consumers.d" / "etp-explanations.conf").exists()

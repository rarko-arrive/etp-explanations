"""Lake paths published by etp-lake's VM sync (``~/.config/dqt/lake.env``).

On an Azure ML VM, ``etp-lake scripts/vm_lake_sync.sh`` (systemd timer) mirrors the
team lake to local disk and writes ``lake.env``. It points at the mirror only when
the mirror is complete, otherwise at the team SoT. Apps call :func:`load_lake_env`
right after ``load_dotenv()`` so the lake — not each app's ``.env`` — decides where
parquet is read from.

``DQT_LAKE_ENV`` overrides the file path; ``DQT_LAKE_ENV=off`` disables it (tests,
laptops that want their own ``.env``).
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

DEFAULT_LAKE_ENV = Path("~/.config/dqt/lake.env")

#: Only these keys are taken from lake.env; everything else stays with the app.
LAKE_ENV_KEYS = (
    "DQT_DATA_DIR",
    "DQT_LAKE_MIRROR",
    "DQT_USE_LAKE_MIRROR",
    "MIRROR_LAKE",
    "LAKE_VERSION",
    "LAKE_STATUS",
)

_DISABLED = {"0", "off", "false", "no", "none"}


def lake_env_path() -> Path | None:
    """Resolved ``lake.env`` path, or ``None`` when disabled via ``DQT_LAKE_ENV``."""
    raw = (os.environ.get("DQT_LAKE_ENV") or "").strip()
    if raw.lower() in _DISABLED:
        return None
    return Path(os.path.expanduser(raw or os.fspath(DEFAULT_LAKE_ENV)))


def read_lake_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (no shell expansion), keeping only :data:`LAKE_ENV_KEYS`."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in LAKE_ENV_KEYS:
            values[key] = value.strip().strip("'\"")
    return values


def load_lake_env() -> dict[str, str]:
    """Export lake.env keys into ``os.environ`` (overriding ``.env``). Returns what was applied."""
    path = lake_env_path()
    if path is None or not path.is_file():
        return {}
    values = read_lake_env(path)
    os.environ.update(values)
    logger.info(
        "lake.env {} → data={} mirror={} version={} status={}",
        path,
        values.get("DQT_DATA_DIR", "-"),
        values.get("DQT_LAKE_MIRROR", "-") if values.get("DQT_USE_LAKE_MIRROR") == "1" else "off",
        values.get("LAKE_VERSION", "-"),
        values.get("LAKE_STATUS", "-"),
    )
    return values

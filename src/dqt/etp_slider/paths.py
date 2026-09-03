"""ETP parquet cache path resolution."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from dqt import repo_root, resolve_data_dir
from dqt.etp_slider.sql import PER_LOAD_CACHE

_LAYER_FALLBACKS: tuple[str, ...] = ("data", "data/current")


def resolve_etp_cache_dir(
    data_dir: str | Path | None = None,
    *,
    root: Path | None = None,
    required: str = PER_LOAD_CACHE,
) -> Path:
    """Return ``$LAYER/etp`` where ``required`` parquet exists.

    When ``DQT_DATA_DIR=data/current`` but mart caches live under ``data/etp/``,
    falls back across repo ``data`` and ``data/current`` with a warning.
    """
    root = root or repo_root()
    primary_layer = resolve_data_dir(data_dir, root=root)
    primary_cache = primary_layer / "etp"
    if (primary_cache / required).is_file():
        return primary_cache

    tried = {primary_cache.resolve()}
    for rel in _LAYER_FALLBACKS:
        alt_cache = (root / rel / "etp").resolve()
        if alt_cache in tried:
            continue
        tried.add(alt_cache)
        if (alt_cache / required).is_file():
            logger.warning(
                "ETP cache missing at {} — using {}",
                primary_cache / required,
                alt_cache,
            )
            return alt_cache

    return primary_cache

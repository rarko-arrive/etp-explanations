"""Dependency injection for FastAPI routes."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from app.explain.config import ExplainSettings, get_settings
from dqt.etp_lake import EtpLake


@lru_cache(maxsize=1)
def get_etp_lake(settings: Annotated[ExplainSettings, Depends(get_settings)]) -> EtpLake:
    """Get the ETP Lake instance (cached singleton).

    Args:
        settings: Application settings

    Returns:
        Initialized EtpLake instance
    """
    return EtpLake(settings.data_dir)


def get_cache_dir(settings: Annotated[ExplainSettings, Depends(get_settings)]) -> Path:
    """Get the cache directory path.

    Args:
        settings: Application settings

    Returns:
        Cache directory path
    """
    return settings.get_cache_dir()


# Type aliases for common dependencies
Settings = Annotated[ExplainSettings, Depends(get_settings)]
Lake = Annotated[EtpLake, Depends(get_etp_lake)]
CacheDir = Annotated[Path, Depends(get_cache_dir)]

"""Dependency injection for FastAPI routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends

from app.explain.config import ExplainSettings, get_settings
from dqt.etp_lake import EtpLake

# Global singleton for EtpLake (initialized on first access)
_lake_instance: EtpLake | None = None


def get_etp_lake(settings: Annotated[ExplainSettings, Depends(get_settings)]) -> EtpLake:
    """Get the ETP Lake instance (cached singleton).

    Args:
        settings: Application settings

    Returns:
        Initialized EtpLake instance
    """
    global _lake_instance
    if _lake_instance is None:
        _lake_instance = EtpLake(settings.data_dir)
    return _lake_instance


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

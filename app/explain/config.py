"""Centralized configuration management for the ETP Explainer application.

Uses Pydantic Settings for type-safe, validated configuration with environment
variable support and sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExplainSettings(BaseSettings):
    """Application configuration settings.

    Loads from environment variables or .env file. All settings have sensible
    defaults except data_dir which must be provided.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server configuration
    host: str = Field(
        default="127.0.0.1",
        description="Server bind address",
        validation_alias="EXPLAIN_HOST",
    )
    port: int = Field(
        default=8765,
        ge=1,
        le=65535,
        description="Server port",
        validation_alias="EXPLAIN_PORT",
    )
    behind_proxy: bool = Field(
        default=False,
        description="Trust X-Forwarded-* headers from proxy",
        validation_alias="EXPLAIN_BEHIND_PROXY",
    )

    # Data paths
    data_dir: Path = Field(
        default=Path("data"),
        description="Root data directory containing etp_lake and cohort data",
        validation_alias="DQT_DATA_DIR",
    )
    cache_dir: Path | None = Field(
        default=None,
        description="Writable cache directory for rendered HTML (None = data/explain-shipments)",
        validation_alias="EXPLAIN_CACHE_DIR",
    )

    # Authentication
    auth_enabled: bool = Field(
        default=True,
        description="Enable basic authentication (disable for local dev only)",
        validation_alias="AUTH_ENABLED",
    )
    auth_username: str = Field(
        default="etp",
        description="Basic auth username",
        validation_alias="AUTH_USERNAME",
    )
    auth_password: str = Field(
        default="",
        description="Basic auth password (bcrypt hash or plaintext for dev)",
        validation_alias="AUTH_PASSWORD",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
        validation_alias="DQT_LOG_LEVEL",
    )

    @field_validator("data_dir", "cache_dir", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path | None) -> Path | None:
        """Expand user paths and environment variables in path settings."""
        if v is None:
            return None
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            v = os.path.expanduser(os.path.expandvars(v))
        return Path(v)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is uppercase and valid."""
        v = v.upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return v

    def get_cache_dir(self) -> Path:
        """Get the cache directory, using default if not explicitly set."""
        if self.cache_dir is not None:
            return self.cache_dir
        return Path("data/explain-shipments")


# Global settings instance (lazy-loaded on first access)
_settings: ExplainSettings | None = None


def get_settings() -> ExplainSettings:
    """Get the global settings instance (singleton pattern)."""
    global _settings
    if _settings is None:
        _settings = ExplainSettings()
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance (useful for testing)."""
    global _settings
    _settings = None

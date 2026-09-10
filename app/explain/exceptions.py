"""Custom exception types for the ETP Explainer application."""

from __future__ import annotations


class ExplainerError(Exception):
    """Base exception for all explainer errors."""


class LoadNotFoundError(ExplainerError):
    """Raised when etp-lake has no history for the requested load."""

    def __init__(self, loadnumber: int, message: str | None = None):
        self.loadnumber = loadnumber
        if message is None:
            message = f"No ETP history found for load {loadnumber}"
        super().__init__(message)


class CacheError(ExplainerError):
    """Raised when cache operations fail."""


class ConfigurationError(ExplainerError):
    """Raised when configuration is invalid or missing."""


class AuthenticationError(ExplainerError):
    """Raised when authentication fails."""
